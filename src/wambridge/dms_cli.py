"""One-shot Samsung WAM DMS playback diagnostic for a local MP3."""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Callable
from pathlib import Path
from time import monotonic, sleep

from .cli import DEFAULT_MAX_START_VOLUME, choose_start_volume, volume_level
from .discovery import discover, local_ip_for
from .dms_probe import (
    DEFAULT_DMS_PORT,
    SamsungDmsServer,
    SsdpAdvertiser,
    play_new_folder_control_apk,
    play_share_control_apk,
    set_ip_info_apk,
)
from .profiles import ProfileError, ProfileStore, resolve_device
from .samsung import (
    WamApiError,
    get_mute,
    get_play_status,
    get_volume,
    probe,
    set_mute,
    set_playback_control,
    set_volume,
)

LOGGER = logging.getLogger("wambridge")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wambridge-dlna",
        description=(
            "Diagnose Samsung WAM share playback using a local UPnP "
            "MediaServer, SSDP and the literal Multiroom APK commands."
        ),
    )
    parser.add_argument("source", type=Path, help="Local MP3 file")
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--speaker", help="Speaker IPv4 address")
    target.add_argument("--device", help="Saved device alias")
    parser.add_argument("--port", type=int, default=55001, help="Samsung WAM API port")
    parser.add_argument("--volume", type=volume_level, help="Startup volume from 0 to 100")
    parser.add_argument(
        "--max-start-volume",
        type=volume_level,
        default=DEFAULT_MAX_START_VOLUME,
        help=f"Clamp current volume (default: {DEFAULT_MAX_START_VOLUME})",
    )
    parser.add_argument(
        "--bind",
        default="0.0.0.0",
        help="Local HTTP bind address",
    )
    parser.add_argument(
        "--http-port",
        type=int,
        default=DEFAULT_DMS_PORT,
        help=f"Local DMS HTTP port (default: {DEFAULT_DMS_PORT})",
    )
    parser.add_argument("--config", type=Path, help="Override device profile file")
    parser.add_argument(
        "--discovery-timeout",
        type=float,
        default=5.0,
        help="Seconds to resolve speakers",
    )
    parser.add_argument(
        "--interface",
        action="append",
        dest="interfaces",
        help="Local IPv4 used for discovery; repeat when needed",
    )
    parser.add_argument(
        "--no-scan",
        action="store_true",
        help="Disable API-scan fallback",
    )
    parser.add_argument("--verbose", action="store_true")
    return parser


def select_speaker(args: argparse.Namespace, store: ProfileStore) -> tuple[str, int]:
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


def _run_wam_command(label: str, action: Callable[[], object]) -> bool:
    """Run a command, tolerating the firmware's unclosed async responses."""

    try:
        action()
    except WamApiError as error:
        if _is_timeout_error(error):
            LOGGER.warning(
                "%s timed out waiting for the HTTP reply; continuing because "
                "the firmware may process it asynchronously",
                label,
            )
            return True
        LOGGER.warning("%s was rejected: %s", label, error)
        return False
    return True


def _wait_for_contact(server: SamsungDmsServer, timeout: float) -> bool:
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        if server.has_contact:
            return True
        sleep(0.1)
    return server.has_contact


def _register_server(
    speaker_ip: str,
    speaker_port: int,
    server: SamsungDmsServer,
    host_ip: str,
) -> bool:
    address = f"{host_ip}:{server.port}"
    return _run_wam_command(
        "SetIpInfo",
        lambda: set_ip_info_apk(
            speaker_ip,
            server.uuid,
            address,
            port=speaker_port,
        ),
    )


def _start_share(
    speaker_ip: str,
    speaker_port: int,
    server: SamsungDmsServer,
    device_udn: str,
) -> bool:
    return _run_wam_command(
        "SetSharePlaybackControl",
        lambda: play_share_control_apk(
            speaker_ip,
            source_name="WAMBridge",
            device_udn=device_udn,
            object_id=server.object_id,
            port=speaker_port,
        ),
    )


def _start_folder_fallback(
    speaker_ip: str,
    speaker_port: int,
    server: SamsungDmsServer,
) -> bool:
    return _run_wam_command(
        "SetNewFolderPlaybackControl",
        lambda: play_new_folder_control_apk(
            speaker_ip,
            source_name="WAMBridge",
            device_udn=server.udn,
            object_id=server.object_id,
            parent_id="0",
            play_index=0,
            playtime=0,
            port=speaker_port,
        ),
    )


def _run_ladder(
    speaker_ip: str,
    speaker_port: int,
    server: SamsungDmsServer,
    host_ip: str,
    ssdp: SsdpAdvertiser,
) -> bool:
    """Try all useful APK-compatible variants in one process."""

    LOGGER.info("Attempt 1/3: SetIpInfo + SSDP + SetSharePlaybackControl")
    _register_server(speaker_ip, speaker_port, server, host_ip)
    ssdp.announce()
    accepted = _start_share(speaker_ip, speaker_port, server, server.udn)
    if _wait_for_contact(server, 6.0):
        return accepted

    LOGGER.warning(
        "No MediaServer contact after share control; trying "
        "SetNewFolderPlaybackControl"
    )
    LOGGER.info("Attempt 2/3: APK folder fallback")
    ssdp.announce()
    accepted = _start_folder_fallback(speaker_ip, speaker_port, server) or accepted
    if _wait_for_contact(server, 6.0):
        return accepted

    LOGGER.warning(
        "No MediaServer contact after folder fallback; repeating SSDP, "
        "SetIpInfo and share with the raw UUID compatibility form"
    )
    LOGGER.info("Attempt 3/3: re-register and retry raw UUID")
    ssdp.announce(repeats=3)
    _register_server(speaker_ip, speaker_port, server, host_ip)
    sleep(0.5)
    accepted = _start_share(speaker_ip, speaker_port, server, server.uuid) or accepted
    _wait_for_contact(server, 8.0)
    return accepted


def _wait_for_completion(
    speaker_ip: str,
    speaker_port: int,
    server: SamsungDmsServer,
    *,
    poll_interval: float = 0.75,
) -> None:
    fallback_seconds = (server.duration_ms or 0) / 1000 + 20
    deadline = monotonic() + max(60, fallback_seconds)
    seen_dlna = False
    while monotonic() < deadline:
        sleep(poll_interval)
        try:
            status = get_play_status(speaker_ip, port=speaker_port)
        except WamApiError as error:
            LOGGER.debug("Cannot read WAM playback state: %s", error)
            continue
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
    LOGGER.info("Playback duration elapsed; stopping local MediaServer")


def _restore_speaker(
    *,
    speaker_ip: str,
    speaker_port: int,
    previous_volume: int | None,
    previous_mute: bool | None,
    speaker_touched: bool,
    playback_touched: bool,
    source_closed: bool,
) -> None:
    if not speaker_touched and not playback_touched:
        return
    muted = False
    try:
        set_mute(speaker_ip, True, port=speaker_port)
        muted = True
    except WamApiError as error:
        LOGGER.warning("Could not mute speaker during shutdown: %s", error)

    quiesced = not playback_touched or source_closed
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


def _failure_message(server: SamsungDmsServer) -> str:
    if not server.description_requested.is_set():
        return (
            "M5 made no HTTP contact after fixed port 3921, SSDP alive, "
            "SetIpInfo, SetSharePlaybackControl and the folder fallback. "
            "Report the complete verbose log; this now points to DMS identity "
            "or firmware behavior rather than the MP3 endpoint."
        )
    if not server.browse_requested.is_set():
        return "M5 read description.xml but did not Browse ContentDirectory."
    return "M5 browsed ContentDirectory but did not request the MP3."


def run(args: argparse.Namespace) -> int:
    source = args.source.expanduser().resolve()
    if not source.is_file():
        raise RuntimeError(f"Audio file does not exist: {source}")
    if source.suffix.casefold() != ".mp3":
        raise RuntimeError("Samsung share playback diagnostic supports local MP3 only")

    store = ProfileStore(args.config)
    speaker_ip, speaker_port = select_speaker(args, store)
    response = probe(speaker_ip, port=speaker_port)
    LOGGER.info("Speaker %s replied with %s", speaker_ip, response.method or "XML")
    host_ip = local_ip_for(speaker_ip)

    try:
        server = SamsungDmsServer(source, bind=args.bind, port=args.http_port)
    except OSError as error:
        if args.http_port == DEFAULT_DMS_PORT:
            raise RuntimeError(
                "Cannot bind Samsung DMS port 3921; close the process using it "
                "or pass --http-port with another free port"
            ) from error
        raise

    ssdp = SsdpAdvertiser(
        host_ip=host_ip,
        location=server.description_url(host_ip),
        udn=server.udn,
    )
    previous_volume: int | None = None
    previous_mute: bool | None = None
    speaker_touched = False
    playback_touched = False
    source_closed = False
    try:
        previous_volume = get_volume(speaker_ip, port=speaker_port)
        previous_mute = get_mute(speaker_ip, port=speaker_port)
        start_volume = choose_start_volume(
            previous_volume,
            args.volume,
            args.max_start_volume,
        )
        server.start()
        ssdp.start()
        LOGGER.info("MediaServer description: %s", server.description_url(host_ip))
        LOGGER.info("DMS identity: uuid=%s udn=%s", server.uuid, server.udn)
        LOGGER.info(
            "Offering object %s as %s (%s bytes, duration %s)",
            server.object_id,
            server.url(host_ip),
            server.size,
            server.duration or "unknown",
        )

        set_volume(speaker_ip, 0, port=speaker_port)
        speaker_touched = True
        set_mute(speaker_ip, True, port=speaker_port)

        playback_touched = _run_ladder(
            speaker_ip,
            speaker_port,
            server,
            host_ip,
            ssdp,
        )
        if not server.has_contact:
            raise RuntimeError(_failure_message(server))
        if not server.request_started.wait(timeout=12.0):
            raise RuntimeError(_failure_message(server))

        set_volume(speaker_ip, start_volume, port=speaker_port)
        set_mute(speaker_ip, False, port=speaker_port)
        print(
            f"Samsung DMS playback started on {speaker_ip} at volume "
            f"{start_volume}. Press Ctrl+C to stop."
        )
        _wait_for_completion(speaker_ip, speaker_port, server)
        return 0
    except KeyboardInterrupt:
        print("\nStopping")
        return 130
    finally:
        ssdp.close()
        source_closed = playback_touched and not server.request_started.is_set()
        if source_closed:
            server.close()
        try:
            _restore_speaker(
                speaker_ip=speaker_ip,
                speaker_port=speaker_port,
                previous_volume=previous_volume,
                previous_mute=previous_mute,
                speaker_touched=speaker_touched,
                playback_touched=playback_touched,
                source_closed=source_closed,
            )
        finally:
            if not source_closed:
                server.close()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )
    try:
        return run(args)
    except (OSError, ProfileError, RuntimeError, ValueError, WamApiError) as error:
        LOGGER.error("%s", error)
        return 1


if __name__ == "__main__":
    sys.exit(main())
