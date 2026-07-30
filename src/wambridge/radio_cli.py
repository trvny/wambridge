"""Radio commands layered on top of the core WAM Bridge CLI."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from time import monotonic, sleep

from . import cli
from .profiles import ProfileError, ProfileStore
from .samsung import (
    WamApiError,
    get_mute,
    get_radio_info,
    get_volume,
    probe,
    set_mute,
    set_volume,
)
from .station_packs import get_station_pack, station_pack_names
from .stations import RadioStation, StationError, StationStore
from .stream import StreamError, continuous_source
from .tunein import (
    find_tunein_preset,
    get_tunein_presets,
    play_tunein_preset,
)

LOGGER = logging.getLogger("wambridge")


def build_parser() -> argparse.ArgumentParser:
    """Extend the core parser with radio station and TuneIn actions."""
    parser = cli.build_parser()
    radio = parser.add_mutually_exclusive_group()
    radio.add_argument(
        "--radio-add",
        nargs="+",
        metavar="ALIAS_OR_URL",
        help="Save a station as ALIAS URL [FALLBACK_URL ...]",
    )
    radio.add_argument(
        "--radio-import",
        metavar="PACK",
        help=(
            "Import a bundled station pack; available: "
            f"{', '.join(station_pack_names())}"
        ),
    )
    radio.add_argument(
        "--radio-list",
        action="store_true",
        help="List custom internet-radio stations",
    )
    radio.add_argument(
        "--radio-remove",
        metavar="ALIAS",
        help="Remove a custom internet-radio station",
    )
    radio.add_argument(
        "--radio-play",
        metavar="ALIAS",
        help="Play a custom station through the local FFmpeg bridge",
    )
    radio.add_argument(
        "--tunein-list",
        action="store_true",
        help="List native TuneIn presets stored by the speaker",
    )
    radio.add_argument(
        "--tunein-play",
        metavar="ID_OR_TITLE",
        help="Play a native TuneIn preset by content ID or exact title",
    )
    parser.add_argument(
        "--stations-config",
        type=Path,
        help="Override the per-user custom radio station file",
    )
    return parser


def _radio_action(args: argparse.Namespace) -> bool:
    return any(
        (
            args.radio_add,
            args.radio_import,
            args.radio_list,
            args.radio_remove,
            args.radio_play,
            args.tunein_list,
            args.tunein_play,
        )
    )


def _legacy_action(args: argparse.Namespace) -> bool:
    return any(
        (
            args.probe,
            args.discover,
            args.remember,
            args.list_devices,
            args.forget,
            args.status,
            args.set_volume is not None,
            args.mute,
            args.unmute,
            args.pause,
            args.play,
            args.stop,
            args.standby,
        )
    )


def _print_stations(store: StationStore) -> int:
    stations = store.all()
    if not stations:
        print("No custom radio stations saved")
        return 0
    for station in stations:
        print(f"{station.alias}\tprimary\t{station.url}")
        for fallback in station.fallback_urls:
            print(f"{station.alias}\tfallback\t{fallback}")
    return 0


def _print_tunein_presets(
    speaker_ip: str,
    *,
    port: int,
) -> int:
    presets = get_tunein_presets(speaker_ip, port=port)
    if not presets:
        print("No TuneIn presets returned by Samsung WAM")
        return 0
    for preset in presets:
        print(f"{preset.content_id}\t{preset.kind}\t{preset.title}")
    return 0


def _wait_for_tunein_playback(
    speaker_ip: str,
    *,
    port: int,
    timeout: float = 25.0,
) -> None:
    deadline = monotonic() + timeout
    last_error: WamApiError | None = None
    while monotonic() < deadline:
        try:
            status = get_radio_info(
                speaker_ip,
                port=port,
                timeout=min(5.0, max(1.0, deadline - monotonic())),
            )
        except WamApiError as error:
            last_error = error
        else:
            if (
                status.cp_name
                and status.cp_name.casefold() == "tunein"
                and status.play_status
                and status.play_status.casefold() == "play"
            ):
                return
        sleep(0.5)
    suffix = f": {last_error}" if last_error else ""
    raise WamApiError(f"TuneIn preset did not start before timeout{suffix}")


def _play_tunein_safely(
    args: argparse.Namespace,
    speaker_ip: str,
    speaker_port: int,
) -> int:
    previous_volume = get_volume(speaker_ip, port=speaker_port)
    previous_mute = get_mute(speaker_ip, port=speaker_port)
    start_volume = cli.choose_start_volume(
        previous_volume,
        args.volume,
        args.max_start_volume,
    )
    startup_complete = False
    tunein_touched = False
    preset = None
    try:
        # SetSelectRadio may wake or resume old TuneIn state on quirky firmware.
        set_volume(speaker_ip, 0, port=speaker_port)
        set_mute(speaker_ip, True, port=speaker_port)
        tunein_touched = True
        presets = get_tunein_presets(speaker_ip, port=speaker_port)
        preset = find_tunein_preset(presets, args.tunein_play)
        play_tunein_preset(
            speaker_ip,
            preset,
            port=speaker_port,
        )
        _wait_for_tunein_playback(
            speaker_ip,
            port=speaker_port,
        )
        set_volume(speaker_ip, start_volume, port=speaker_port)
        set_mute(speaker_ip, False, port=speaker_port)
        startup_complete = True
    finally:
        if not startup_complete:
            try:
                if tunein_touched:
                    set_volume(speaker_ip, 0, port=speaker_port)
                    set_mute(speaker_ip, True, port=speaker_port)
                else:
                    set_volume(
                        speaker_ip,
                        previous_volume,
                        port=speaker_port,
                    )
                    set_mute(
                        speaker_ip,
                        previous_mute,
                        port=speaker_port,
                    )
            except WamApiError as error:
                LOGGER.warning(
                    "Could not secure WAM state after TuneIn startup failure: %s",
                    error,
                )
    assert preset is not None
    print(
        f"Playing TuneIn preset {preset.content_id}: {preset.title} "
        f"at volume {start_volume}"
    )
    return 0


def _play_custom_station(
    args: argparse.Namespace,
    station: RadioStation,
) -> int:
    """Try a station's primary stream and fallbacks in order."""
    failures: list[str] = []
    original_source = args.source
    try:
        for index, url in enumerate(station.all_urls, start=1):
            args.source = url
            LOGGER.info(
                "Trying radio station %s stream %s/%s: %s",
                station.alias,
                index,
                len(station.all_urls),
                url,
            )
            try:
                with continuous_source(url):
                    result = cli.run(args)
            except (RuntimeError, StreamError, WamApiError) as error:
                failures.append(f"{url}: {error}")
                if index < len(station.all_urls):
                    LOGGER.warning(
                        "Radio stream failed, trying fallback %s/%s",
                        index + 1,
                        len(station.all_urls),
                    )
                continue
            if result in {0, 130}:
                return result
            failures.append(f"{url}: exited with status {result}")
    finally:
        args.source = original_source

    details = "; ".join(failures)
    raise RuntimeError(
        f"All streams failed for radio station {station.alias!r}: {details}"
    )


def run(args: argparse.Namespace) -> int:
    """Run radio actions or delegate unchanged commands to the core CLI."""
    if not _radio_action(args):
        return cli.run(args)
    if args.source:
        raise RuntimeError(
            "A positional audio source cannot be combined with a radio action"
        )
    if _legacy_action(args):
        raise RuntimeError(
            "A radio action cannot be combined with another control action"
        )

    station_store = StationStore(args.stations_config)
    if args.radio_add:
        if len(args.radio_add) < 2:
            raise RuntimeError(
                "--radio-add needs ALIAS URL [FALLBACK_URL ...]"
            )
        alias, primary_url, *fallback_urls = args.radio_add
        station = RadioStation(
            alias=alias,
            url=primary_url,
            fallback_urls=tuple(fallback_urls),
        )
        station_store.put(station)
        print(
            f"Saved radio station {station.alias} with "
            f"{len(station.all_urls)} stream(s)"
        )
        return 0
    if args.radio_import:
        imported = station_store.put_many(get_station_pack(args.radio_import))
        aliases = ", ".join(station.alias for station in imported)
        print(f"Imported radio pack {args.radio_import}: {aliases}")
        return 0
    if args.radio_list:
        return _print_stations(station_store)
    if args.radio_remove:
        removed = station_store.remove(args.radio_remove)
        print(f"Removed radio station {removed.alias}")
        return 0
    if args.radio_play:
        station = station_store.get(args.radio_play)
        return _play_custom_station(args, station)

    profile_store = ProfileStore(args.config)
    speaker_ip, speaker_port = cli.select_speaker(args, profile_store)
    response = probe(speaker_ip, port=speaker_port)
    LOGGER.info(
        "Speaker %s replied with %s",
        speaker_ip,
        response.method or "XML",
    )
    if args.tunein_list:
        return _print_tunein_presets(speaker_ip, port=speaker_port)
    return _play_tunein_safely(args, speaker_ip, speaker_port)


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point with radio extensions."""
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )
    try:
        return run(args)
    except (
        RuntimeError,
        StreamError,
        WamApiError,
        ProfileError,
        StationError,
        ValueError,
    ) as error:
        LOGGER.error("%s", error)
        return 1


if __name__ == "__main__":
    sys.exit(main())
