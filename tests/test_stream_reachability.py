"""The guard that refuses a stream URL nothing is serving.

The failure this exists for is the one the project cannot undo from software: a
``SetUrlPlayback`` aimed at an address the speaker cannot pull wedges the control
port, and the two commands that would recover it - ``SetUrlPlayback`` and
``SetStopPlayback`` - are precisely the two that stop answering. Only a power
cycle clears it, so the refusal has to happen before the command leaves.
"""

import socket
import socketserver
import threading
from unittest import TestCase
from unittest.mock import patch

from wambridge.samsung import (
    WamUnreachableStreamError,
    assert_stream_reachable,
    play_url,
)


class _Listener:
    """A socket that accepts and says nothing, like a stream server not yet asked."""

    def __enter__(self) -> str:
        self._server = socketserver.TCPServer(
            ("127.0.0.1", 0), socketserver.BaseRequestHandler
        )
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return f"http://127.0.0.1:{self._server.server_address[1]}/stream.wav"

    def __exit__(self, *_exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


class StreamReachabilityTests(TestCase):
    def test_accepts_a_port_something_is_listening_on(self) -> None:
        with _Listener() as url:
            assert_stream_reachable(url)

    def test_refuses_a_port_with_nothing_behind_it(self) -> None:
        with self.assertRaises(WamUnreachableStreamError) as raised:
            assert_stream_reachable("http://127.0.0.1:1/stream.wav")

        self.assertIn("nothing is listening", str(raised.exception))
        self.assertIn("power-cycles", str(raised.exception))

    def test_refuses_a_scheme_the_speaker_cannot_fetch(self) -> None:
        with self.assertRaises(WamUnreachableStreamError):
            assert_stream_reachable("file:///tmp/stream.wav")

    def test_refuses_a_url_naming_no_host(self) -> None:
        with self.assertRaises(WamUnreachableStreamError):
            assert_stream_reachable("http:///stream.wav")

    def test_never_sends_a_request_to_the_stream_server(self) -> None:
        """The relay serves one consumer; a probe that asked for the body would be a second."""
        received: list[bytes] = []

        class _Recorder(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                self.request.settimeout(0.5)
                try:
                    received.append(self.request.recv(1024))
                except OSError:
                    received.append(b"")

        server = socketserver.TCPServer(("127.0.0.1", 0), _Recorder)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            assert_stream_reachable(
                f"http://127.0.0.1:{server.server_address[1]}/stream.wav"
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.assertEqual(received, [b""])

    def test_uses_the_scheme_default_port_when_the_url_omits_one(self) -> None:
        seen: list[tuple[str, int]] = []

        def _record(address, timeout):  # noqa: ANN001 - mirrors socket.create_connection
            seen.append(address)
            raise OSError("refused")

        with patch.object(socket, "create_connection", _record):
            for url in (
                "http://speaker.local/stream.wav",
                "https://speaker.local/stream.wav",
            ):
                with self.assertRaises(WamUnreachableStreamError):
                    assert_stream_reachable(url)

        self.assertEqual(seen, [("speaker.local", 80), ("speaker.local", 443)])


class PlayUrlGuardTests(TestCase):
    @patch("wambridge.samsung.request")
    def test_play_url_refuses_before_the_speaker_hears_anything(self, request_mock) -> None:
        with self.assertRaises(WamUnreachableStreamError):
            play_url("10.0.0.104", "http://127.0.0.1:1/stream.wav")

        request_mock.assert_not_called()

    @patch("wambridge.samsung.request")
    def test_play_url_can_be_asked_to_skip_the_check(self, request_mock) -> None:
        play_url(
            "10.0.0.104",
            "http://127.0.0.1:1/stream.wav",
            verify_reachable=False,
        )

        request_mock.assert_called_once()
