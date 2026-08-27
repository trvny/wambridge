"""Process protocol for streaming foobar2000 PCM to Samsung WAM."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
from collections.abc import Iterator
from time import monotonic
from typing import BinaryIO, TextIO

from .cli import choose_start_volume, recovers_from_silence, volume_level
from .cli_common import (
    DEFAULT_MAX_START_VOLUME,
    RAW_MAX_VOLUME,
    RAW_MIN_VOLUME,
    add_target_arguments,
    bounded_int,
    configure_logging,
    select_speaker,
)
from .connections import wait_until_released
from .control_channel import ControlChannel
from .discovery import local_ip_for
from .identity import load_client_uuid
from .pcm_stream import PCM_FORMATS, PcmAudioStreamServer
from .profiles import ProfileError, ProfileStore
from .samsung import (
    WamApiError,
    get_volume,
    methods_agree,
    probe,
    set_volume,
    sleep_timer_arguments,
)
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
_PAUSE_ACK_TIMEOUT = 1.0
_STOP_COMMAND = "SetPlaybackControl"
_SLEEP_COMMAND = "SetSleepTimer"
_STOP_ACK_TIMEOUT = 1.0
# Long enough for an orderly close to leave the table, short enough that the
# component's shutdown grace does not expire and terminate the helper mid-release
# - which is the very shape of teardown that leaves the speaker lit.
_RELEASE_TIMEOUT = 1.5
_RELEASE_POLL = 0.25
MAXIMUM_SLEEP_AFTER_STOP_SECONDS = 86400


sample_rate = bounded_int("sample rate", minimum=1)
"""Parse a PCM sample rate."""

channel_count = bounded_int("channels", minimum=1)
"""Parse a PCM channel count."""

startup_silence = bounded_int("startup silence in ms", minimum=0, maximum=10000)
"""Parse the leading silence in milliseconds."""

sleep_after_stop = bounded_int(
    "sleep timer in seconds",
    minimum=0,
    maximum=MAXIMUM_SLEEP_AFTER_STOP_SECONDS,
)
"""Parse the sleep timer armed when a stream ends; 0 arms nothing."""


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
    parser.add_argument(
        "--sleep-after-stop",
        type=sleep_after_stop,
        default=0,
        help=(
            "Seconds of sleep timer to arm once the stream ends; 0 arms "
            "nothing. A fallback: the speaker sleeps on its own once every "
            "program lets go, and this helper now releases it, but nothing "
            "can read or set that idle power-down"
        ),
    )
    parser.add_argument(
        "--clear-sleep-timer",
        action="store_true",
        help=(
            "Clear any pending sleep timer before offering the stream. Set by "
            "the component once some helper of the playback session has armed "
            "one, which is not the same as --sleep-after-stop being non-zero: "
            "the setting can drop to zero while an armed timer still runs"
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

    def __init__(
        self,
        speaker_ip: str,
        client_uuid: str,
        *,
        port: int,
        sleep_after_stop: int = 0,
        clear_sleep_timer: bool = False,
    ) -> None:
        self._speaker_ip = speaker_ip
        self._client_uuid = client_uuid.casefold()
        self._port = port
        self._sleep_after_stop = sleep_after_stop
        self._clear_sleep_timer = clear_sleep_timer
        self._released = False
        # Carries the sleep field from the start. A session whose __enter__
        # raises never reaches release(), and this default is what the teardown
        # line prints - so without it that one path reports a shorter line than
        # every other, which is exactly the line read the morning after.
        self.release_summary = f"stop=skipped {_unarmed_sleep_field(sleep_after_stop)}"
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
        self._response_events: dict[str, WamEvent] = {}
        self._response_lock = threading.Lock()
        self._volume_lock = threading.Lock()
        # Actual raw level last sent through this helper, or later observed from
        # the speaker. Startup seeds it before the loopback control channel is
        # announced, and VolumeLevel broadcasts keep it honest when another
        # client or the physical buttons move the M5. Pause can therefore avoid
        # a GetVolume round trip without restoring stale state.
        self._current_volume: int | None = None
        self._pause_restore_volume: int | None = None

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
        # Before the stop flag, not after: the listener thread owns the socket
        # this releases the speaker over, and every exit path arrives here -
        # including the ones that failed, which are exactly the sessions that
        # used to walk away from a speaker still holding a playback session.
        self.release()
        self._stop.set()
        if self._thread is not None:
            self._thread.join()

    def _unarmed_sleep_field(self) -> str:
        """Report why no timer was armed, for this watcher's configuration."""
        return _unarmed_sleep_field(self._sleep_after_stop)

    def release(self) -> None:
        """Tell the speaker the stream is over, on the connection already open.

        Nothing else does. Teardown closes the local HTTP server and this
        socket, which leaves the M5 holding a URL playback session whose source
        simply vanished - and a speaker that believes it is still serving one
        never reaches the idle state its own power-down needs: after a session
        that ended this way on 2026-08-08 the speaker was still lit the next
        morning.

        Best effort by construction. This runs while a session is being torn
        down, often because something already went wrong, so a speaker that has
        gone away must not turn a stop into a second failure. What happened is
        recorded in ``release_summary`` for the protocol line instead.
        """
        if self._released:
            return
        self._released = True
        # A stop, track change or foobar shutdown does not have to deliver a
        # matching resume callback. Restore the raw volume we replaced with 0 for pause
        # while the persistent 55001 connection is still alive.
        self._restore_pause_volume_quietly()
        if not self._armed.is_set():
            # No URL was ever offered, so there is no playback session of ours
            # to end. Sending a stop anyway would reach past this helper into
            # whatever else the speaker is doing.
            self.release_summary = f"stop=skipped {self._unarmed_sleep_field()}"
            return
        with self._response_lock:
            refused = self._results.get(_PLAYBACK_COMMAND)
        if refused:
            # Offered and refused is not the same as owned. A matched rejection
            # means the speaker never took the URL, so it is still doing
            # whatever it was doing before - a TuneIn station, someone else's
            # DLNA queue - and `pause` would reach past this helper and stop
            # that instead. Arming happens before the offer, so it cannot carry
            # this distinction on its own.
            LOGGER.debug("Not releasing a playback the speaker refused")
            self.release_summary = f"stop=skipped {self._unarmed_sleep_field()}"
            return
        if not self._stream_active.is_set():
            # Offered and never taken up. The rejection above only catches a
            # refusal the speaker bothered to send; this firmware answers plenty
            # of things with silence, so the commoner way to own nothing is an
            # offer that simply went unanswered - a startup timeout, or stdin
            # closing right after the URL went out. Until the speaker fetches
            # the stream there is no session of ours to end, and pausing on the
            # guess does the same harm the rejection case avoids: it reaches
            # past this helper into whatever the speaker is really doing.
            LOGGER.debug("Not releasing a stream the speaker never requested")
            self.release_summary = f"stop=skipped {self._unarmed_sleep_field()}"
            return

        # `pause` rather than `stop`: measured on this firmware, the URL and DLNA
        # path answers UIC pause, while `stop` belongs to the native CP API. The
        # mute that `stop_playback` pairs with it is deliberately left out - it
        # would hand the speaker back silent to whoever picks it up next.
        try:
            self._send_command(
                method=_STOP_COMMAND,
                arguments=[("playbackcontrol", "pause", "str")],
            )
        except (StreamError, WamApiError) as error:
            self.release_summary = f"stop=unreachable {self._unarmed_sleep_field()}"
            LOGGER.warning("Could not stop speaker playback: %s", error)
            return
        rejection = self.wait_for_response(
            _STOP_COMMAND,
            timeout=_STOP_ACK_TIMEOUT,
        )
        self.release_summary = "stop=rejected" if rejection else "stop=sent"
        if rejection:
            LOGGER.warning("%s while releasing the speaker", rejection)

        if self._sleep_after_stop <= 0:
            self.release_summary += " sleep=off"
            return
        try:
            self._send_command(
                method=_SLEEP_COMMAND,
                arguments=sleep_timer_arguments(self._sleep_after_stop),
            )
        except (StreamError, WamApiError) as error:
            self.release_summary += " sleep=unreachable"
            LOGGER.warning("Could not arm the sleep timer: %s", error)
            return
        # Same treatment the stop gets, and for the same reason: `_send_command`
        # returns once the request is written, so without this a speaker that
        # answered `result="ng"` was reported as `sleep=Ns`. The listener stops
        # right after release, so an unmatched rejection would never be seen.
        rejection = self.wait_for_response(
            _SLEEP_COMMAND,
            timeout=_STOP_ACK_TIMEOUT,
        )
        if rejection:
            self.release_summary += " sleep=rejected"
            LOGGER.warning("%s while arming the sleep timer", rejection)
            return
        self.release_summary += f" sleep={self._sleep_after_stop}s"

    def cancel_sleep_timer(self) -> None:
        """Clear a timer an earlier helper of this session armed on its way out.

        A seek stops one helper and starts another, so the timer armed by the
        one that left survives into the stream that replaced it and would put
        the speaker into standby mid-track.

        Whether to clear is the component's call, not this process's, and it
        arrives as ``--clear-sleep-timer``. Reading it off ``sleep_after_stop``
        instead was wrong in the one case that matters: with the setting moved
        to zero, the helper saw a disabled feature and left the timer its
        predecessor had armed running, so the speaker slept mid-track precisely
        when the listener had just turned the feature off. The component keeps
        the flag sticky for the playback session, so it survives that change.

        Two limits, both real. This clears **any** pending timer, including one
        the listener set from the Samsung app, because the speaker does not say
        who armed it - so only a configuration that has armed timers clears
        them, and a default install never touches it. And it is a race, not a
        guarantee: the replacement only gets here after discovery, probing and
        the server coming up, so a short enough timer can fire first. Closing
        that needs the component to say whether a helper is being replaced or
        the session is ending, which it knows and the helper does not.

        Not covered, and not coverable here: a timer armed in an earlier foobar
        session. Nothing survives the component's own lifetime to record it, so
        turning the feature off and restarting foobar leaves at most one armed
        timer to fire once.
        """
        if not self._clear_sleep_timer:
            return
        try:
            self._send_command(
                method=_SLEEP_COMMAND,
                arguments=sleep_timer_arguments(0),
            )
        except (StreamError, WamApiError) as error:
            # Not fatal: the worst case is the speaker sleeping early, which is
            # visible and recoverable, while refusing to play over it is not.
            LOGGER.warning("Could not clear a pending sleep timer: %s", error)

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
        """Set speaker volume without opening a competing control socket."""
        with self._volume_lock:
            if self._pause_restore_volume is not None:
                # A slider move while paused changes what resume should restore,
                # not the speaker's current zero. PCM keeps flowing as silence.
                self._pause_restore_volume = level
                return
        self._send_volume_state(level)
        with self._volume_lock:
            self._current_volume = level

    def set_pause_volume(self, paused: bool) -> None:
        """Silence pause with raw volume 0 while preserving the live HTTP stream."""
        if paused:
            with self._volume_lock:
                if self._pause_restore_volume is not None:
                    return
                previous = self._current_volume
                if previous is None:
                    # The control channel is announced only after startup has
                    # sent its volume, so this is defensive rather than normal.
                    raise WamApiError("Speaker volume is unknown at pause")
                self._pause_restore_volume = previous
            if previous == 0:
                return
            # Physical M5, 2026-08-28: 3 -> 0 -> 3 kept the exact same HTTP
            # request, helper PID, FFmpeg PID and advancing CLOCK. SetMute did
            # the opposite and closed the speaker's HTTP pull.
            self._set_volume_and_wait(0)
            with self._volume_lock:
                self._current_volume = 0
            return

        try:
            self._restore_pause_volume()
        except (StreamError, WamApiError) as error:
            # Failed resume must not leave live playback at raw volume 0. Mark
            # the helper failed so the component can rebuild the URL session.
            self._error = f"Could not restore speaker volume after pause: {error}"
            raise

    def _send_volume_state(self, level: int) -> None:
        self._send_command(
            method=_VOLUME_COMMAND,
            arguments=[("volume", level, "dec")],
            power_on=True,
        )

    def _set_volume_and_wait(self, level: int) -> None:
        self._send_volume_state(level)
        rejection = self.wait_for_response(
            _VOLUME_COMMAND,
            timeout=_PAUSE_ACK_TIMEOUT,
        )
        if rejection is not None:
            raise WamApiError(rejection)

    def _restore_pause_volume(self) -> None:
        with self._volume_lock:
            previous = self._pause_restore_volume
        if previous is None:
            return
        if previous != 0:
            self._set_volume_and_wait(previous)
        with self._volume_lock:
            self._current_volume = previous
            self._pause_restore_volume = None

    def _observe_volume_event(self, event: WamEvent, *, external: bool) -> None:
        """Refresh cached raw volume from a VolumeLevel speaker event."""
        if not event.method or not methods_agree(_VOLUME_COMMAND, event.method):
            return
        level: int | None = None
        for key, value in event.values.items():
            if key.casefold() not in {"volume", "volumelevel", "volume_level"}:
                continue
            try:
                candidate = int(value)
            except (TypeError, ValueError):
                return
            if RAW_MIN_VOLUME <= candidate <= RAW_MAX_VOLUME:
                level = candidate
            break
        if level is None:
            return
        with self._volume_lock:
            self._current_volume = level
            # Non-zero speaker changes while paused become the target for resume.
            # Raw 0 is our pause primitive and may arrive as either a matched
            # response or an unmatched broadcast, so it must never erase the
            # saved target merely because the firmware changed event shape.
            if self._pause_restore_volume is not None and level != 0:
                self._pause_restore_volume = level

    def _restore_pause_volume_quietly(self) -> None:
        try:
            self._restore_pause_volume()
        except (StreamError, WamApiError) as error:
            LOGGER.warning("Could not restore speaker volume after pause: %s", error)

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

    def wait_for_response_event(self, method: str, *, timeout: float) -> WamEvent | None:
        """Return the full matched response event, or ``None`` on silence."""
        deadline = monotonic() + timeout
        while True:
            with self._response_lock:
                event = self._response_events.get(method)
                if event is not None:
                    return event
            if monotonic() >= deadline or self._stop.wait(timeout=0.05):
                return None

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
            self._response_events.pop(method, None)
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
            message = ""
        else:
            code = event.reported_error_code
            suffix = f" (error {code})" if code else ""
            message = f"Speaker rejected {command}{suffix}"
        with self._response_lock:
            self._response_events[command] = event
            self._results[command] = message
        if not message:
            return
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
                    self._observe_volume_event(event, external=command is None)
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


def _unarmed_sleep_field(sleep_after_stop: int) -> str:
    """Report why no timer was armed, without claiming none was configured.

    Both fields appear on every teardown line: one that changes shape by case
    is one more thing to work out at the moment it is being read to explain
    something that has already gone wrong. ``off`` means nobody asked for a
    timer; ``skipped`` means one was configured and this session had nothing to
    arm it after. This lives outside the watcher because the line still has to
    be printed when startup failed before there was one.
    """
    return "sleep=off" if sleep_after_stop <= 0 else "sleep=skipped"


def _stopped_line(
    watcher: PlaybackWatcher | None,
    speaker_ip: str,
    *,
    sleep_after_stop: int,
) -> str:
    """Describe how this session let the speaker go.

    Every session used to end with silence in the console, so the morning after
    a speaker that stayed lit there was nothing to read. ``holding`` counts
    every local socket still attached, including this helper's own - a
    connection *we* failed to close is a leak worth as much attention as
    anyone else's, and hiding it would make this line's zero mean "nobody
    checked".

    What is skipped is narrower: this helper's own sockets that are already
    closing. By the time this runs the server and the control socket have been
    closed, and measured on 2026-08-15 they sit in ``FIN_WAIT`` for a further
    0.5 s to 1.5 s while the kernel finishes. Waiting that out cost that long on
    every exit - and a helper exit precedes every seek, because the component
    stops one before starting its replacement - to report this teardown as
    something holding the speaker. A killed session's sockets are untouched by
    this: they belong to a process that is gone, so its PID cannot match ours.

    The value is ``unknown`` when the table could not be read, never a
    comforting zero.
    """
    summary = (
        watcher.release_summary
        if watcher is not None
        else f"stop=skipped {_unarmed_sleep_field(sleep_after_stop)}"
    )
    held = wait_until_released(
        speaker_ip,
        timeout=_RELEASE_TIMEOUT,
        poll=_RELEASE_POLL,
        own_pid=os.getpid(),
    )
    return (
        f"WAMBRIDGE STOPPED {summary} "
        f"holding={'unknown' if held is None else held}"
    )


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
    watcher: PlaybackWatcher | None = None
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
        # Bound before the `with`, so the teardown line can still report what
        # the release did when the body leaves by exception.
        watcher = PlaybackWatcher(
            speaker_ip,
            client_uuid,
            port=speaker_port,
            sleep_after_stop=args.sleep_after_stop,
            clear_sleep_timer=args.clear_sleep_timer,
        )
        with watcher:
            LOGGER.info("Offering %s to %s", stream_url, speaker_ip)
            watcher.cancel_sleep_timer()
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

            # Only now: before this point the startup sequence owns the volume,
            # and a level arriving mid-handshake would fight the unmute that
            # PLAYING depends on.
            with ControlChannel(
                watcher.set_volume,
                set_paused=watcher.set_pause_volume,
                minimum_volume=RAW_MIN_VOLUME,
                maximum_volume=RAW_MAX_VOLUME,
            ) as control:
                print(control.announcement, file=output_stream, flush=True)
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
            # After the server and the control socket are gone, never before:
            # the count is only meaningful once this helper has let go of
            # everything it held.
            print(
                _stopped_line(
                    watcher,
                    speaker_ip,
                    sleep_after_stop=args.sleep_after_stop,
                ),
                file=output_stream,
                flush=True,
            )


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
