"""Radio commands layered on top of the core WAM Bridge CLI."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from time import monotonic, sleep

from . import cli
from .catalogue import (
    RadioPage,
    current_page,
    descend,
    open_catalogue,
    search,
    station_detail,
)
from .cli_common import configure_logging
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
from .stations import (
    RadioStation,
    StationError,
    StationStore,
    validate_tunein_id,
)
from .stream import StreamError, continuous_source
from .tunein import (
    find_tunein_preset,
    get_tunein_presets,
    play_tunein_preset,
    resolve_tunein_station,
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
    parser.add_argument(
        "--tunein-id",
        metavar="ID",
        help=(
            "TuneIn station id for --radio-add, e.g. s15984. Resolved at play "
            "time and tried before the saved URLs, which stay as fallbacks"
        ),
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
    radio.add_argument(
        "--tunein-browse",
        nargs="?",
        const="",
        metavar="PATH",
        help=(
            "Browse the speaker's TuneIn catalogue. PATH is a slash-separated "
            "list of row numbers from the pages above it, e.g. 1/0; without one "
            "the catalogue root is listed. A row that is a station is shown with "
            "its playable URL"
        ),
    )
    radio.add_argument(
        "--tunein-search",
        metavar="QUERY",
        help="Search the TuneIn catalogue by name",
    )
    parser.add_argument(
        "--tunein-start",
        type=int,
        default=0,
        metavar="N",
        help=(
            "First row to show for --tunein-browse or --tunein-search; levels "
            "are paginated and can run to dozens of entries"
        ),
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
            # An empty string is a real value here: it means the catalogue root.
            args.tunein_browse is not None,
            args.tunein_search,
        )
    )


def _print_stations(store: StationStore) -> int:
    stations = store.all()
    if not stations:
        print("No custom radio stations saved")
        return 0
    for station in stations:
        # The id is listed first because it is what gets tried first, and because
        # a station having one is otherwise invisible: the saved URLs look
        # identical whether or not they are about to be overtaken.
        if station.tunein_id:
            print(f"{station.alias}\ttunein\t{station.tunein_id}")
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


def _print_page(page: RadioPage) -> None:
    shown = len(page.entries)
    total = page.total if page.total is not None else shown
    header = page.category or "?"
    print(f"{header}\t{page.start_index}-{page.start_index + shown} of {total}")
    for entry in page.entries:
        if entry.is_station:
            kind, extra = "station", entry.media_id or ""
        elif entry.is_folder:
            kind, extra = "folder", ""
        else:
            kind, extra = f"type{entry.item_type}", entry.media_id or ""
        print(f"{entry.content_id}\t{kind}\t{extra}\t{entry.title}")
    if page.has_more:
        print(f"(more: --tunein-start {page.start_index + shown})")


def _browse_catalogue(
    speaker_ip: str,
    *,
    port: int,
    path: str,
    start_index: int,
) -> int:
    """List one level of the catalogue, or one station's detail.

    The speaker holds the cursor and it survives this process, so every run
    starts by normalising to the root and walks the path from there. That costs
    one request per level and buys an interface that has no hidden state: the
    same path always means the same place.
    """
    segments = [part for part in path.split("/") if part.strip()]
    try:
        rows = [int(part) for part in segments]
    except ValueError:
        raise RuntimeError(
            f"--tunein-browse takes row numbers separated by /, not {path!r}"
        ) from None

    page = open_catalogue(speaker_ip, port=port)
    for position, row in enumerate(rows):
        entry = next(
            (item for item in page.entries if item.content_id == str(row)),
            None,
        )
        if entry is None:
            raise RuntimeError(f"No row {row} on {page.category or 'this level'}")
        if entry.is_station:
            if position != len(rows) - 1:
                raise RuntimeError(
                    f"Row {row} ({entry.title}) is a station, so nothing is below it"
                )
            detail = station_detail(speaker_ip, entry.index, port=port)
            print(f"{detail.title or entry.title}")
            if entry.media_id:
                print(f"tunein\t{entry.media_id}")
            if detail.description:
                print(f"about\t{detail.description}")
            if detail.station_url:
                print(f"url\t{detail.station_url}")
            return 0
        page = descend(speaker_ip, entry.index, port=port)

    if start_index:
        page = current_page(speaker_ip, start_index=start_index, port=port)
    _print_page(page)
    return 0


def _search_catalogue(
    speaker_ip: str,
    *,
    port: int,
    query: str,
    start_index: int,
) -> int:
    page = search(speaker_ip, query, start_index=start_index, port=port)
    if not page.entries:
        print(f"Nothing found for {query!r}")
        return 0
    # Results are mixed. Only the rows carrying a TuneIn id are stations; the
    # rest are headings the catalogue cannot descend into.
    _print_page(page)
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


def _station_candidates(station: RadioStation) -> tuple[str, ...]:
    """Return the URLs to try, freshest first.

    A saved `tunein_id` is resolved now rather than stored, because the answer
    changes whenever the broadcaster moves its endpoint - which is exactly the
    failure a hardcoded URL cannot survive. The saved URLs stay behind it as
    the static net for when TuneIn is unreachable, or answers with nothing this
    speaker can take: BBC Radio 1 resolves to HLS only, so it comes back empty
    and the fallbacks carry the station.

    Resolution failing is not fatal. It costs one entry at the front of a list
    that still has everything it had before.
    """
    if not station.tunein_id:
        return station.all_urls
    try:
        resolved = resolve_tunein_station(station.tunein_id)
    except WamApiError as error:
        LOGGER.warning(
            "Could not resolve TuneIn id %s for %s: %s",
            station.tunein_id,
            station.alias,
            error,
        )
        return station.all_urls
    if not resolved:
        LOGGER.info(
            "TuneIn offered no directly playable stream for %s (%s); "
            "using the saved URLs",
            station.alias,
            station.tunein_id,
        )
        return station.all_urls
    ordered = list(resolved)
    ordered += [url for url in station.all_urls if url not in resolved]
    return tuple(ordered)


def _play_custom_station(
    args: argparse.Namespace,
    station: RadioStation,
) -> int:
    """Try a station's primary stream and fallbacks in order."""
    failures: list[str] = []
    original_source = args.source
    candidates = _station_candidates(station)
    try:
        for index, url in enumerate(candidates, start=1):
            args.source = url
            LOGGER.info(
                "Trying radio station %s stream %s/%s: %s",
                station.alias,
                index,
                len(candidates),
                url,
            )
            try:
                with continuous_source(url):
                    result = cli.run(args)
            except (RuntimeError, StreamError, WamApiError) as error:
                failures.append(f"{url}: {error}")
                if index < len(candidates):
                    LOGGER.warning(
                        "Radio stream failed, trying fallback %s/%s",
                        index + 1,
                        len(candidates),
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
    if cli.has_control_action(args):
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
            tunein_id=(
                validate_tunein_id(args.tunein_id.strip())
                if getattr(args, "tunein_id", None)
                else None
            ),
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
    if args.tunein_browse is not None:
        return _browse_catalogue(
            speaker_ip,
            port=speaker_port,
            path=args.tunein_browse,
            start_index=args.tunein_start,
        )
    if args.tunein_search:
        return _search_catalogue(
            speaker_ip,
            port=speaker_port,
            query=args.tunein_search,
            start_index=args.tunein_start,
        )
    return _play_tunein_safely(args, speaker_ip, speaker_port)


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point with radio extensions."""
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging(args.verbose)
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
