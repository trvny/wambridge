"""Command-line entry point for WAM Bridge."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .cli_common import (
    DEFAULT_MAX_START_VOLUME,
    add_target_arguments,
    bounded_int,
    configure_logging,
    find_speakers,
    select_discovered_speaker,
    select_speaker,
)
from .discovery import local_ip_for
from .profiles import (
    ProfileError,
    ProfileStore,
    remember_device,
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
    require_local_playback_mode,
    resume_playback,
    set_mute,
    set_volume,
    stop_playback,
)
from .stream import AudioStreamServer, OUTPUT_PROFILES, StreamError

LOGGER = logging.getLogger("wambridge")

volume_level = bounded_int("volume", minimum=MIN_VOLUME, maximum=MAX_VOLUME)
"""Parse a raw WAM volume level for argparse."""


RECOVERY_VOLUME = 3
"""Level used when the speaker is found silent and nobody asked for one."""


def recovers_from_silence(current_volume: int, explicit_volume: int | None) -> bool:
    """Report whether a start level is being invented rather than followed.

    Callers need this to decide what "put it back" means on an aborted startup:
    restoring a 0 that was found rather than chosen would undo the recovery.
    """
    return current_volume == 0 and explicit_volume is None


def choose_start_volume(
    current_volume: int,
    explicit_volume: int | None,
    max_start_volume: int,
) -> int:
    """Choose an explicit level or clamp the current volume to a safe maximum."""
    if explicit_volume is not None:
        return explicit_volume
    if recovers_from_silence(current_volume, explicit_volume):
        # Startup mutes the speaker and restores it once audio flows, so a
        # helper killed in between leaves it at 0. Following that reading would
        # start the next stream silent while every other signal reports playing
        # - the failure this project has already lost days to. Nobody asks for
        # playback in order to hear nothing.
        return min(RECOVERY_VOLUME, max_start_volume)
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
    add_target_arguments(parser)
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

    parser.add_argument("--verbose", action="store_true")
    return parser


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


def has_control_action(args: argparse.Namespace) -> bool:
    """Report whether any one-shot device or playback action was requested."""
    return any(
        (
            args.probe,
            args.discover,
            args.remember,
            args.list_devices,
            args.forget,
            _has_remote_action(args),
        )
    )


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

    if args.source and has_control_action(args):
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
        # Checked here, not once per session: the speaker drifts back into
        # content-provider mode on its own, and from there it fetches the
        # stream and stays silent.
        require_local_playback_mode(speaker_ip, port=speaker_port)
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
    configure_logging(args.verbose)
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
