"""Play one local file on a Samsung WAM speaker through the share path.

Sequence measured on a physical M5:

1. serve the object at ``/DLNA/<object id>``,
2. register the client UUID and server address with ``SetIpInfo``,
3. send one ``SetSharePlaybackControl`` with that same raw UUID,
4. wait for ``StartPlaybackEvent``.

One attempt is enough once the identifier form and the path are right, so there
is deliberately no fallback ladder.
"""

from __future__ import annotations

import argparse
import logging
import socket
import threading
from pathlib import Path

from .identity import load_client_uuid
from .samsung import (
    DEFAULT_PORT,
    WamApiError,
    get_mute,
    get_volume,
    play_share,
    register_share_source,
    require_local_playback_mode,
    set_mute,
    set_volume,
)
from .share import DEFAULT_SHARE_PORT, ShareServer, UnsupportedMediaError
from .wam_events import listen_events

LOGGER = logging.getLogger(__name__)

SUCCESS_EVENT = "StartPlaybackEvent"
PROGRESS_EVENTS = ("MediaBufferStartEvent", "MediaBufferEndEvent")
FAILURE_EVENT = "ErrorEvent"


def local_ip_for(speaker_ip: str) -> str:
    """Return the local address the speaker will be able to reach."""

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.connect((speaker_ip, 9))
        return probe.getsockname()[0]


class SpeakerState:
    """Save and restore volume and mute around a playback attempt.

    Each ``touched`` flag is set *before* the command that changes the speaker,
    never after. A mutation whose response times out may still have been
    applied, so treating it as untouched can leave the speaker muted or silent.
    """

    def __init__(self, speaker_ip: str, *, port: int = DEFAULT_PORT) -> None:
        self.speaker_ip = speaker_ip
        self.port = port
        self.previous_volume: int | None = None
        self.previous_mute: bool | None = None
        self._volume_touched = False
        self._mute_touched = False

    def capture(self) -> None:
        try:
            self.previous_volume = get_volume(self.speaker_ip, port=self.port)
        except WamApiError as error:
            LOGGER.debug("Could not read speaker volume before playback: %s", error)
        try:
            self.previous_mute = get_mute(self.speaker_ip, port=self.port)
        except WamApiError as error:
            LOGGER.debug("Could not read speaker mute before playback: %s", error)

    def set_volume(self, value: int) -> None:
        self._volume_touched = True
        set_volume(self.speaker_ip, value, port=self.port)

    def set_mute(self, value: bool) -> None:
        self._mute_touched = True
        set_mute(self.speaker_ip, value, port=self.port)

    def restore(self) -> None:
        # A failed restore leaves the speaker muted or at the wrong volume, which
        # is silent to the user unless it is logged. Try both regardless of which
        # one fails.
        if self._mute_touched and self.previous_mute is not None:
            try:
                set_mute(self.speaker_ip, self.previous_mute, port=self.port)
            except WamApiError as error:
                LOGGER.warning(
                    "Could not restore speaker mute to %s: %s",
                    self.previous_mute,
                    error,
                )
        if self._volume_touched and self.previous_volume is not None:
            try:
                set_volume(self.speaker_ip, self.previous_volume, port=self.port)
            except WamApiError as error:
                LOGGER.warning(
                    "Could not restore speaker volume to %s: %s",
                    self.previous_volume,
                    error,
                )


class PlaybackWatcher:
    """Watch TCP 55001 for the events that actually confirm playback.

    ``MusicInfo`` and ``PlayStatus`` are not usable here: both were observed
    reporting a playing state with nothing playing, and mixing fields from an
    earlier session.
    """

    def __init__(self, speaker_ip: str, client_uuid: str, *, port: int) -> None:
        self.started = threading.Event()
        self.failed = threading.Event()
        self.error_code = ""
        self._speaker_ip = speaker_ip
        self._client_uuid = client_uuid
        self._port = port
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> PlaybackWatcher:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        # Commands sent before the socket is up would race with the events they
        # are supposed to produce.
        if not self._ready.wait(timeout=10.0):
            LOGGER.warning("Event listener did not become ready; events may be missed")
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()

    def _run(self) -> None:
        try:
            for event in listen_events(
                self._speaker_ip,
                self._client_uuid,
                port=self._port,
                stop=self._stop,
                ready=self._ready,
            ):
                if event.method in PROGRESS_EVENTS:
                    LOGGER.info("%s", event.method)
                elif event.method == SUCCESS_EVENT:
                    self.started.set()
                    return
                elif event.method == FAILURE_EVENT:
                    self.error_code = event.values.get("errCode") or event.values.get(
                        "errcode", ""
                    )
                    self.failed.set()
                    return
        except Exception as error:  # noqa: BLE001 - must not kill playback
            # Never silent: without this the caller only sees "no event arrived"
            # and cannot tell a stalled speaker from a broken listener.
            self.error_code = f"listener failed: {error}"
            LOGGER.warning("Event listener stopped: %s", error)
            self.failed.set()


def start_share_playback(
    speaker_ip: str,
    media_path: Path,
    *,
    speaker_port: int = DEFAULT_PORT,
    share_port: int = DEFAULT_SHARE_PORT,
    volume: int | None = None,
    timeout: float = 20.0,
) -> ShareServer:
    """Start playback and return the running server once the speaker confirms."""

    require_local_playback_mode(speaker_ip, port=speaker_port)
    client_uuid = load_client_uuid()
    host_ip = local_ip_for(speaker_ip)
    server = ShareServer(media_path, port=share_port)
    state = SpeakerState(speaker_ip, port=speaker_port)
    state.capture()

    try:
        server.start()
        LOGGER.info("Offering %s (%s bytes)", server.url(host_ip), server.size)
        with PlaybackWatcher(speaker_ip, client_uuid, port=speaker_port) as watcher:
            register_share_source(
                speaker_ip,
                client_uuid,
                f"{host_ip}:{server.port}",
                port=speaker_port,
            )
            if volume is not None:
                state.set_volume(volume)
            play_share(
                speaker_ip,
                device_udn=client_uuid,
                object_id=server.object_id,
                port=speaker_port,
            )

            if watcher.started.wait(timeout=timeout):
                return server
            if watcher.failed.is_set():
                raise WamApiError(
                    f"Speaker reported {FAILURE_EVENT} {watcher.error_code}".strip()
                )
            if not server.requested.is_set():
                raise WamApiError(
                    "Speaker never fetched the object; it did not accept the command"
                )
            raise WamApiError(
                f"Speaker fetched the object but no {SUCCESS_EVENT} arrived"
            )
    except BaseException:
        state.restore()
        server.close()
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wambridge-share",
        description="Play a local audio file on a Samsung WAM speaker.",
    )
    parser.add_argument("speaker", help="Speaker IP address")
    parser.add_argument("media", type=Path, help="Audio file to play")
    parser.add_argument("--speaker-port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--share-port", type=int, default=DEFAULT_SHARE_PORT)
    parser.add_argument(
        "--volume",
        type=int,
        help="Raw speaker step 0..30; left untouched when omitted",
    )
    parser.add_argument("--timeout", type=float, default=20.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = build_parser().parse_args(argv)
    if args.volume is not None and not 0 <= args.volume <= 30:
        print("Volume must be a raw speaker step between 0 and 30.")
        return 2
    try:
        server = start_share_playback(
            args.speaker,
            args.media,
            speaker_port=args.speaker_port,
            share_port=args.share_port,
            volume=args.volume,
            timeout=args.timeout,
        )
    except (FileNotFoundError, UnsupportedMediaError, ValueError, WamApiError) as error:
        print(f"{error}")
        return 1

    print(f"Playing {args.media.name}. Press Ctrl+C to stop.")
    try:
        while True:
            server.requested.wait(timeout=1.0)
    except KeyboardInterrupt:
        print("\nStopping")
    finally:
        server.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
