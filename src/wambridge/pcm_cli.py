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
from .control_cli import DEFAULT_RETRIES, DEFAULT_RETRY_DELAY, ControlError, Target, standby
from .discovery import local_ip_for
from .identity import load_client_uuid
from .lease import Lease, claim_lease, find_stale_leases, remove_lease, write_lease
from .pcm_stream import PCM_FORMATS, PcmAudioStreamServer
from .profiles import ProfileError, ProfileStore
from .samsung import (
    WamApiError,
    assert_stream_reachable,
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
        "--menu-sleep-timer-active",
        action="store_true",
        help=(
            "Preserve an explicit menu-owned timer already accepted by the "
            "speaker while retaining --sleep-after-stop for later release"
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
        menu_sleep_timer_active: bool = False,
        clear_sleep_timer: bool = False,
    ) -> None:
        self._speaker_ip = speaker_ip
        self._client_uuid = client_uuid.casefold()
        self._port = port
        self._sleep_after_stop = sleep_after_stop
        self._menu_sleep_timer_active = menu_sleep_timer_active
        self._clear_sleep_timer = clear_sleep_timer
        self._released = False
        # Guards the check-then-set on _released. Single-threaded until the
        # control channel's dispatch thread could call release()/discard()
        # concurrently with __exit__'s own call - both must not pass the
        # `if self._released` check in the same instant.
        self._release_lock = threading.Lock()
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
        # Written once arm() sends the playback command, removed once _release()
        # tears the session down. A process killed in between leaves the file
        # behind for the next session's stale-lease sweep to find - see lease.py.
        self._lease: Lease | None = None
        self._connection: WamEventConnection | None = None
        self._connection_lock = threading.Lock()
        self._pending: list[str] = []
        self._results: dict[str, str] = {}
        self._response_events: dict[str, WamEvent] = {}
        self._response_lock = threading.Lock()
        self._volume_lock = threading.Lock()
        # Serialize actual SetVolume writes. In particular, a listener-thread
        # pause re-zero must either finish before resume/teardown restores the
        # saved level, or be cancelled when that restore already owns the lane.
        self._volume_write_lock = threading.Lock()
        # Actual raw level last sent through this helper, or later observed from
        # the speaker. Startup seeds it before the loopback control channel is
        # announced, and VolumeLevel broadcasts keep it honest when another
        # client or the physical buttons move the M5. Pause can therefore avoid
        # a GetVolume round trip without restoring stale state.
        self._current_volume: int | None = None
        self._pause_restore_volume: int | None = None
        # Keep logical routed-pause state separate from the restore target. A
        # rejected SetVolume(0) is ambiguous on this firmware because replies
        # have no request IDs: the target may still be needed to unstick raw 0
        # even after we fall back to paced PCM silence.
        self._pause_volume_active = False
        self._pause_rezero_pending = False
        # One SetVolume can be awaiting a VolumeLevel response at a time. Keep
        # its requested level so an unsolicited physical/client change is not
        # mistaken for that response merely because Samsung uses the same event.
        self._pending_volume_level: int | None = None
        self._pending_volume_rezero = False
        self._matched_volume_rezero = False

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
        self._release(arm_sleep_timer=True)

    def discard(self) -> None:
        """Release the speaker without arming a sleep timer.

        For the component replacing this helper (a seek or format change),
        not for ending the listening session. The stop and the paused-volume
        restore still run - the old session really is over from the
        speaker's side until the replacement helper starts, and skipping
        them risks the exact "still lit the next morning" failure
        ``release()`` exists to prevent, since ``flush()`` can run this
        before the component has fully decided the process is ending rather
        than being replaced. Only the sleep timer is what a replacement must
        not arm: it would put the speaker to sleep mid-track, which is
        exactly what ``cancel_sleep_timer()`` exists to race against today.
        """
        self._release(arm_sleep_timer=False)

    def _release(self, *, arm_sleep_timer: bool) -> None:
        """Shared body of ``release()`` and ``discard()`` - see their docstrings."""
        with self._release_lock:
            if self._released:
                return
            self._released = True
        try:
            self._release_locked(arm_sleep_timer=arm_sleep_timer)
        finally:
            # Only once the outcome is known, and only when it says the
            # speaker is actually clear. "stop=unreachable"/"stop=rejected"
            # mean the abandoned SetUrlPlayback session may still be held -
            # removing the lease there would tell the next session's sweep
            # there is nothing left to recover, which is exactly backwards.
            if self._lease is not None and not self.release_summary.startswith(
                ("stop=unreachable", "stop=rejected")
            ):
                remove_lease(self._lease)
                self._lease = None

    def _release_locked(self, *, arm_sleep_timer: bool) -> None:
        """Body of ``_release`` proper, run once ``_released`` is claimed."""
        # A stop, track change or foobar shutdown does not have to deliver a
        # matching resume callback. Restore the raw volume we replaced with 0 for pause
        # while the persistent 55001 connection is still alive.
        restore_status = self._restore_pause_volume_quietly()
        restore_suffix = f" restore={restore_status}" if restore_status else ""
        if not self._armed.is_set():
            # No URL was ever offered, so there is no playback session of ours
            # to end. Sending a stop anyway would reach past this helper into
            # whatever else the speaker is doing.
            self.release_summary = f"stop=skipped {self._unarmed_sleep_field()}{restore_suffix}"
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
            self.release_summary = f"stop=skipped {self._unarmed_sleep_field()}{restore_suffix}"
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
            self.release_summary = f"stop=skipped {self._unarmed_sleep_field()}{restore_suffix}"
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
            self.release_summary = f"stop=unreachable {self._unarmed_sleep_field()}{restore_suffix}"
            LOGGER.warning("Could not stop speaker playback: %s", error)
            return
        rejection = self.wait_for_response(
            _STOP_COMMAND,
            timeout=_STOP_ACK_TIMEOUT,
        )
        self.release_summary = ("stop=rejected" if rejection else "stop=sent") + restore_suffix
        if rejection:
            LOGGER.warning("%s while releasing the speaker", rejection)

        if not arm_sleep_timer:
            # discard(): a replacement is coming, and it will keep the speaker
            # awake on its own - arming here is exactly the race
            # cancel_sleep_timer() exists to clear after the fact.
            # Not "sleep=skipped" - _unarmed_sleep_field already gives that
            # string a different meaning (configured but nothing to arm
            # after), and it would collide here with a genuine "stop=sent".
            self.release_summary += " sleep=discarded"
            return
        if self._menu_sleep_timer_active:
            self.release_summary += " sleep=menu"
            return
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
        self._lease = write_lease(self._speaker_ip, self._port)

    def mark_stream_active(self) -> None:
        """Mark that the speaker requested this attempt's local HTTP stream."""
        self._stream_active.set()

    def mark_startup_complete(self) -> None:
        """Keep later listener transport failures diagnostic only."""
        self._startup_complete.set()

    def offer_stream(self, stream_url: str) -> None:
        """Send SetUrlPlayback through the connection that receives its events.

        The reachability check runs here too, and it matters more on this path
        than on the plain one: this connection is the single owner of the
        control socket, so a wedge here takes the listener down with it and the
        session cannot even report its own stop.
        """
        assert_stream_reachable(stream_url)
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
        with self._volume_write_lock:
            with self._volume_lock:
                if self._pause_volume_active:
                    # A slider move while routed pause is active changes what
                    # resume should restore, not the speaker's current zero.
                    self._pause_restore_volume = level
                    return
                if self._pause_restore_volume is not None:
                    # An earlier pause write may have been rejected ambiguously.
                    # Keep the newest desired level as the safety restore target,
                    # but do not swallow the slider while paced PCM is fallback.
                    self._pause_restore_volume = level
                # Publish the optimistic level before sending. The listener can
                # receive an immediate rejection while `send()` is still on this
                # thread; a post-send assignment would overwrite its invalidation.
                self._current_volume = level
            try:
                self._send_volume_state(level)
            except (StreamError, WamApiError):
                with self._volume_lock:
                    if self._current_volume == level:
                        self._current_volume = None
                raise

    def set_sleep_timer(self, seconds: int) -> None:
        """Arm or cancel an explicit menu timer on the persistent connection.

        While that timer is armed it owns the deadline. ``release()`` must not
        replace it with ``sleep_after_stop`` merely because playback ended.
        Cancelling it restores the normal automatic-after-stop behaviour.
        """
        self._send_command(
            method=_SLEEP_COMMAND,
            arguments=sleep_timer_arguments(seconds),
        )
        rejection = self.wait_for_response(
            _SLEEP_COMMAND,
            timeout=_STOP_ACK_TIMEOUT,
        )
        if rejection:
            raise WamApiError(rejection)
        self._menu_sleep_timer_active = seconds > 0

    def set_pause_volume(self, paused: bool) -> None:
        """Silence pause with raw volume 0 while preserving the live HTTP stream."""
        if paused:
            with self._volume_write_lock:
                with self._volume_lock:
                    if self._pause_volume_active:
                        return
                    if self._pause_restore_volume is not None:
                        # A previous ambiguous pause failure still carries a
                        # restore debt. The component is already using paced PCM
                        # fallback, so do not stack another indistinguishable 0.
                        return
                    previous = self._current_volume
                    if previous is None:
                        # The control channel is announced only after startup has
                        # sent its volume, so this is defensive rather than normal.
                        raise WamApiError("Speaker volume is unknown at pause")
                    self._pause_restore_volume = previous
                    self._pause_volume_active = True
                    self._pause_rezero_pending = False
                    if previous != 0:
                        # Optimistic raw 0 is published before sending so an
                        # immediate rejection can invalidate it without being
                        # overwritten after send returns.
                        self._current_volume = 0
                if previous == 0:
                    return
                # Physical M5, 2026-08-28: 3 -> 0 -> 3 kept the exact same HTTP
                # request, helper PID, FFmpeg PID and advancing CLOCK. SetMute did
                # the opposite and closed the speaker's HTTP pull.
                try:
                    self._set_volume_and_wait(0)
                    # A physical/client change can arrive during the one-second
                    # pause response budget while this thread owns the write lane.
                    # The listener records that as deferred work; flush it before
                    # releasing the lane so buffered audio cannot leak.
                    self._reapply_pause_zero_locked(required=False)
                except (StreamError, WamApiError):
                    with self._volume_lock:
                        # The rejection may belong to an older superseded slider
                        # because Samsung gives SetVolume no request ID. Fall back
                        # to paced PCM, but retain the restore target so resume or
                        # teardown can still recover if raw 0 actually took.
                        self._pause_volume_active = False
                        self._pause_rezero_pending = False
                        if self._current_volume == 0:
                            self._current_volume = None
                    raise
            self._drain_pause_rezero_after_unlock()
            return

        try:
            self._restore_pause_volume()
        except (StreamError, WamApiError) as error:
            # Failed resume must not leave live playback at raw volume 0. Mark
            # the helper failed so the component can rebuild the URL session.
            self._error = f"Could not restore speaker volume after pause: {error}"
            raise

    def _send_volume_state(self, level: int, *, pause_rezero: bool = False) -> None:
        self._send_command(
            method=_VOLUME_COMMAND,
            arguments=[("volume", level, "dec")],
            power_on=True,
            requested_volume=level,
            pause_rezero=pause_rezero,
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
        with self._volume_write_lock:
            with self._volume_lock:
                previous = self._pause_restore_volume
                self._pause_volume_active = False
                self._pause_rezero_pending = False
                if previous is None:
                    return
                if previous != 0:
                    # Publish the restore optimistically before sending. Any
                    # later VolumeLevel event, including a physical/client change
                    # during the response wait, remains authoritative and must not
                    # be overwritten when the wait finishes.
                    self._current_volume = previous
            if previous != 0:
                self._set_volume_and_wait(previous)
            with self._volume_lock:
                self._pause_restore_volume = None

    def _drain_pause_rezero_after_unlock(self) -> None:
        """Drain work deferred at the write-lane release boundary."""
        with self._volume_lock:
            pending = self._pause_volume_active and self._pause_rezero_pending
        if not pending:
            return
        with self._volume_write_lock:
            self._reapply_pause_zero_locked(required=False)

    def _reapply_pause_zero_locked(self, *, required: bool) -> None:
        """Send deferred raw-0 writes while the caller owns the write lane."""
        while True:
            with self._volume_lock:
                if not self._pause_volume_active:
                    self._pause_rezero_pending = False
                    return
                if not required and not self._pause_rezero_pending:
                    return
                required = False
                self._pause_rezero_pending = False
                self._current_volume = 0
            try:
                # This compensating write must not block the listener thread on a
                # firmware response. A later matched rejection is tagged and makes
                # the helper fail rather than leaving queued audio audible.
                self._send_volume_state(0, pause_rezero=True)
            except (StreamError, WamApiError) as error:
                with self._volume_lock:
                    self._current_volume = None
                self._error = f"Could not reapply pause volume: {error}"
                LOGGER.warning("%s", self._error)
                return
            # If another external level arrived while send() held this same lane,
            # _observe_volume_event marked another deferred re-zero. Loop only for
            # work actually observed; normal operation exits after one send.

    @staticmethod
    def _event_volume_level(event: WamEvent) -> int | None:
        """Return a valid raw level carried by a SetVolume/VolumeLevel event."""
        if not event.method or not methods_agree(_VOLUME_COMMAND, event.method):
            return None
        for key, value in event.values.items():
            if key.casefold() not in {"volume", "volumelevel", "volume_level"}:
                continue
            try:
                candidate = int(value)
            except (TypeError, ValueError):
                return None
            if RAW_MIN_VOLUME <= candidate <= RAW_MAX_VOLUME:
                return candidate
            return None
        return None

    def _observe_volume_event(self, event: WamEvent, *, external: bool) -> None:
        """Refresh cached raw volume from a VolumeLevel speaker event."""
        level = self._event_volume_level(event)
        if level is None:
            return
        reapply_zero = False
        with self._volume_lock:
            self._current_volume = level
            # Non-zero speaker changes while a restore target exists become the
            # newest target. This also covers the ambiguous-pause fallback state:
            # resume/teardown should preserve a later physical/client choice even
            # though routed pause itself is no longer considered active.
            if self._pause_restore_volume is not None and level != 0:
                self._pause_restore_volume = level
                reapply_zero = self._pause_volume_active and external
        if not reapply_zero:
            return

        # A physical button/second client temporarily made the speaker audible
        # while routed pause is still active. Preserve that level for resume, but
        # immediately put the live URL/PCM session back at raw 0. If initial pause
        # still owns the write lane, defer instead of dropping this compensation.
        if not self._volume_write_lock.acquire(blocking=False):
            with self._volume_lock:
                if self._pause_volume_active:
                    self._pause_rezero_pending = True
            return
        try:
            self._reapply_pause_zero_locked(required=True)
        finally:
            self._volume_write_lock.release()

    def _restore_pause_volume_quietly(self) -> str | None:
        for attempt in range(2):
            try:
                self._restore_pause_volume()
                return None
            except StreamError as error:
                LOGGER.warning("Could not restore speaker volume after pause: %s", error)
                return "unreachable"
            except WamApiError as error:
                rejected = "rejected" in str(error).casefold()
                if rejected and attempt == 0:
                    LOGGER.warning(
                        "Speaker rejected pause-volume restore; retrying once: %s",
                        error,
                    )
                    continue
                LOGGER.warning("Could not restore speaker volume after pause: %s", error)
                return "rejected" if rejected else "unreachable"
        return "rejected"

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
                self._retire_pending(method)
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
                self._retire_pending(method)
                return None

    def _retire_pending(self, method: str) -> None:
        """Stop treating an unanswered command as a future response target."""
        with self._response_lock:
            self._pending[:] = [pending for pending in self._pending if pending != method]
            if method == _VOLUME_COMMAND:
                self._pending_volume_level = None
                self._pending_volume_rezero = False

    def _send_command(
        self,
        *,
        method: str,
        arguments: list[tuple[str, str | int, str]] | None = None,
        power_on: bool = False,
        requested_volume: int | None = None,
        pause_rezero: bool = False,
    ) -> None:
        with self._connection_lock:
            connection = self._connection
        if connection is None:
            raise StreamError("WAM control connection is not ready")
        with self._response_lock:
            self._results.pop(method, None)
            self._response_events.pop(method, None)
            if method == _VOLUME_COMMAND:
                # SetVolume has no request ID and may answer with silence. A newer
                # setter supersedes every older one, otherwise an old pending entry
                # can steal a later unsolicited VolumeLevel broadcast.
                self._pending[:] = [
                    pending for pending in self._pending if pending != method
                ]
                self._pending_volume_level = requested_volume
                self._pending_volume_rezero = pause_rezero
            self._pending.append(method)
        try:
            connection.send(
                method=method,
                arguments=arguments,
                power_on=power_on,
            )
        except (OSError, WamEventError) as error:
            self._retire_pending(method)
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
        event_volume = self._event_volume_level(event)
        with self._response_lock:
            for index, command in enumerate(self._pending):
                if not methods_agree(command, event.method):
                    continue
                if (
                    command == _VOLUME_COMMAND
                    and (event.result or "").casefold() == "ok"
                    and event_volume is not None
                    and self._pending_volume_level is not None
                    and event_volume != self._pending_volume_level
                ):
                    # Samsung uses VolumeLevel for both SetVolume replies and
                    # unsolicited physical/client changes. A different explicit
                    # level cannot acknowledge our request, so leave the setter
                    # pending and let the caller classify this event as external.
                    continue
                del self._pending[index]
                if command == _VOLUME_COMMAND:
                    self._matched_volume_rezero = self._pending_volume_rezero
                    self._pending_volume_level = None
                    self._pending_volume_rezero = False
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
            pause_rezero = (
                self._matched_volume_rezero
                if command == _VOLUME_COMMAND
                else False
            )
            if command == _VOLUME_COMMAND:
                self._matched_volume_rezero = False
        if not message:
            return
        if command == _VOLUME_COMMAND:
            # Routed slider writes are cached optimistically because this firmware
            # may answer successful SetVolume with silence. A matched rejection is
            # the one case where that optimistic level is definitely false. Do not
            # guess the previous level amid possibly overlapping slider writes; mark
            # it unknown until a later routed write or observed VolumeLevel refreshes
            # the cache, so pause cannot restore a level the speaker rejected.
            with self._volume_lock:
                self._current_volume = None
            if pause_rezero:
                # Only the listener-thread compensating re-zero is asynchronous.
                # The initial pause write has its own waiter and, if rejected, must
                # simply fall back to paced PCM silence rather than kill the helper.
                self._error = f"Could not keep speaker paused: {message}"
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


def _recover_abandoned_speakers(current_target: tuple[str, int]) -> None:
    """Send ``standby`` for any speaker a crashed prior session left holding.

    A PC that loses power, or a helper killed outright, runs no cleanup of
    its own - the lease it wrote in ``arm()`` is exactly what survives that,
    and this is the other side of it: every new PCM session checks for one
    before starting its own, so recovery does not depend on anyone noticing
    a speaker that never idles. Runs on a background thread (see its call
    site in ``run()``) so an unreachable stale speaker's retries and timeouts
    never delay the session actually being started.

    ``current_target`` is this session's own ``(speaker_ip, speaker_port)`` -
    a stale lease naming it is not sent ``standby``. This session is about to
    become that speaker's new legitimate owner; its own ``SetUrlPlayback``
    supersedes whatever the crashed session left behind, and racing this
    background thread's pause/mute against the fresh stream this same
    startup is about to offer would stop the very playback being started
    (found in review - the naive version of this function recovered
    unconditionally).  The stale lease is simply discarded instead.

    ``require_stop_confirmed=True`` matters here specifically: an
    interactive ``standby`` treats a confirmed mute as success even if the
    stop itself failed, which is right for a human clearing a speaker they
    can see. Automated recovery has no one to notice a wrong call - the
    abandoned ``SetUrlPlayback`` session must be confirmed stopped, or the
    lease has to survive to be tried again.

    Claiming before calling ``standby`` (see ``claim_lease``) is what keeps
    two sessions started close together from both recovering the same
    speaker at once - the claim is left in place, not undone, on failure,
    which doubles as backoff until the next sweep's claim-age check allows a
    retry.
    """
    for lease in find_stale_leases():
        if (lease.speaker_ip, lease.speaker_port) == current_target:
            remove_lease(lease)
            continue
        claimed = claim_lease(lease)
        if claimed is None:
            continue  # lost the race to another sweep, or already resolved
        try:
            standby(
                Target(claimed.speaker_ip, claimed.speaker_port),
                retries=DEFAULT_RETRIES,
                retry_delay=DEFAULT_RETRY_DELAY,
                require_stop_confirmed=True,
            )
        except ControlError as error:
            LOGGER.warning(
                "Could not recover speaker %s from a crashed session (pid %s): %s",
                claimed.speaker_ip,
                claimed.pid,
                error,
            )
            continue
        LOGGER.info(
            "Recovered speaker %s, abandoned by a crashed session (pid %s)",
            claimed.speaker_ip,
            claimed.pid,
        )
        remove_lease(claimed)


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
    # Backgrounded: a stale lease naming an unreachable speaker retries and
    # times out on its own schedule (up to ~30 s, see RECOVERY_CLAIM_TIMEOUT_S
    # in lease.py), and none of that may delay the session this call is
    # actually here to start.
    threading.Thread(
        target=_recover_abandoned_speakers,
        args=((speaker_ip, speaker_port),),
        name="wambridge-abandoned-speaker-sweep",
        daemon=True,
    ).start()
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
            menu_sleep_timer_active=args.menu_sleep_timer_active,
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
                set_release=watcher.release,
                set_discard=watcher.discard,
                set_sleep_timer=watcher.set_sleep_timer,
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
