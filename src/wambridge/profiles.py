"""Persistent Samsung WAM device profiles and address resolution."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from .alias_store import CONFIG_VERSION, AliasStore, default_config_path
from .discovery import (
    DiscoveredSpeaker,
    discover_ssdp,
    local_ipv4_addresses,
    scan_local_subnets,
)
from .samsung import DEFAULT_PORT, WamApiError, WamIdentity, identify, normalize_device_id

__all__ = [
    "CONFIG_VERSION",
    "DeviceProfile",
    "ProfileError",
    "ProfileStore",
    "default_profile_path",
    "remember_device",
    "resolve_device",
    "resolve_profile",
]


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
    return default_config_path("WAMBRIDGE_CONFIG", "devices.json")


class ProfileStore(AliasStore[DeviceProfile]):
    """Read and atomically update the user's saved WAM devices."""

    error = ProfileError
    collection = "devices"
    entry_label = "WAM profile"
    plural_label = "WAM profiles"
    subject_label = "WAM device"

    @classmethod
    def default_path(cls) -> Path:
        return default_profile_path()

    @classmethod
    def parse(cls, value: dict[str, Any]) -> DeviceProfile:
        return DeviceProfile.from_dict(value)


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
