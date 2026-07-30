"""Persistent Samsung WAM device profiles and address resolution."""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from .discovery import (
    DiscoveredSpeaker,
    discover_ssdp,
    local_ipv4_addresses,
    scan_local_subnets,
)
from .samsung import DEFAULT_PORT, WamApiError, WamIdentity, identify, normalize_device_id

CONFIG_VERSION = 1


class ProfileError(RuntimeError):
    """Raised when a saved WAM profile cannot be read or resolved."""


@dataclass(frozen=True, slots=True)
class DeviceProfile:
    """A stable WAM identity plus the most recently working network address."""

    alias: str
    device_id: str
    name: str
    last_ip: str
    port: int = DEFAULT_PORT

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> DeviceProfile:
        """Validate and construct a profile loaded from JSON."""
        try:
            alias = str(value["alias"]).strip()
            device_id = normalize_device_id(str(value["device_id"]))
            name = str(value.get("name") or alias).strip()
            last_ip = str(value["last_ip"]).strip()
            port = int(value.get("port", DEFAULT_PORT))
        except (KeyError, TypeError, ValueError) as error:
            raise ProfileError(f"Invalid WAM device profile: {value!r}") from error
        if not alias or not device_id or not last_ip or not 1 <= port <= 65535:
            raise ProfileError(f"Invalid WAM device profile: {value!r}")
        return cls(
            alias=alias,
            device_id=device_id,
            name=name or alias,
            last_ip=last_ip,
            port=port,
        )

    def to_dict(self) -> dict[str, Any]:
        """Return JSON-safe profile data."""
        return asdict(self)


def default_profile_path() -> Path:
    """Return the per-user profile file used by the bridge and foobar helper."""
    if configured := os.environ.get("WAMBRIDGE_CONFIG"):
        return Path(configured).expanduser()
    if local_app_data := os.environ.get("LOCALAPPDATA"):
        return Path(local_app_data) / "WAMBridge" / "devices.json"
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return config_home / "wambridge" / "devices.json"


def _alias_key(alias: str) -> str:
    key = alias.strip().casefold()
    if not key:
        raise ProfileError("Device alias cannot be empty")
    return key


class ProfileStore:
    """Read and atomically update the user's saved WAM devices."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_profile_path()

    def load(self) -> dict[str, DeviceProfile]:
        """Load profiles indexed by a case-insensitive alias."""
        if not self.path.exists():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ProfileError(f"Cannot read WAM profiles from {self.path}: {error}") from error
        if not isinstance(payload, dict):
            raise ProfileError(f"Unsupported WAM profile file: {self.path}")
        if payload.get("version") != CONFIG_VERSION or not isinstance(payload.get("devices"), list):
            raise ProfileError(f"Unsupported WAM profile file: {self.path}")

        profiles: dict[str, DeviceProfile] = {}
        for raw_profile in payload["devices"]:
            if not isinstance(raw_profile, dict):
                raise ProfileError(f"Invalid WAM profile entry in {self.path}")
            profile = DeviceProfile.from_dict(raw_profile)
            profiles[_alias_key(profile.alias)] = profile
        return profiles

    def all(self) -> list[DeviceProfile]:
        """Return saved devices ordered by alias."""
        return sorted(self.load().values(), key=lambda profile: profile.alias.casefold())

    def get(self, alias: str) -> DeviceProfile:
        """Return one saved device."""
        profile = self.load().get(_alias_key(alias))
        if profile is None:
            raise ProfileError(f"No saved WAM device named {alias!r}")
        return profile

    def put(self, profile: DeviceProfile) -> None:
        """Create or replace a saved device."""
        profiles = self.load()
        profiles[_alias_key(profile.alias)] = profile
        self._save(profiles.values())

    def remove(self, alias: str) -> DeviceProfile:
        """Delete and return a saved device."""
        profiles = self.load()
        try:
            removed = profiles.pop(_alias_key(alias))
        except KeyError as error:
            raise ProfileError(f"No saved WAM device named {alias!r}") from error
        self._save(profiles.values())
        return removed

    def _save(self, profiles: Iterable[DeviceProfile]) -> None:
        payload = {
            "version": CONFIG_VERSION,
            "devices": [
                profile.to_dict()
                for profile in sorted(profiles, key=lambda item: item.alias.casefold())
            ],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with NamedTemporaryFile(
                "w",
                encoding="utf-8",
                newline="\n",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                json.dump(payload, temporary, ensure_ascii=False, indent=2)
                temporary.write("\n")
                temporary_path = Path(temporary.name)
            os.replace(temporary_path, self.path)
        except OSError as error:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise ProfileError(f"Cannot save WAM profiles to {self.path}: {error}") from error


IdentifyFunc = Callable[..., WamIdentity]
DiscoverFunc = Callable[..., list[DiscoveredSpeaker]]
ScanFunc = Callable[..., list[DiscoveredSpeaker]]


def remember_device(
    alias: str,
    speaker_ip: str,
    *,
    port: int = DEFAULT_PORT,
    store: ProfileStore | None = None,
    identify_func: IdentifyFunc = identify,
) -> DeviceProfile:
    """Read stable identity from a speaker and save it under an alias."""
    identity = identify_func(speaker_ip, port=port)
    normalized_device_id = normalize_device_id(identity.device_id)
    if not normalized_device_id:
        raise ProfileError("Samsung WAM returned an empty device ID")
    profile = DeviceProfile(
        alias=alias.strip(),
        device_id=normalized_device_id,
        name=identity.name or alias.strip(),
        last_ip=speaker_ip,
        port=port,
    )
    if not profile.alias:
        raise ProfileError("Device alias cannot be empty")
    (store or ProfileStore()).put(profile)
    return profile


def resolve_profile(
    profile: DeviceProfile,
    *,
    timeout: float = 4.0,
    local_addresses: Iterable[str] | None = None,
    scan: bool = True,
    identify_func: IdentifyFunc = identify,
    discover_func: DiscoverFunc = discover_ssdp,
    scan_func: ScanFunc = scan_local_subnets,
) -> DeviceProfile:
    """Find the current IP of a saved speaker by matching its stable device ID."""

    def identify_at(ip: str, request_timeout: float) -> WamIdentity | None:
        try:
            return identify_func(
                ip,
                port=profile.port,
                timeout=request_timeout,
            )
        except (WamApiError, OSError, TimeoutError):
            return None

    current = identify_at(profile.last_ip, min(timeout, 2.0))
    if current and normalize_device_id(current.device_id) == profile.device_id:
        return replace(profile, name=current.name or profile.name)

    addresses = list(dict.fromkeys(local_addresses or local_ipv4_addresses()))

    def match(candidates: Iterable[DiscoveredSpeaker]) -> DeviceProfile | None:
        for candidate in candidates:
            identity = identify_at(candidate.ip, min(timeout, 2.0))
            if identity and normalize_device_id(identity.device_id) == profile.device_id:
                return replace(
                    profile,
                    last_ip=candidate.ip,
                    name=identity.name or profile.name,
                )
        return None

    ssdp_match = match(
        discover_func(
            timeout=timeout,
            local_addresses=addresses,
        )
    )
    if ssdp_match is not None:
        return ssdp_match

    if scan:
        scan_match = match(scan_func(addresses, port=profile.port))
        if scan_match is not None:
            return scan_match

    raise ProfileError(
        f"Saved WAM device {profile.alias!r} ({profile.device_id}) was not found; "
        f"last address was {profile.last_ip}"
    )


def resolve_device(
    alias: str,
    *,
    store: ProfileStore | None = None,
    timeout: float = 4.0,
    local_addresses: Iterable[str] | None = None,
    scan: bool = True,
    identify_func: IdentifyFunc = identify,
    discover_func: DiscoverFunc = discover_ssdp,
    scan_func: ScanFunc = scan_local_subnets,
) -> DeviceProfile:
    """Resolve a saved alias and persist a changed IP or display name."""
    selected_store = store or ProfileStore()
    previous = selected_store.get(alias)
    resolved = resolve_profile(
        previous,
        timeout=timeout,
        local_addresses=local_addresses,
        scan=scan,
        identify_func=identify_func,
        discover_func=discover_func,
        scan_func=scan_func,
    )
    if resolved != previous:
        selected_store.put(resolved)
    return resolved
