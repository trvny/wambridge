"""A loopback control input for the PCM helper.

The helper's stdin carries PCM and its stdout carries the protocol, so there is
no way for the component to say anything to a running helper. That gap is why
every routed control from the foobar UI has to spawn `wambridge-control.exe`,
which opens a second TCP connection to port 55001 beside the persistent one
`pcm_cli` already owns - measured on 2026-08-08 at four connections per single
menu press, half of them re-verifying the identity of a speaker the helper is
already talking to.

This adds the missing direction: a listener bound to the loopback interface,
announced on the existing stdout protocol, that turns short text commands into
calls on the connection the helper already holds. Nothing new reaches the
speaker's network port.
"""

from __future__ import annotations

import logging
import secrets
import socket
import threading
from collections.abc import Callable

from .samsung import MAX_SLEEP_TIMER_SECONDS

LOGGER = logging.getLogger(__name__)

MAX_COMMAND_BYTES = 256
"""A command is a short ASCII line. Anything longer is not one of ours."""

ACCEPT_TIMEOUT = 0.5
"""How often the accept loop checks whether the session is shutting down."""


class ControlChannel:
    """Serve speaker controls to one local client over the loopback interface."""

    def __init__(
        self,
        set_volume: Callable[[int], None],
        *,
        set_paused: Callable[[bool], None] | None = None,
        set_release: Callable[[], None] | None = None,
        set_discard: Callable[[], None] | None = None,
        set_sleep_timer: Callable[[int], None] | None = None,
        minimum_volume: int = 0,
        maximum_volume: int = 30,
    ) -> None:
        self._set_volume = set_volume
        self._set_paused = set_paused
        # `release` is a real stop, sent by the component before it starts
        # killing this helper - see PlaybackWatcher.release(). `discard` is a
        # replacement (seek, format change) - see PlaybackWatcher.discard().
        # Both are best-effort like `pause`/`resume`: the stream matters more
        # than the command.
        self._set_release = set_release
        self._set_discard = set_discard
        self._set_sleep_timer = set_sleep_timer
        self._minimum_volume = minimum_volume
        self._maximum_volume = maximum_volume
        # Loopback only. This accepts commands that move a speaker in someone's
        # room, so it has no business being reachable from the network, and the
        # token keeps other local processes from driving it by guessing a port.
        self._server = socket.create_server(("127.0.0.1", 0), backlog=1)
        self._server.settimeout(ACCEPT_TIMEOUT)
        self.token = secrets.token_urlsafe(18)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._client: socket.socket | None = None
        self._client_lock = threading.Lock()
        # Serialize every callback that can move the speaker. close() takes this
        # lock after setting _stop, which means it cannot return while a pause
        # or volume command is still in flight, and a queued command sees _stop
        # before it can touch the speaker.
        self._dispatch_lock = threading.Lock()

    @property
    def port(self) -> int:
        """Return the bound loopback port."""
        return int(self._server.getsockname()[1])

    @property
    def announcement(self) -> str:
        """Return the stdout protocol line that tells the component where to go."""
        return f"WAMBRIDGE CONTROL_PORT {self.port} {self.token}"

    def start(self) -> None:
        """Begin accepting the single control client."""
        self._thread = threading.Thread(
            target=self._accept_loop,
            name="wambridge-control-channel",
            daemon=True,
        )
        self._thread.start()

    def close(self) -> None:
        """Stop serving and release the socket."""
        self._stop.set()
        with self._client_lock:
            client = self._client
            self._client = None
        if client is not None:
            _shutdown_quietly(client)
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2)
        self._server.close()
        # PlaybackWatcher.release() runs immediately after this context exits.
        # Wait out any callback already inside a speaker round trip; anything
        # arriving later will see _stop in _dispatch and become a no-op.
        with self._dispatch_lock:
            pass

    def __enter__(self) -> ControlChannel:
        self.start()
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                client, _address = self._server.accept()
            except TimeoutError:
                continue
            except OSError:
                return  # the socket was closed under us by close()
            with self._client_lock:
                previous = self._client
                self._client = client
            if previous is not None:
                # One client at a time: the component reconnects when its helper
                # is replaced, and a stale socket would keep answering.
                _shutdown_quietly(previous)
            # In its own thread, or accepting the replacement would have to wait
            # for the connection it is replacing to end first.
            threading.Thread(
                target=self._serve,
                args=(client,),
                name="wambridge-control-client",
                daemon=True,
            ).start()

    def _serve(self, client: socket.socket) -> None:
        try:
            client.settimeout(ACCEPT_TIMEOUT)
            with client:
                # One reader for the whole connection. Authenticating from a
                # separate one dropped whatever had already arrived behind the
                # token, and a component that sends both in the same write is
                # exactly what this is for.
                lines = _read_lines(client, self._stop)
                token = next(lines, None)
                # compare_digest, because this is a secret.
                if token is None or not secrets.compare_digest(token, self.token):
                    LOGGER.warning("Control client failed authentication")
                    return
                for line in lines:
                    reply = self._dispatch(line)
                    if reply is not None:
                        client.sendall((reply + "\n").encode("ascii"))
        except OSError:
            LOGGER.debug("Control client disconnected", exc_info=True)

    def _dispatch(self, line: str) -> str | None:
        with self._dispatch_lock:
            if self._stop.is_set():
                return None
            return self._dispatch_locked(line)

    def _dispatch_locked(self, line: str) -> str | None:
        command, _, argument = line.partition(" ")
        if command in {"release", "discard"}:
            callback = self._set_release if command == "release" else self._set_discard
            if argument or callback is None:
                LOGGER.warning("Ignoring malformed playback command %r", line)
                return "error"
            try:
                callback()
            except Exception:  # helper boundary: a failed command is not fatal
                LOGGER.warning("Control %s failed", command, exc_info=True)
            return None
        if command in {"pause", "resume"}:
            if argument or self._set_paused is None:
                LOGGER.warning("Ignoring malformed playback command %r", line)
                return "error"
            # Do the speaker round trip here, but do not make the component wait
            # for an acknowledgement. Pause uses the raw volume already tracked
            # by the helper, writes 0 on the existing 55001 connection, and
            # resume restores the saved level.
            # The physical M5 keeps the same HTTP request alive across that path.
            # Paced silence remains the transport fallback if the control fails.
            try:
                self._set_paused(command == "pause")
            except Exception:  # helper boundary: a failed command is not fatal
                LOGGER.warning("Control %s failed", command, exc_info=True)
            return None
        if command == "sleep":
            if self._set_sleep_timer is None:
                LOGGER.warning("Ignoring unavailable sleep command %r", line)
                return "error"
            try:
                seconds = int(argument)
            except ValueError:
                LOGGER.warning("Ignoring sleep command with argument %r", argument)
                return "error"
            if not 0 <= seconds <= MAX_SLEEP_TIMER_SECONDS:
                LOGGER.warning("Ignoring out-of-range sleep timer %s", seconds)
                return "error"
            try:
                self._set_sleep_timer(seconds)
            except Exception:
                LOGGER.warning("Control sleep %s failed", seconds, exc_info=True)
                return "error"
            return "ok"
        if command != "volume":
            LOGGER.warning("Ignoring unknown control command %r", command)
            return None
        try:
            level = int(argument)
        except ValueError:
            LOGGER.warning("Ignoring volume command with argument %r", argument)
            return None
        if not self._minimum_volume <= level <= self._maximum_volume:
            LOGGER.warning("Ignoring out-of-range volume %s", level)
            return None
        try:
            self._set_volume(level)
        except Exception:  # helper boundary: a failed command is not fatal
            # The stream matters more than the command. A rejected SetVolume
            # must not take playback down with it.
            LOGGER.warning("Control volume %s failed", level, exc_info=True)
        return None


def _shutdown_quietly(client: socket.socket) -> None:
    try:
        client.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        client.close()
    except OSError:
        pass


def _read_lines(client: socket.socket, stop: threading.Event):
    """Yield newline-delimited commands, bounded so a peer cannot exhaust memory."""
    buffer = b""
    while not stop.is_set():
        try:
            chunk = client.recv(MAX_COMMAND_BYTES)
        except TimeoutError:
            continue
        if not chunk:
            return
        buffer += chunk
        while b"\n" in buffer:
            raw, _, buffer = buffer.partition(b"\n")
            yield raw.decode("ascii", errors="replace").strip()
        if len(buffer) > MAX_COMMAND_BYTES:
            # No newline in a full command's worth of bytes: not our protocol.
            LOGGER.warning("Control client sent an oversized line")
            return
