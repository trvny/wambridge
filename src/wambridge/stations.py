"""Persistent user-defined radio stations."""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any
from urllib.parse import urlparse

CONFIG_VERSION = 1


class StationError(RuntimeError):
    """Raised when saved radio stations are invalid or unavailable."""


@dataclass(frozen=True, slots=True)
class RadioStation:
    """A named internet-radio stream with optional fallback URLs."""

    alias: str
    url: str
    fallback_urls: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> RadioStation:
        """Validate and construct a station loaded from JSON."""
        try:
            alias = str(value["alias"]).strip()
            url = str(value["url"]).strip()
            raw_fallbacks = value.get("fallback_urls", ())
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
        return cls(
            alias=alias,
            url=validated_urls[0],
            fallback_urls=validated_urls[1:],
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
        return payload


def validate_station_url(url: str) -> str:
    """Return a validated HTTP or HTTPS stream URL."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise StationError("Radio station URL must use HTTP or HTTPS")
    return url


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
    if configured := os.environ.get("WAMBRIDGE_STATIONS_CONFIG"):
        return Path(configured).expanduser()
    if local_app_data := os.environ.get("LOCALAPPDATA"):
        return Path(local_app_data) / "WAMBridge" / "stations.json"
    config_home = Path(
        os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")
    )
    return config_home / "wambridge" / "stations.json"


def _alias_key(alias: str) -> str:
    key = alias.strip().casefold()
    if not key:
        raise StationError("Radio station alias cannot be empty")
    return key


class StationStore:
    """Read and atomically update saved radio stations."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_station_path()

    def load(self) -> dict[str, RadioStation]:
        """Load stations indexed by case-insensitive alias."""
        if not self.path.exists():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise StationError(
                f"Cannot read radio stations from {self.path}: {error}"
            ) from error
        if not isinstance(payload, dict):
            raise StationError(f"Unsupported radio station file: {self.path}")
        if (
            payload.get("version") != CONFIG_VERSION
            or not isinstance(payload.get("stations"), list)
        ):
            raise StationError(f"Unsupported radio station file: {self.path}")

        stations: dict[str, RadioStation] = {}
        for raw_station in payload["stations"]:
            if not isinstance(raw_station, dict):
                raise StationError(
                    f"Invalid radio station entry in {self.path}"
                )
            station = RadioStation.from_dict(raw_station)
            stations[_alias_key(station.alias)] = station
        return stations

    def all(self) -> list[RadioStation]:
        """Return saved stations ordered by alias."""
        return sorted(
            self.load().values(),
            key=lambda station: station.alias.casefold(),
        )

    def get(self, alias: str) -> RadioStation:
        """Return one saved station."""
        station = self.load().get(_alias_key(alias))
        if station is None:
            raise StationError(f"No saved radio station named {alias!r}")
        return station

    def put(self, station: RadioStation) -> None:
        """Create or replace a saved station."""
        self.put_many([station])

    def put_many(self, new_stations: Iterable[RadioStation]) -> list[RadioStation]:
        """Create or replace multiple stations in one atomic update."""
        validated = [
            RadioStation.from_dict(station.to_dict())
            for station in new_stations
        ]
        stations = self.load()
        for station in validated:
            stations[_alias_key(station.alias)] = station
        self._save(stations.values())
        return validated

    def remove(self, alias: str) -> RadioStation:
        """Delete and return a saved station."""
        stations = self.load()
        try:
            removed = stations.pop(_alias_key(alias))
        except KeyError as error:
            raise StationError(
                f"No saved radio station named {alias!r}"
            ) from error
        self._save(stations.values())
        return removed

    def _save(self, stations: Iterable[RadioStation]) -> None:
        payload = {
            "version": CONFIG_VERSION,
            "stations": [
                station.to_dict()
                for station in sorted(
                    stations,
                    key=lambda item: item.alias.casefold(),
                )
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
            raise StationError(
                f"Cannot save radio stations to {self.path}: {error}"
            ) from error
