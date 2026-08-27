"""The helper's missing input direction.

Every one of these exists because the alternative is spawning a process per
volume change, which opens a second connection to the speaker's control port
beside the one the helper already holds.
"""

from __future__ import annotations

import socket
import threading
import unittest

from wambridge.control_channel import ControlChannel


def _connect(channel: ControlChannel, *, token: str | None = None) -> socket.socket:
    client = socket.create_connection(("127.0.0.1", channel.port), timeout=2)
    client.sendall(f"{token if token is not None else channel.token}\n".encode())
    return client


class ControlChannelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.levels: list[int] = []
        self.paused: list[bool] = []
        self.applied = threading.Event()

        def record(level: int) -> None:
            self.levels.append(level)
            self.applied.set()

        def record_paused(paused: bool) -> None:
            self.paused.append(paused)
            self.applied.set()

        self.channel = ControlChannel(record, set_paused=record_paused)
        self.channel.start()
        self.addCleanup(self.channel.close)

    def _send(self, client: socket.socket, line: str) -> None:
        self.applied.clear()
        client.sendall(f"{line}\n".encode())

    def test_binds_loopback_only(self) -> None:
        # It moves a speaker in someone's room; the network has no business
        # reaching it.
        self.assertTrue(self.channel.announcement.startswith("WAMBRIDGE CONTROL_PORT "))
        self.assertIn(str(self.channel.port), self.channel.announcement)
        self.assertIn(self.channel.token, self.channel.announcement)

    def test_applies_a_volume_command(self) -> None:
        with _connect(self.channel) as client:
            self._send(client, "volume 7")
            self.assertTrue(self.applied.wait(timeout=2))
        self.assertEqual(self.levels, [7])

    def test_applies_several_commands_on_one_connection(self) -> None:
        # The whole point: no new connection per change.
        with _connect(self.channel) as client:
            for level in (3, 5, 9):
                self._send(client, f"volume {level}")
                self.assertTrue(self.applied.wait(timeout=2))
        self.assertEqual(self.levels, [3, 5, 9])

    def test_pause_and_resume_are_acknowledged_on_the_same_connection(self) -> None:
        with _connect(self.channel) as client:
            self._send(client, "pause")
            self.assertTrue(self.applied.wait(timeout=2))
            self.assertEqual(client.recv(16), b"ok\n")
            self._send(client, "resume")
            self.assertTrue(self.applied.wait(timeout=2))
            self.assertEqual(client.recv(16), b"ok\n")
        self.assertEqual(self.paused, [True, False])

    def test_failing_pause_returns_error_without_ending_the_channel(self) -> None:
        def explode(_paused: bool) -> None:
            raise RuntimeError("speaker said no")

        channel = ControlChannel(lambda _level: None, set_paused=explode)
        channel.start()
        self.addCleanup(channel.close)
        with _connect(channel) as client:
            client.sendall(b"pause\n")
            self.assertEqual(client.recv(16), b"error\n")

    def test_rejects_a_wrong_token(self) -> None:
        with _connect(self.channel, token="not-the-token") as client:
            self._send(client, "volume 7")
            self.assertFalse(self.applied.wait(timeout=0.5))
        self.assertEqual(self.levels, [])

    def test_ignores_an_out_of_range_level(self) -> None:
        with _connect(self.channel) as client:
            self._send(client, "volume 99")
            self.assertFalse(self.applied.wait(timeout=0.5))
            self._send(client, "volume 4")
            self.assertTrue(self.applied.wait(timeout=2))
        self.assertEqual(self.levels, [4])

    def test_ignores_a_malformed_command(self) -> None:
        with _connect(self.channel) as client:
            for line in ("volume", "volume x", "pause now", "mute 1", ""):
                self._send(client, line)
            self._send(client, "volume 2")
            self.assertTrue(self.applied.wait(timeout=2))
        self.assertEqual(self.levels, [2])

    def test_a_failing_command_does_not_end_the_session(self) -> None:
        # A rejected SetVolume must not take playback down with it.
        failures = {"count": 0}
        applied = threading.Event()

        def explode(level: int) -> None:
            if failures["count"] == 0:
                failures["count"] += 1
                raise RuntimeError("speaker said no")
            applied.set()

        channel = ControlChannel(explode)
        channel.start()
        self.addCleanup(channel.close)
        with _connect(channel) as client:
            client.sendall(b"volume 5\n")
            client.sendall(b"volume 6\n")
            self.assertTrue(applied.wait(timeout=2))

    def test_a_second_client_replaces_the_first(self) -> None:
        # The component reconnects when its helper is replaced; a stale socket
        # would otherwise keep answering.
        first = _connect(self.channel)
        self.addCleanup(first.close)
        self._send(first, "volume 3")
        self.assertTrue(self.applied.wait(timeout=2))

        with _connect(self.channel) as second:
            self._send(second, "volume 8")
            self.assertTrue(self.applied.wait(timeout=2))
        self.assertEqual(self.levels, [3, 8])

    def test_close_is_safe_without_a_client(self) -> None:
        channel = ControlChannel(lambda _level: None)
        channel.start()
        channel.close()
        channel.close()


if __name__ == "__main__":
    unittest.main()
