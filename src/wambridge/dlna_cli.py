"""Standalone DLNA file playback probe for Samsung WAM speakers."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from time import sleep

from .cli import DEFAULT_MAX_START_VOLUME, choose_start_volume, volume_level
from .discovery import discover, local_ip_for
from .dlna import (
    DlnaError,
    UpnpService,
    build_mp3_metadata,
    discover_av_transport,
    get_transport_info,
    pause,
    play,
    set_transport_uri,
    stop,
)
from .dlna_server import DlnaFileServer
from .profiles import ProfileError, ProfileStore, resolve_device
from .samsung import (
    WamApiError,
    get_mute,
    get_volume,
    probe,
    set_mute,
    set_volume,
)

LOGGER = logging.getLogger("wambridge")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wambridge-dlna",
        description=(
            "Play one local MP3 through the speaker's DLNA AVTransport service."
        ),
    )
    parser.add_argument("source", type=Path, help="Local MP3 file")
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--speaker", help="Speaker IPv4 address")
    target.add_argument(
        "--device",
        help="Saved device alias; current IP is resolved automatically",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=55001,
        help="Samsung WAM API port",
    )
    parser.add_argument(
        "--volume",
        type=volume_level,
        help="Explicit startup volume from 0 to 100",
    )
    parser.add_argument(
        "--max-start-volume",
        type=volume_level,
        default=DEFAULT_MAX_START_VOLUME,
        help=(
            "Clamp current volume before playback "
            f"(default: {DEFAULT_MAX_START_VOLUME})"
        ),
    )
    parser.add_argument(
        "--bind",
        default="0.0.0.0",
        help="Local HTTP bind address",
    )
    parser.add_argument(
        "--http-port",
        type=int,
        default=0,
        help="Local HTTP port; 0 chooses one",
    )
    parser.add_argument(
        "--config",
        type=Path,
        help="Override the per-user device profile file",
    )
    parser.add_argument(
        "--discovery-timeout",
        type=float,
        default=5.0,
        help="Seconds to wait for SSDP replies",
    )
    parser.add_argument(
        "--interface",
        action="append",
        dest="interfaces",
        help="Local IPv4 used for SSDP; repeat for multiple interfaces",
    )
    parser.add_argument(
        "--no-scan",
        action="store_true",
        help="Disable the API-scan fallback while resolving a saved device",
    )
    parser.add_argument("--verbose", action="store_true")
    return parser


def select_speaker(
    args: argparse.Namespace,
    store: ProfileStore,
) -> tuple[str, int]:
    if args.device:
        profile = resolve_device(
            args.device,
            store=store,
            timeout=args.discovery_timeout,
            local_addresses=args.interfaces,
            scan=not args.no_scan,
        )
        LOGGER.info(
            "Resolved saved device %s (%s) to %s",
            profile.alias,
            profile.device_id,
            profile.last_ip,
        )
        return profile.last_ip, profile.port
    if args.speaker:
        return args.speaker, args.port

    speakers = discover(
        timeout=args.discovery_timeout,
        local_addresses=args.interfaces,
        port=args.port,
        scan=not args.no_scan,
    )
    if not speakers:
        raise RuntimeError("No Samsung WAM speaker found")
    if len(speakers) > 1:
        addresses = ", ".join(speaker.ip for speaker in speakers)
        raise RuntimeError(
            f"More than one Samsung WAM found ({addresses}); pass --speaker IP"
        )
    return speakers[0].ip, args.port


def _transport_state(service: UpnpService) -> str:
    info = get_transport_info(service)
    return info.get("CurrentTransportState", "").upper()


def _wait_for_completion(
    service: UpnpService,
    *,
    poll_interval: float = 0.75,
) -> None:
    while True:
        sleep(poll_interval)
        try:
            state = _transport_state(service)
        except DlnaError as error:
            LOGGER.debug("Cannot read AVTransport state: %s", error)
            continue
        if state == "STOPPED":
            return


def _secure_stop(
    service: UpnpService | None,
    *,
    speaker_ip: str,
    speaker_port: int,
    previous_volume: int | None,
    previous_mute: bool | None,
    speaker_touched: bool,
    transport_touched: bool,
) -> None:
    if not speaker_touched and not transport_touched:
        return

    muted = False
    try:
        set_mute(speaker_ip, True, port=speaker_port)
        muted = True
    except WamApiError as error:
        LOGGER.warning("Could not mute speaker during shutdown: %s", error)

    quiesced = not transport_touched
    if service is not None and transport_touched:
        try:
            stop(service)
            quiesced = True
        except DlnaError as stop_error:
            try:
                quiesced = _transport_state(service) == "STOPPED"
            except DlnaError:
                quiesced = False
            if not quiesced:
                try:
                    pause(service)
                except DlnaError as pause_error:
                    LOGGER.warning(
                        "AVTransport stop failed (%s); pause also failed (%s)",
                        stop_error,
                        pause_error,
                    )
                else:
                    LOGGER.warning(
                        "AVTransport stop failed; playback was paused and left muted: %s",
                        stop_error,
                    )

    if (
        quiesced
        and previous_volume is not None
        and previous_mute is not None
    ):
        try:
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
            LOGGER.warning("Could not restore speaker state: %s", error)
    elif muted:
        LOGGER.warning(
            "Speaker remains muted because AVTransport stop was not confirmed"
        )


def run(args: argparse.Namespace) -> int:
    source = args.source.expanduser().resolve()
    if not source.is_file():
        raise RuntimeError(f"Audio file does not exist: {source}")
    if source.suffix.casefold() != ".mp3":
        raise RuntimeError("Initial DLNA playback supports local MP3 files only")

    store = ProfileStore(args.config)
    speaker_ip, speaker_port = select_speaker(args, store)
    response = probe(speaker_ip, port=speaker_port)
    LOGGER.info(
        "Speaker %s replied with %s",
        speaker_ip,
        response.method or "XML",
    )

    service = discover_av_transport(
        speaker_ip,
        timeout=args.discovery_timeout,
        local_addresses=args.interfaces,
    )
    host_ip = local_ip_for(speaker_ip)
    server = DlnaFileServer(
        source,
        bind=args.bind,
        port=args.http_port,
    )

    previous_volume: int | None = None
    previous_mute: bool | None = None
    speaker_touched = False
    transport_touched = False
    try:
        previous_volume = get_volume(speaker_ip, port=speaker_port)
        previous_mute = get_mute(speaker_ip, port=speaker_port)
        start_volume = choose_start_volume(
            previous_volume,
            args.volume,
            args.max_start_volume,
        )

        server.start()
        media_url = server.url(host_ip)
        metadata = build_mp3_metadata(media_url, source)
        LOGGER.info("Offering DLNA file %s to %s", media_url, speaker_ip)

        set_volume(speaker_ip, 0, port=speaker_port)
        speaker_touched = True
        set_mute(speaker_ip, True, port=speaker_port)
        try:
            stop(service, timeout=3)
        except DlnaError:
            pass
        set_transport_uri(service, media_url, metadata)
        transport_touched = True
        play(service)

        if not server.request_started.wait(timeout=20):
            raise RuntimeError(
                "AVTransport accepted playback but the speaker did not request "
                "the MP3; check Windows Firewall"
            )

        set_volume(speaker_ip, start_volume, port=speaker_port)
        set_mute(speaker_ip, False, port=speaker_port)
        print(
            f"DLNA playback started on {speaker_ip} at volume {start_volume}. "
            "Press Ctrl+C to stop."
        )

        _wait_for_completion(service)
        return 0
    except KeyboardInterrupt:
        print("\nStopping")
        return 130
    finally:
        try:
            _secure_stop(
                service,
                speaker_ip=speaker_ip,
                speaker_port=speaker_port,
                previous_volume=previous_volume,
                previous_mute=previous_mute,
                speaker_touched=speaker_touched,
                transport_touched=transport_touched,
            )
        finally:
            server.close()


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )
    try:
        return run(args)
    except (
        DlnaError,
        ProfileError,
        RuntimeError,
        ValueError,
        WamApiError,
    ) as error:
        LOGGER.error("%s", error)
        return 1


if __name__ == "__main__":
    sys.exit(main())
