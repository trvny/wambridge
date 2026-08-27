"""Bundled station packs shared by desktop and the Android app."""

from __future__ import annotations

import json
from importlib.resources import files

from .stations import RadioStation, StationError

_DATA_FILE = "station_packs.json"


def _load_station_packs() -> dict[str, tuple[RadioStation, ...]]:
    """Load the maintained station catalogue from the package JSON."""
    try:
        payload = json.loads(
            files(__package__).joinpath(_DATA_FILE).read_text(encoding="utf-8")
        )
        raw_stations = payload["stations"]
        raw_packs = payload["packs"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise StationError(f"Invalid bundled station data: {error}") from error

    stations: dict[str, RadioStation] = {}
    for raw_station in raw_stations:
        station = RadioStation.from_dict(raw_station)
        if station.alias in stations:
            raise StationError(f"Duplicate bundled station alias: {station.alias}")
        stations[station.alias] = station
    packs: dict[str, tuple[RadioStation, ...]] = {}
    for raw_name, raw_aliases in raw_packs.items():
        name = str(raw_name).strip().casefold()
        if not name or not isinstance(raw_aliases, list):
            raise StationError(f"Invalid bundled station pack: {raw_name!r}")
        try:
            pack = tuple(stations[str(alias)] for alias in raw_aliases)
        except KeyError as error:
            raise StationError(
                f"Station pack {name!r} references unknown alias {error.args[0]!r}"
            ) from error
        if not pack:
            raise StationError(f"Bundled station pack {name!r} cannot be empty")
        packs[name] = pack
    return packs


STATION_PACKS = _load_station_packs()


def station_pack_names() -> tuple[str, ...]:
    """Return bundled station-pack names."""
    return tuple(sorted(STATION_PACKS))


def get_station_pack(name: str) -> tuple[RadioStation, ...]:
    """Return a bundled station pack by case-insensitive name."""
    key = name.strip().casefold()
    try:
        return STATION_PACKS[key]
    except KeyError as error:
        available = ", ".join(station_pack_names())
        raise StationError(
            f"Unknown radio station pack {name!r}; available: {available}"
        ) from error
