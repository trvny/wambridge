"""Process protocol for streaming foobar2000 PCM to Samsung WAM."""

from __future__ import annotations

import argparse
import logging
import sys
import threading
from collections.abc import Iterator
from time import monotonic
from typing import BinaryIO, TextIO

from .cli import choose_start_volume, recovers_from_silence, volume_level
from .cli_common import (
    DEFAULT_MAX_START_VOLUME,
    add_target_arguments,
    bounded_int,
    configure_logging,
    select_speaker,
)
from .discovery import local_ip_for
from .identity import load_client_uuid
from .pcm_stream import PCM_FORMATS, PcmAudioStreamServer
from .profiles import ProfileError, ProfileStore
from .samsung import WamApiError, get_volume, methods_agree, probe, set_volume
from .stream import OUTPUT_PROFILES, STARTUP_SILENCE_MS, StreamError
from .wam_events import WamEvent, WamEventConnection, WamEventError

LOGGER = logging.getLogger("wambridge")
_BROKEN_PIPE_ERRORS = {109, 233}
_SUCCESS_EVENT = "StartPlaybackEvent"
_FAILURE_EVENT = "ErrorEvent"
_PUBLIC_IDENTIFIER = "public"
_PLAYBACK_COMMAND = "SetUrlPlayback"
_VOLUME_COMMAND = "SetVolume"
_VOLUME_ACK_TIMEOUT = 1.0


sample_rate = bounded_int("sample rate", minimum=1)
"""Parse a PCM sample rate."""

channel_count = bounded_int("channels", minimum=1)
"""Parse a PCM channel count."""

startup_silence = bounded_int("startup silence in ms", minimum=0, maximum=10000)
"""Parse the leading silence in milliseconds."""


def build_parser() -> argparse.ArgumentParser:
    """Create the helper protocol parser."""
    parser = argparse.ArgumentParser(
        prog="wambridge-pcm",
        description="Read raw PCM from stdin and stream it to Samsung WAM.",
    )
    add_target_arguments(parser)
    parser.add_argument(
        "--sample-rate",
        type=sample_rate,
        required=True,
        help="Input PCM sample rate in Hz",
    )
    parser.add_argument(
        "--channels",
        type=channel_count,
        required=True,
        help="Input PCM channel count",
    )
    parser.add_argument(
        "--sample-format",
        choices=PCM_FORMATS,
        default="f32le",
        help="Input PCM sample format",
    )
    parser.add_argument(
        "--format",
        choices=sorted(OUTPUT_PROFILES),
        default="flac",
        help="Format sent to the speaker",
    )
    parser.add_argument("--volume", type=volume_level)
    parser.add_argument(
        "--max-start-volume",
        type=volume_level,
        default=DEFAULT_MAX_START_VOLUME,
    )
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--http-port", type=int, default=0)
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument(
        "--startup-timeout",
        type=float,
        default=30.0,
        help="Seconds to wait for the speaker and first PCM frame",
    )
    parser.add_argument(
        "--startup-silence",
        type=startup_silence,
        default=STARTUP_SILENCE_MS,
        help=(
            "Milliseconds of silence prepended to the stream; 0 disables the "
            "filter entirely. Every millisecond here is a millisecond of delay"
        ),
    )
    parser.add_argument("--verbose", action="store_true")
    return parser


def _pcm_input_closed(stream: BinaryIO) -> bool:
    """Return whether a Windows pipe writer closed without consuming PCM."""
    if sys.platform != "win32":
        return False

    try:
        import ctypes
        import msvcrt
        from ctypes import wintypes

        handle = msvcrt.get_osfhandle(stream.fileno())
    except (AttributeError, OSError, ValueError):
        return False

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    peek_named_pipe = kernel32.PeekNamedPipe
    peek_named_pipe.argtypes = [
        wintypes.HANDLE,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPVOID,
    ]
    peek_named_pipe.restype = wintypes.BOOL
    available = wintypes.DWORD()
    if peek_named_pipe(
        handle,
        None,
        0,
        None,
        ctypes.byref(available),
        None,
    ):
        return False
    return ctypes.get_last_error() in _BROKEN_PIPE_ERRORS


def _raise_if_pcm_input_closed(stream: BinaryIO) -> None:
    if _pcm_input_closed(stream):
        raise StreamError(
            "PCM input closed before the speaker requested the stream"
        )


def _wait_slices(timeout: float, *, slice_seconds: float = 0.1) -> Iterator[float]:
    """Yield short wait budgets until the timeout expires.

    Callers poll their own abort conditions between slices, so no wait may
    swallow the remaining budget in one blocking call.
    """
    deadline = monotonic() + timeout
    while (remaining := deadline - monotonic()) > 0:
        yield min(slice_seconds, remaining)


def _wait_for_stream_request(
    server: PcmAudioStreamServer,
    pcm_input: BinaryIO,
    *,
    timeout: float,
    watcher: PlaybackWatcher | None = None,
) -> None:
    for budget in _wait_slices(timeout):
        if server.request_started.wait(timeout=budget):
            return
        _raise_if_pcm_input_closed(pcm_input)
        if watcher is not None:
            watcher.raise_if_failed()
        if server.request_finished.is_set():
            raise StreamError(
                server.error
                or "PCM stream ended before the speaker requested it"
            )
    raise StreamError(
        "Speaker accepted URL playback but did not request the PCM stream"
    )


def _wait_for_stream_event(
    server: PcmAudioStreamServer,
    event_name: str,
    *,
    timeout: float,
) -> None:
    event = getattr(server, event_name)
    for budget in _wait_slices(timeout):
        if event.wait(timeout=budget):
            return
        if server.request_finished.is_set():
            raise StreamError(
                server.error or f"PCM stream ended before {event_name}"
            )
    raise StreamError(f"Timed out waiting for {event_name}")


class PlaybackWatcher:
    """Send playback and wait for its speaker events on one TCP connection."""

    def __init__(self, speaker_ip: str, client_uuid: str, *, port: int) -> None:
        self._speaker_ip = speaker_ip
        self._client_uuid = client_uuid.casefold()
        self._port = port
        self._armed = threading.Event()
        self._stream_active = threading.Event()
        self._started = threading.Event()
        self._startup_complete = threading.Event()
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._error = ""
        self._thread: threading.Thread | None = None
        self._connection: WamEventConnection | None = None
        self._connection_lock = threading.Lock()
        self._pending: list[str] = []
        self._results: dict[str, str] = {}
        self._response_lock = threading.Lock()

    def __enter__(self) -> PlaybackWatcher:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=10.0):
            self._stop.set()
            self._thread.join()
            raise StreamError("Event listener did not become ready")
        if self._error:
            raise StreamError(self._error)
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()

    def arm(self) -> None:
        """Accept playback events only after this attempt sends its command."""
        self._armed.set()

    def mark_stream_active(self) -> None:
        """Mark that the speaker requested this attempt's local HTTP stream."""
        self._stream_active.set()

    def mark_startup_complete(self) -> None:
        """Keep later listener transport failures diagnostic only."""
        self._startup_complete.set()

    def offer_stream(self, stream_url: str) -> None:
        """Send SetUrlPlayback through the connection that receives its events."""
        self._send_command(
            method="SetUrlPlayback",
            arguments=[
                ("url", stream_url, "cdata"),
                ("buffersize", 0, "dec"),
                ("seektime", 0, "dec"),
                ("resume", 0, "dec"),
            ],
        )

    def set_volume(self, level: int) -> None:
        """Set startup volume without opening a competing control socket."""
        self._send_command(
            method="SetVolume",
            arguments=[("volume", level, "dec")],
            power_on=True,
        )

    def wait_for_start(self, *, timeout: float) -> None:
        for budget in _wait_slices(timeout):
            if self._started.wait(timeout=budget):
                return
            self.raise_if_failed()
        raise StreamError(f"Speaker did not confirm {_SUCCESS_EVENT}")

    def raise_if_failed(self) -> None:
        """Surface an asynchronous control-channel failure."""
        if self._error:
            raise StreamError(self._error)

    def wait_for_response(self, method: str, *, timeout: float) -> str | None:
        """Return the rejection message for the last ``method``, if any.

        A silent speaker is not a failure (AGENTS.md): an unanswered command
        times out to ``None`` exactly like an accepted one. Only a response
        matched to this command and carrying a non-``ok`` result reports back.
        """
        deadline = monotonic() + timeout
        while True:
            with self._response_lock:
                if method in self._results:
                    return self._results[method] or None
            if monotonic() >= deadline or self._stop.wait(timeout=0.05):
                return None

    def _send_command(
        self,
        *,
        method: str,
        arguments: list[tuple[str, str | int, str]] | None = None,
        power_on: bool = False,
    ) -> None:
        with self._connection_lock:
            connection = self._connection
        if connection is None:
            raise StreamError("WAM control connection is not ready")
        with self._response_lock:
            self._results.pop(method, None)
            self._pending.append(method)
        try:
            connection.send(
                method=method,
                arguments=arguments,
                power_on=power_on,
            )
        except (OSError, WamEventError) as error:
            raise WamApiError(
                f"Cannot reach Samsung WAM at "
                f"{self._speaker_ip}:{self._port}: {error}"
            ) from error

    def _match_pending(self, event: WamEvent) -> str | None:
        """Return the command this response answers, if it answers one.

        Samsung replies with whatever it is broadcasting, so a body only counts
        as an answer when it carries a result and its method agrees with a
        command still waiting for one. Everything else stays a diagnostic.
        """
        if not event.method or event.result is None:
            return None
        with self._response_lock:
            for index, command in enumerate(self._pending):
                if methods_agree(command, event.method):
                    del self._pending[index]
                    return command
        return None

    def _record_response(self, command: str, event: WamEvent) -> None:
        if (event.result or "").casefold() == "ok":
            with self._response_lock:
                self._results[command] = ""
            return

        code = event.reported_error_code
        suffix = f" (error {code})" if code else ""
        message = f"Speaker rejected {command}{suffix}"
        with self._response_lock:
            self._results[command] = message
        if command == _PLAYBACK_COMMAND:
            self._error = message
            return
        LOGGER.warning("%s; startup checks decide whether that is fatal", message)

    def _belongs_to_attempt(self, event: WamEvent) -> bool:
        if (
            event.method != _SUCCESS_EVENT
            or not self._armed.is_set()
            or not event.user_identifier
        ):
            return False
        identifier = event.user_identifier.casefold()
        return identifier in {self._client_uuid, _PUBLIC_IDENTIFIER}

    def _run(self) -> None:
        try:
            with WamEventConnection(
                self._speaker_ip,
                self._client_uuid,
                port=self._port,
            ) as connection:
                with self._connection_lock:
                    self._connection = connection
                self._ready.set()
                for event in connection.events(stop=self._stop):
                    if self._belongs_to_attempt(event):
                        self._started.set()
                        LOGGER.info(
                            "Speaker emitted %s for URL playback",
                            _SUCCESS_EVENT,
                        )
                        continue
                    command = self._match_pending(event)
                    if command is not None:
                        self._record_response(command, event)
                        continue
                    if event.method == _FAILURE_EVENT and self._armed.is_set():
                        code = event.reported_error_code
                        if (
                            self._stream_active.is_set()
                            and not self._started.is_set()
                            and code == "NETWORK_TIMEOUT_ERROR"
                        ):
                            self._error = (
                                f"Speaker reported {_FAILURE_EVENT} {code}"
                            )
                            return
                        LOGGER.warning(
                            "Ignoring unmatched ErrorEvent during PCM startup: "
                            "code=%s user=%s",
                            code or "unknown",
                            event.user_identifier or "unknown",
                        )
        except Exception as error:  # noqa: BLE001 - surface listener failure
            message = f"Event listener failed: {error}"
            if self._startup_complete.is_set():
                LOGGER.warning("%s; continuing active PCM stream", message)
            else:
                self._error = message
            self._ready.set()
        finally:
            with self._connection_lock:
                self._connection = None


def run(
    args: argparse.Namespace,
    *,
    pcm_input: BinaryIO | None = None,
    protocol_output: TextIO | None = None,
) -> int:
    """Run one raw-PCM helper session."""
    input_stream = pcm_input if pcm_input is not None else sys.stdin.buffer
    output_stream = protocol_output if protocol_output is not None else sys.stdout
    store = ProfileStore(args.config)
    speaker_ip, speaker_port = select_speaker(args, store)
    client_uuid = load_client_uuid()
    response = probe(speaker_ip, port=speaker_port)
    LOGGER.info(
        "Speaker %s replied with %s",
        speaker_ip,
        response.method or "XML",
    )

    host_ip = local_ip_for(speaker_ip)
    server = PcmAudioStreamServer(
        input_stream,
        sample_rate=args.sample_rate,
        channels=args.channels,
        sample_format=args.sample_format,
        profile=args.format,
        bind=args.bind,
        port=args.http_port,
        ffmpeg=args.ffmpeg,
        startup_silence_ms=args.startup_silence,
    )
    restore_volume: int | None = None
    volume_changed = False
    startup_complete = False
    try:
        current_volume = get_volume(speaker_ip, port=speaker_port)
        start_volume = choose_start_volume(
            current_volume,
            args.volume,
            args.max_start_volume,
        )
        # Only where the recovery above actually fired. Restoring a 0 that was
        # found rather than chosen would put the speaker back into the silence
        # this startup just took it out of, and the abort path is exactly when
        # nobody is watching. With an explicit level the 0 was not recovered
        # from, so leaving no trace remains the right contract.
        restore_volume = (
            start_volume
            if recovers_from_silence(current_volume, args.volume)
            else current_volume
        )
        LOGGER.info(
            "Speaker volume is %s; starting PCM playback at %s",
            current_volume,
            start_volume,
        )

        if current_volume != 0:
            volume_changed = True
            set_volume(speaker_ip, 0, port=speaker_port)
            _raise_if_pcm_input_closed(input_stream)

        server.start()
        stream_url = server.url(host_ip)
        with PlaybackWatcher(
            speaker_ip,
            client_uuid,
            port=speaker_port,
        ) as watcher:
            LOGGER.info("Offering %s to %s", stream_url, speaker_ip)
            watcher.arm()
            watcher.offer_stream(stream_url)
            _raise_if_pcm_input_closed(input_stream)

            _wait_for_stream_request(
                server,
                input_stream,
                timeout=args.startup_timeout,
                watcher=watcher,
            )
            watcher.mark_stream_active()
            print("WAMBRIDGE STREAM_REQUESTED", file=output_stream, flush=True)
            server.release_audio()
            _wait_for_stream_event(
                server,
                "encoder_started",
                timeout=args.startup_timeout,
            )
            print("WAMBRIDGE ENCODER_STARTED", file=output_stream, flush=True)
            volume_changed = True
            watcher.set_volume(start_volume)
            _raise_if_pcm_input_closed(input_stream)
            print("WAMBRIDGE READY", file=output_stream, flush=True)

            _wait_for_stream_event(
                server,
                "audio_started",
                timeout=args.startup_timeout,
            )
            print("WAMBRIDGE AUDIO_STARTED", file=output_stream, flush=True)
            watcher.set_volume(start_volume)
            watcher.raise_if_failed()
            rejection = watcher.wait_for_response(
                _VOLUME_COMMAND,
                timeout=_VOLUME_ACK_TIMEOUT,
            )
            if rejection is not None:
                # The speaker was muted on purpose before playback started.
                # A rejected restore leaves it audibly silent, so reporting
                # PLAYING here would hide the failure and skip the restore.
                raise StreamError(f"{rejection}; the speaker would stay muted")
            startup_complete = True
            watcher.mark_startup_complete()
            print(
                f"WAMBRIDGE PLAYING volume={start_volume}",
                file=output_stream,
                flush=True,
            )

            while not server.request_finished.wait(timeout=1):
                watcher.raise_if_failed()
            watcher.raise_if_failed()
        if server.error:
            raise StreamError(server.error)
        return 0
    except KeyboardInterrupt:
        print("WAMBRIDGE STOPPING", file=output_stream, flush=True)
        return 130
    finally:
        try:
            if (
                restore_volume is not None
                and volume_changed
                and not startup_complete
            ):
                try:
                    set_volume(
                        speaker_ip,
                        restore_volume,
                        port=speaker_port,
                        timeout=1.0,
                    )
                except WamApiError as error:
                    LOGGER.warning(
                        "Could not restore speaker volume after aborted PCM "
                        "startup: %s",
                        error,
                    )
        finally:
            server.close()


def main(argv: list[str] | None = None) -> int:
    """Run the PCM helper protocol."""
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
        print(f"WAMBRIDGE ERROR {error}", flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
