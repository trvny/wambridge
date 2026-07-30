"""Command-line entry point for WAM Bridge."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .discovery import DiscoveredSpeaker, discover, local_ip_for
from .profiles import (
    ProfileError,
    ProfileStore,
    remember_device,
    resolve_device,
)
from .samsung import (
    MAX_VOLUME,
    MIN_VOLUME,
    WamApiError,
    get_status,
    get_volume,
    pause_playback,
    play_url,
    probe,
    resume_playback,
    set_mute,
    set_volume,
    stop_playback,
)
from .stream import AudioStreamServer, OUTPUT_PROFILES, StreamError

LOGGER = logging.getLogger("wambridge")
DEFAULT_MAX_START_VOLUME = 10


def volume_level(value: str) -> int:
    """Parse a raw WAM volume level for argparse."""
    try:
        level = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "volume must be an integer from 0 to 100"
        ) from error
    if not MIN_VOLUME <= level <= MAX_VOLUME:
        raise argparse.ArgumentTypeError(
            f"volume must be between {MIN_VOLUME} and {MAX_VOLUME}"
        )
    return level


def choose_start_volume(
    current_volume: int,
    explicit_volume: int | None,
    max_start_volume: int,
) -> int:
    """Choose an explicit level or clamp the current volume to a safe maximum."""
    if explicit_volume is not None:
        return explicit_volume
    return min(current_volume, max_start_volume)


def build_parser() -> argparse.ArgumentParser:
    """Create the CLI parser."""
    parser = argparse.ArgumentParser(
        prog="wambridge",
        description=(
            "Stream and control a Samsung WAM speaker over the local network."
        ),
    )
    parser.add_argument(
        "source",
        nargs="?",
        help="Audio file, radio URL or other FFmpeg input",
    )
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
        "--format",
        choices=sorted(OUTPUT_PROFILES),
        default="flac",
        help="Format sent to the speaker (input may be Opus, Ogg, AAC and more)",
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
            "Clamp the current speaker volume before playback "
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
        "--ffmpeg",
        default="ffmpeg",
        help="FFmpeg executable or path",
    )

    action = parser.add_mutually_exclusive_group()
    action.add_argument(
        "--probe",
        action="store_true",
        help="Only test the selected speaker API",
    )
    action.add_argument(
        "--discover",
        action="store_true",
        help="List discovered speakers and exit",
    )
    action.add_argument(
        "--remember",
        metavar="ALIAS",
        help="Save a speaker by stable device ID",
    )
    action.add_argument(
        "--list-devices",
        action="store_true",
        help="List saved device profiles",
    )
    action.add_argument(
        "--forget",
        metavar="ALIAS",
        help="Delete a saved device profile",
    )
    action.add_argument(
        "--status",
        action="store_true",
        help="Show current source, playback, volume and mute state",
    )
    action.add_argument(
        "--set-volume",
        type=volume_level,
        metavar="LEVEL",
        help="Set speaker volume and exit",
    )
    action.add_argument(
        "--mute",
        action="store_true",
        help="Mute the speaker and exit",
    )
    action.add_argument(
        "--unmute",
        action="store_true",
        help="Unmute the speaker and exit",
    )
    action.add_argument(
        "--pause",
        action="store_true",
        help="Pause current playback",
    )
    action.add_argument(
        "--play",
        "--resume",
        dest="play",
        action="store_true",
        help="Resume TuneIn or DLNA playback",
    )
    action.add_argument(
        "--stop",
        action="store_true",
        help="Stop TuneIn or safely quiesce URL playback",
    )
    action.add_argument(
        "--standby",
        action="store_true",
        help="Stop playback and mute so the speaker can enter standby",
    )

    parser.add_argument(
        "--config",
        type=Path,
        help="Override the per-user device profile file",
    )
    parser.add_argument(
        "--discovery-timeout",
        type=float,
        default=4.0,
        help="Seconds to wait for SSDP replies before the API-scan fallback",
    )
    parser.add_argument(
        "--interface",
        action="append",
        dest="interfaces",
        help="Local IPv4 used for SSDP; repeat to try multiple interfaces",
    )
    parser.add_argument(
        "--no-scan",
        action="store_true",
        help="Disable fallback scanning of local /24 networks on port 55001",
    )
    parser.add_argument("--verbose", action="store_true")
    return parser


def find_speakers(args: argparse.Namespace) -> list[DiscoveredSpeaker]:
    """Run discovery using CLI diagnostics and fallback settings."""
    return discover(
        timeout=args.discovery_timeout,
        local_addresses=args.interfaces,
        port=args.port,
        scan=not args.no_scan,
    )


def select_discovered_speaker(args: argparse.Namespace) -> str:
    """Discover exactly one speaker."""
    speakers = find_speakers(args)
    if not speakers:
        raise RuntimeError("No Samsung WAM speaker found; pass --speaker IP")
    if len(speakers) > 1:
        addresses = ", ".join(speaker.ip for speaker in speakers)
        raise RuntimeError(
            f"More than one Samsung WAM found ({addresses}); pass --speaker IP"
        )
    return speakers[0].ip


def select_speaker(
    args: argparse.Namespace,
    store: ProfileStore,
) -> tuple[str, int]:
    """Select a direct address, resolve a saved profile or discover one speaker."""
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
    return select_discovered_speaker(args), args.port


def normalize_source(source: str) -> str:
    """Resolve existing local files while preserving URLs and FFmpeg inputs."""
    path = Path(source).expanduser()
    return str(path.resolve()) if path.exists() else source


def _print_saved_devices(store: ProfileStore) -> int:
    profiles = store.all()
    if not profiles:
        print("No saved Samsung WAM devices")
        return 0
    for profile in profiles:
        print(
            f"{profile.alias}\t{profile.last_ip}:{profile.port}\t"
            f"{profile.device_id}\t{profile.name}"
        )
    return 0


def _has_remote_action(args: argparse.Namespace) -> bool:
    return any(
        (
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


def _print_status(
    speaker_ip: str,
    *,
    port: int,
) -> int:
    status = get_status(speaker_ip, port=port)
    playback = status.playback
    source = "/".join(
        value for value in (playback.function, playback.submode) if value
    )
    print(f"address={speaker_ip}:{port}")
    print(f"power={status.power_status or 'unknown'}")
    print(f"source={source or 'unknown'}")
    print(f"provider={playback.cp_name or '-'}")
    print(f"playback={playback.play_status or 'unknown'}")
    print(f"volume={status.volume}")
    print(f"muted={'on' if status.muted else 'off'}")
    if playback.title:
        print(f"title={playback.title}")
    if playback.description:
        print(f"description={playback.description}")
    return 0


def _run_remote_action(
    args: argparse.Namespace,
    speaker_ip: str,
    speaker_port: int,
) -> int | None:
    if args.status:
        return _print_status(speaker_ip, port=speaker_port)
    if args.set_volume is not None:
        set_volume(speaker_ip, args.set_volume, port=speaker_port)
        print(f"Samsung WAM volume set to {args.set_volume}")
        return 0
    if args.mute:
        set_mute(speaker_ip, True, port=speaker_port)
        print("Samsung WAM muted")
        return 0
    if args.unmute:
        set_mute(speaker_ip, False, port=speaker_port)
        print("Samsung WAM unmuted")
        return 0
    if args.pause:
        pause_playback(speaker_ip, port=speaker_port)
        print("Samsung WAM playback paused")
        return 0
    if args.play:
        resume_playback(speaker_ip, port=speaker_port)
        print("Samsung WAM playback resumed")
        return 0
    if args.stop:
        stop_playback(speaker_ip, port=speaker_port)
        print("Samsung WAM playback stopped")
        return 0
    if args.standby:
        stop_playback(
            speaker_ip,
            standby=True,
            port=speaker_port,
        )
        print("Samsung WAM stopped and muted for standby")
        return 0
    return None


def run(args: argparse.Namespace) -> int:
    """Execute one bridge session."""
    store = ProfileStore(args.config)

    if args.source and (
        args.probe
        or args.discover
        or args.remember
        or args.list_devices
        or args.forget
        or _has_remote_action(args)
    ):
        raise RuntimeError(
            "Audio source cannot be combined with a one-shot control action"
        )

    if args.list_devices:
        return _print_saved_devices(store)

    if args.forget:
        removed = store.remove(args.forget)
        print(f"Forgot Samsung WAM device {removed.alias}")
        return 0

    if args.discover:
        speakers = find_speakers(args)
        if not speakers:
            print("No Samsung WAM speakers found")
            return 1
        for speaker in speakers:
            print(f"{speaker.ip}\t{speaker.source}\t{speaker.usn or '-'}")
        return 0

    if args.remember:
        if args.device:
            raise ProfileError("--remember cannot be combined with --device")
        speaker_ip = args.speaker or select_discovered_speaker(args)
        profile = remember_device(
            args.remember,
            speaker_ip,
            port=args.port,
            store=store,
        )
        print(
            f"Saved {profile.alias}: {profile.name} at "
            f"{profile.last_ip}:{profile.port} (device {profile.device_id})"
        )
        return 0

    speaker_ip, speaker_port = select_speaker(args, store)
    response = probe(speaker_ip, port=speaker_port)
    LOGGER.info(
        "Speaker %s replied with %s",
        speaker_ip,
        response.method or "XML",
    )

    if args.probe:
        print(f"Samsung WAM reachable at {speaker_ip}:{speaker_port}")
        return 0

    remote_result = _run_remote_action(
        args,
        speaker_ip,
        speaker_port,
    )
    if remote_result is not None:
        return remote_result

    if not args.source:
        raise RuntimeError(
            "Provide a file or stream URL, or choose a control action"
        )

    host_ip = local_ip_for(speaker_ip)
    server = AudioStreamServer(
        normalize_source(args.source),
        profile=args.format,
        bind=args.bind,
        port=args.http_port,
        ffmpeg=args.ffmpeg,
    )
    restore_volume: int | None = None
    startup_complete = False
    try:
        server.prepare()
        current_volume = get_volume(speaker_ip, port=speaker_port)
        restore_volume = current_volume
        start_volume = choose_start_volume(
            current_volume,
            args.volume,
            args.max_start_volume,
        )
        LOGGER.info(
            "Speaker volume is %s; starting playback at %s",
            current_volume,
            start_volume,
        )

        server.start()
        stream_url = server.url(host_ip)
        LOGGER.info("Offering %s to %s", stream_url, speaker_ip)

        # Keep the speaker at zero while URL playback wakes its decoder.
        # The stream begins with silence; only then is the target level applied.
        set_volume(speaker_ip, 0, port=speaker_port)
        play_url(speaker_ip, stream_url, port=speaker_port)
        set_volume(speaker_ip, 0, port=speaker_port)

        if not server.request_started.wait(timeout=15):
            raise RuntimeError(
                "Speaker accepted the command but did not request the stream; "
                "check Windows Firewall"
            )
        set_volume(speaker_ip, 0, port=speaker_port)
        server.release_audio()
        if not server.audio_started.wait(timeout=30):
            raise RuntimeError(
                "Speaker connected but audio encoding did not start"
            )
        set_volume(speaker_ip, start_volume, port=speaker_port)
        startup_complete = True

        print(
            f"Streaming to Samsung WAM at {speaker_ip} with volume "
            f"{start_volume}. Press Ctrl+C to stop."
        )
        while not server.request_finished.wait(timeout=1):
            pass
        if server.error:
            raise RuntimeError(server.error)
        return 0
    except KeyboardInterrupt:
        print("\nStopping")
        return 130
    finally:
        try:
            server.close()
        finally:
            if restore_volume is not None and not startup_complete:
                try:
                    set_volume(
                        speaker_ip,
                        restore_volume,
                        port=speaker_port,
                    )
                    LOGGER.info(
                        "Restored speaker volume to %s after aborted startup",
                        restore_volume,
                    )
                except WamApiError as error:
                    LOGGER.warning(
                        "Could not restore speaker volume after aborted "
                        "startup: %s",
                        error,
                    )


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point."""
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
        ValueError,
    ) as error:
        LOGGER.error("%s", error)
        return 1


if __name__ == "__main__":
    sys.exit(main())
