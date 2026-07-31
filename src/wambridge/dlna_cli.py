"""Standalone Samsung WAM share-playback probe for one local MP3."""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Callable
from pathlib import Path
from time import monotonic, sleep

from .cli import DEFAULT_MAX_START_VOLUME, choose_start_volume, volume_level
from .discovery import discover, local_ip_for
from .dlna_server import DlnaFileServer
from .profiles import ProfileError, ProfileStore, resolve_device
from .samsung import (
    WamApiError,
    get_mute,
    get_play_status,
    get_volume,
    play_new_folder,
    play_share,
    probe,
    set_ip_info,
    set_mute,
    set_playback_control,
    set_volume,
)

LOGGER = logging.getLogger("wambridge")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wambridge-dlna",
        description=(
            "Play one local MP3 through Samsung WAM share playback and a tiny "
            "local UPnP MediaServer."
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
        help="Seconds to wait while resolving speakers",
    )
    parser.add_argument(
        "--interface",
        action="append",
        dest="interfaces",
        help="Local IPv4 used for discovery; repeat for multiple interfaces",
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


def _is_timeout_error(error: BaseException) -> bool:
    """Return whether a WAM failure ultimately came from a socket timeout."""

    current: BaseException | None = error
    while current is not None:
        if isinstance(current, TimeoutError):
            return True
        reason = getattr(current, "reason", None)
        if isinstance(reason, TimeoutError):
            return True
        if "timed out" in str(current).casefold():
            return True
        current = current.__cause__
    return False


def _allow_async_timeout(label: str, action: Callable[[], object]) -> None:
    """Run a WAM command whose firmware response may never be closed."""

    try:
        action()
    except WamApiError as error:
        if not _is_timeout_error(error):
            raise
        LOGGER.warning(
            "%s timed out waiting for the HTTP reply; continuing because "
            "Samsung firmware may process this command asynchronously",
            label,
        )


def _use_samsung_identity(server: DlnaFileServer) -> None:
    """Match the proprietary DMS identity generated by Samsung Multiroom."""

    if not server.uuid.startswith("samsung-"):
        server.uuid = f"samsung-{server.uuid}"
        server.udn = f"uuid:{server.uuid}"


def _start_share(
    speaker_ip: str,
    speaker_port: int,
    server: DlnaFileServer,
) -> None:
    """Send the exact Samsung command and its official rejection fallback."""

    try:
        _allow_async_timeout(
            "SetSharePlayback",
            lambda: play_share(
                speaker_ip,
                source_name="WAMBridge",
                device_udn=server.udn,
                object_id=server.object_id,
                port=speaker_port,
                timeout=3.0,
            ),
        )
    except WamApiError as error:
        LOGGER.warning(
            "SetSharePlayback was rejected (%s); trying Samsung's "
            "SetNewFolderPlayback fallback",
            error,
        )
        _allow_async_timeout(
            "SetNewFolderPlayback",
            lambda: play_new_folder(
                speaker_ip,
                device_udn=server.udn,
                object_id=server.object_id,
                port=speaker_port,
                timeout=3.0,
            ),
        )


def _wait_for_completion(
    speaker_ip: str,
    speaker_port: int,
    server: DlnaFileServer,
    *,
    poll_interval: float = 0.75,
) -> None:
    """Keep the MediaServer alive until WAM leaves DLNA or duration elapses."""

    fallback_seconds = (server.duration_ms or 0) / 1000 + 20
    fallback_deadline = monotonic() + max(60, fallback_seconds)
    seen_dlna = False

    while True:
        sleep(poll_interval)
        try:
            status = get_play_status(speaker_ip, port=speaker_port)
        except WamApiError as error:
            LOGGER.debug("Cannot read WAM playback state: %s", error)
        else:
            submode = (status.submode or "").casefold()
            play_status = (status.play_status or "").casefold()
            LOGGER.debug(
                "WAM playback state: submode=%s playstatus=%s",
                submode or "?",
                play_status or "?",
            )
            if submode == "dlna":
                seen_dlna = True
            elif seen_dlna:
                return
            if seen_dlna and play_status in {"stop", "stopped"}:
                return

        if monotonic() >= fallback_deadline:
            LOGGER.info("Playback duration elapsed; stopping local MediaServer")
            return


def _secure_stop(
    *,
    speaker_ip: str,
    speaker_port: int,
    previous_volume: int | None,
    previous_mute: bool | None,
    speaker_touched: bool,
    playback_touched: bool,
) -> None:
    if not speaker_touched and not playback_touched:
        return

    muted = False
    try:
        set_mute(speaker_ip, True, port=speaker_port)
        muted = True
    except WamApiError as error:
        LOGGER.warning("Could not mute speaker during shutdown: %s", error)

    quiesced = not playback_touched
    if playback_touched:
        try:
            set_playback_control(
                speaker_ip,
                "pause",
                api_type="UIC",
                port=speaker_port,
                timeout=3.0,
            )
            quiesced = True
        except WamApiError as error:
            LOGGER.warning("Could not stop Samsung share playback: %s", error)

    if quiesced and previous_volume is not None and previous_mute is not None:
        try:
            set_volume(speaker_ip, previous_volume, port=speaker_port)
            set_mute(speaker_ip, previous_mute, port=speaker_port)
        except WamApiError as error:
            LOGGER.warning("Could not restore speaker state: %s", error)
    elif muted:
        LOGGER.warning("Speaker remains muted because playback stop was not confirmed")


def _missing_request_error(server: DlnaFileServer) -> str:
    if not server.description_requested.is_set():
        return (
            "M5 did not contact the local MediaServer after the Samsung "
            "share command; allow the EXE through Windows Firewall or pass "
            "--interface with the LAN IPv4"
        )
    if not server.browse_requested.is_set():
        return (
            "M5 read description.xml but did not Browse the ContentDirectory; "
            "run again with --verbose and report the HTTP trace"
        )
    return (
        "M5 browsed the ContentDirectory but did not request the MP3; "
        "run again with --verbose and report the HTTP trace"
    )


def run(args: argparse.Namespace) -> int:
    source = args.source.expanduser().resolve()
    if not source.is_file():
        raise RuntimeError(f"Audio file does not exist: {source}")
    if source.suffix.casefold() != ".mp3":
        raise RuntimeError("Samsung share playback currently supports local MP3 only")

    store = ProfileStore(args.config)
    speaker_ip, speaker_port = select_speaker(args, store)
    response = probe(speaker_ip, port=speaker_port)
    LOGGER.info(
        "Speaker %s replied with %s",
        speaker_ip,
        response.method or "XML",
    )

    host_ip = args.interfaces[0] if args.interfaces else local_ip_for(speaker_ip)
    server = DlnaFileServer(source, bind=args.bind, port=args.http_port)
    _use_samsung_identity(server)

    previous_volume: int | None = None
    previous_mute: bool | None = None
    speaker_touched = False
    playback_touched = False
    try:
        previous_volume = get_volume(speaker_ip, port=speaker_port)
        previous_mute = get_mute(speaker_ip, port=speaker_port)
        start_volume = choose_start_volume(
            previous_volume,
            args.volume,
            args.max_start_volume,
        )

        server.start()
        server_address = f"{host_ip}:{server.port}"
        LOGGER.info("MediaServer description: %s", server.description_url(host_ip))
        LOGGER.info(
            "Offering object %s as %s (%s bytes, duration %s)",
            server.object_id,
            server.url(host_ip),
            server.size,
            server.duration or "unknown",
        )

        _allow_async_timeout(
            "SetIpInfo",
            lambda: set_ip_info(
                speaker_ip,
                server.uuid,
                server_address,
                port=speaker_port,
                timeout=2.0,
            ),
        )
        sleep(0.3)

        set_volume(speaker_ip, 0, port=speaker_port)
        speaker_touched = True
        set_mute(speaker_ip, True, port=speaker_port)

        _start_share(speaker_ip, speaker_port, server)

        if not server.request_started.wait(timeout=20):
            raise RuntimeError(_missing_request_error(server))
        playback_touched = True

        set_volume(speaker_ip, start_volume, port=speaker_port)
        set_mute(speaker_ip, False, port=speaker_port)
        print(
            f"Samsung share playback started on {speaker_ip} at volume "
            f"{start_volume}. Press Ctrl+C to stop."
        )

        _wait_for_completion(speaker_ip, speaker_port, server)
        return 0
    except KeyboardInterrupt:
        print("\nStopping")
        return 130
    finally:
        try:
            _secure_stop(
                speaker_ip=speaker_ip,
                speaker_port=speaker_port,
                previous_volume=previous_volume,
                previous_mute=previous_mute,
                speaker_touched=speaker_touched,
                playback_touched=playback_touched,
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
    except (ProfileError, RuntimeError, ValueError, WamApiError) as error:
        LOGGER.error("%s", error)
        return 1


if __name__ == "__main__":
    sys.exit(main())
