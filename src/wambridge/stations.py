"""Persistent user-defined radio stations."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .alias_store import CONFIG_VERSION, AliasStore, default_config_path

__all__ = [
    "CONFIG_VERSION",
    "RadioStation",
    "StationError",
    "StationStore",
    "default_station_path",
    "validate_station_url",
    "validate_tunein_id",
]

# TuneIn station ids look like `s15984`. The catalogue also uses `p` for podcasts
# and `t` for individual episodes; neither is a live stream, so neither belongs
# in a station entry.
_TUNEIN_ID_RE = re.compile(r"\As[0-9]{1,12}\Z")


class StationError(RuntimeError):
    """Raised when saved radio stations are invalid or unavailable."""


@dataclass(frozen=True, slots=True)
class RadioStation:
    """A named internet-radio stream with optional fallback URLs."""

    alias: str
    url: str
    fallback_urls: tuple[str, ...] = ()
    # A TuneIn station id resolves to whatever stream the broadcaster serves
    # today, so it survives an endpoint move that would leave a hardcoded URL
    # dead. It is resolved at play time and never stored as a URL, because the
    # answer changes; `url` and `fallback_urls` stay as the static safety net
    # for when TuneIn is unreachable or offers only formats the speaker refuses.
    tunein_id: str | None = None

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> RadioStation:
        """Validate and construct a station loaded from JSON."""
        try:
            alias = str(value["alias"]).strip()
            url = str(value["url"]).strip()
            raw_fallbacks = value.get("fallback_urls", ())
            raw_tunein = value.get("tunein_id")
        except (KeyError, TypeError) as error:
            raise StationError(f"Invalid radio station: {value!r}") from error
        if isinstance(raw_fallbacks, str):
            raw_fallbacks = [raw_fallbacks]
        if not isinstance(raw_fallbacks, (list, tuple)):
            raise StationError(f"Invalid radio station fallbacks: {value!r}")
        fallback_urls = tuple(str(item).strip() for item in raw_fallbacks)
        if not alias:
            raise StationError("Radio station alias cannot be empty")
        validated_urls = _validated_unique_urls((url, *fallback_urls))
        tunein_id = (
            validate_tunein_id(str(raw_tunein).strip())
            if raw_tunein not in (None, "")
            else None
        )
        return cls(
            alias=alias,
            url=validated_urls[0],
            fallback_urls=validated_urls[1:],
            tunein_id=tunein_id,
        )

    @property
    def all_urls(self) -> tuple[str, ...]:
        """Return the primary URL followed by unique fallbacks."""
        return (self.url, *self.fallback_urls)

    def to_dict(self) -> dict[str, Any]:
        """Return JSON-safe station data."""
        payload: dict[str, Any] = {"alias": self.alias, "url": self.url}
        if self.fallback_urls:
            payload["fallback_urls"] = list(self.fallback_urls)
        if self.tunein_id:
            payload["tunein_id"] = self.tunein_id
        return payload


def validate_station_url(url: str) -> str:
    """Return a validated HTTP or HTTPS stream URL."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise StationError("Radio station URL must use HTTP or HTTPS")
    return url


def validate_tunein_id(value: str) -> str:
    """Return a validated TuneIn station id such as ``s15984``."""
    if not _TUNEIN_ID_RE.match(value):
        raise StationError(
            f"TuneIn station id must look like s15984, got {value!r}"
        )
    return value


def _validated_unique_urls(urls: Iterable[str]) -> tuple[str, ...]:
    validated: list[str] = []
    seen: set[str] = set()
    for raw_url in urls:
        url = validate_station_url(raw_url.strip())
        if url in seen:
            continue
        seen.add(url)
        validated.append(url)
    if not validated:
        raise StationError("Radio station needs at least one stream URL")
    return tuple(validated)


def default_station_path() -> Path:
    """Return the per-user radio station file."""
    return default_config_path("WAMBRIDGE_STATIONS_CONFIG", "stations.json")


class StationStore(AliasStore[RadioStation]):
    """Read and atomically update saved radio stations."""

    error = StationError
    collection = "stations"
    entry_label = "radio station"
    plural_label = "radio stations"
    subject_label = "radio station"

    @classmethod
    def default_path(cls) -> Path:
        return default_station_path()

    @classmethod
    def parse(cls, value: dict[str, Any]) -> RadioStation:
        return RadioStation.from_dict(value)

    @classmethod
    def validated(cls, entry: RadioStation) -> RadioStation:
        """Re-validate a station so stored URLs are checked and deduplicated."""
        return RadioStation.from_dict(entry.to_dict())
