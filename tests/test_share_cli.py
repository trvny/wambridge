"""Tests for the share playback CLI: state restore, watcher, startup sequence."""

from __future__ import annotations

import io
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

from wambridge.samsung import WamApiError
from wambridge.share import UnsupportedMediaError
from wambridge.share_cli import (
    PlaybackWatcher,
    SpeakerState,
    build_parser,
    local_ip_for,
    main,
    start_share_playback,
)
from wambridge.wam_events import WamEvent


def make_event(method: str, **values: str) -> WamEvent:
    return WamEvent(
        method=method,
        result=None,
        user_identifier=None,
        error_code=None,
        values=dict(values),
        body="",
    )


class LocalIpTests(unittest.TestCase):
    def test_uses_the_route_towards_the_speaker(self) -> None:
        # A UDP connect picks the interface without sending anything.
        self.assertEqual(local_ip_for("127.0.0.1"), "127.0.0.1")


class SpeakerStateTests(unittest.TestCase):
    @patch("wambridge.share_cli.set_volume")
    @patch("wambridge.share_cli.set_mute")
    @patch("wambridge.share_cli.get_mute", return_value=True)
    @patch("wambridge.share_cli.get_volume", return_value=7)
    def test_restores_only_what_it_changed(
        self, _get_volume, _get_mute, mute_mock, volume_mock
    ) -> None:
        state = SpeakerState("10.0.0.118", port=55001)
        state.capture()
        state.set_volume(3)
        volume_mock.reset_mock()

        state.restore()

        volume_mock.assert_called_once_with("10.0.0.118", 7, port=55001)
        mute_mock.assert_not_called()

    @patch("wambridge.share_cli.set_mute")
    @patch("wambridge.share_cli.get_mute", return_value=False)
    @patch("wambridge.share_cli.get_volume", return_value=7)
    def test_a_timed_out_mutation_still_counts_as_touched(
        self, _get_volume, _get_mute, mute_mock
    ) -> None:
        mute_mock.side_effect = [WamApiError("timed out"), None]
        state = SpeakerState("10.0.0.118")
        state.capture()

        with self.assertRaises(WamApiError):
            state.set_mute(True)
        state.restore()

        self.assertEqual(mute_mock.call_count, 2)
        self.assertEqual(mute_mock.call_args.args[1], False)

    @patch("wambridge.share_cli.set_volume", side_effect=WamApiError("timed out"))
    @patch("wambridge.share_cli.get_mute", side_effect=WamApiError("timed out"))
    @patch("wambridge.share_cli.get_volume", side_effect=WamApiError("timed out"))
    def test_unreadable_state_is_not_restored(self, _volume, _mute, set_volume_mock) -> None:
        state = SpeakerState("10.0.0.118")
        state.capture()

        state.restore()

        self.assertIsNone(state.previous_volume)
        self.assertIsNone(state.previous_mute)
        set_volume_mock.assert_not_called()


class PlaybackWatcherTests(unittest.TestCase):
    def _watch(self, events, error=None):
        def fake_listen(_ip, _uuid, *, port, stop, ready):  # noqa: ARG001
            ready.set()
            if error is not None:
                raise error
            yield from events

        with (
            patch("wambridge.share_cli.listen_events", fake_listen),
            PlaybackWatcher("10.0.0.118", "abc", port=55001) as watcher,
        ):
            watcher._thread.join(timeout=5)
            return watcher

    def test_start_event_confirms_playback(self) -> None:
        watcher = self._watch(
            [make_event("MediaBufferStartEvent"), make_event("StartPlaybackEvent")]
        )

        self.assertTrue(watcher.started.is_set())
        self.assertFalse(watcher.failed.is_set())

    def test_error_event_records_its_code(self) -> None:
        watcher = self._watch([make_event("ErrorEvent", errCode="URL_OPEN_FAIL")])

        self.assertTrue(watcher.failed.is_set())
        self.assertEqual(watcher.error_code, "URL_OPEN_FAIL")

    def test_lowercase_error_code_is_accepted(self) -> None:
        watcher = self._watch([make_event("ErrorEvent", errcode="71")])

        self.assertEqual(watcher.error_code, "71")

    def test_a_broken_listener_fails_loudly(self) -> None:
        # Otherwise a broken listener is indistinguishable from a silent speaker.
        watcher = self._watch([], error=OSError("connection reset"))

        self.assertTrue(watcher.failed.is_set())
        self.assertIn("listener failed", watcher.error_code)


class StartSharePlaybackTests(unittest.TestCase):
    def setUp(self) -> None:
        patches = {
            "require_local_playback_mode": None,
            "load_client_uuid": "client-uuid",
            "local_ip_for": "10.0.0.2",
            "register_share_source": None,
            "play_share": None,
            "get_volume": 7,
            "get_mute": False,
            "set_volume": None,
            "set_mute": None,
        }
        self.mocks = {}
        for name, value in patches.items():
            patcher = patch(f"wambridge.share_cli.{name}", return_value=value)
            self.mocks[name] = patcher.start()
            self.addCleanup(patcher.stop)

        self.server = MagicMock()
        self.server.port = 49200
        self.server.object_id = "TRACK.mp3"
        self.server.requested = threading.Event()
        server_patcher = patch(
            "wambridge.share_cli.ShareServer", return_value=self.server
        )
        self.server_class = server_patcher.start()
        self.addCleanup(server_patcher.stop)

        watcher = MagicMock()
        watcher.started = threading.Event()
        watcher.failed = threading.Event()
        watcher.error_code = ""
        watcher.__enter__.return_value = watcher
        watcher.__exit__.return_value = False
        self.watcher = watcher
        watcher_patcher = patch(
            "wambridge.share_cli.PlaybackWatcher", return_value=watcher
        )
        watcher_patcher.start()
        self.addCleanup(watcher_patcher.stop)

    def _start(self, **kwargs):
        return start_share_playback(
            "10.0.0.118", Path("track.mp3"), timeout=0.01, **kwargs
        )

    def test_registers_before_playing_and_returns_the_server(self) -> None:
        self.watcher.started.set()

        server = self._start(volume=3)

        self.assertIs(server, self.server)
        self.mocks["register_share_source"].assert_called_once_with(
            "10.0.0.118", "client-uuid", "10.0.0.2:49200", port=55001
        )
        self.mocks["play_share"].assert_called_once_with(
            "10.0.0.118", device_udn="client-uuid", object_id="TRACK.mp3", port=55001
        )
        self.mocks["set_volume"].assert_called_once_with("10.0.0.118", 3, port=55001)
        self.server.close.assert_not_called()

    def test_volume_is_left_alone_when_omitted(self) -> None:
        self.watcher.started.set()

        self._start()

        self.mocks["set_volume"].assert_not_called()

    def test_reported_error_event_stops_and_restores(self) -> None:
        self.watcher.failed.set()
        self.watcher.error_code = "URL_OPEN_FAIL"

        with self.assertRaisesRegex(WamApiError, "ErrorEvent URL_OPEN_FAIL"):
            self._start(volume=3)

        self.server.close.assert_called_once_with()
        self.mocks["set_volume"].assert_called_with("10.0.0.118", 7, port=55001)

    def test_an_unfetched_object_means_the_command_was_ignored(self) -> None:
        with self.assertRaisesRegex(WamApiError, "never fetched the object"):
            self._start()

    def test_a_fetched_object_without_a_start_event_still_fails(self) -> None:
        self.server.requested.set()

        with self.assertRaisesRegex(WamApiError, "no StartPlaybackEvent arrived"):
            self._start()


class MainTests(unittest.TestCase):
    def test_parser_defaults(self) -> None:
        args = build_parser().parse_args(["10.0.0.118", "track.mp3"])

        self.assertEqual(args.media, Path("track.mp3"))
        self.assertEqual(args.speaker_port, 55001)
        self.assertIsNone(args.volume)
        self.assertEqual(args.timeout, 20.0)

    def test_out_of_range_volume_is_refused_before_contacting_the_speaker(self) -> None:
        with (
            patch("wambridge.share_cli.start_share_playback") as start_mock,
            redirect_stdout(io.StringIO()) as output,
        ):
            status = main(["10.0.0.118", "track.mp3", "--volume", "31"])

        self.assertEqual(status, 2)
        self.assertIn("between 0 and 30", output.getvalue())
        start_mock.assert_not_called()

    def test_expected_failures_exit_1(self) -> None:
        for error in (
            FileNotFoundError("no such file"),
            UnsupportedMediaError("opus is rejected"),
            WamApiError("timed out"),
        ):
            with self.subTest(error=type(error).__name__):
                with (
                    patch("wambridge.share_cli.start_share_playback", side_effect=error),
                    redirect_stdout(io.StringIO()) as output,
                ):
                    self.assertEqual(main(["10.0.0.118", "track.mp3"]), 1)
                self.assertIn(str(error), output.getvalue())

    def test_ctrl_c_closes_the_server(self) -> None:
        server = MagicMock()
        server.requested.wait.side_effect = KeyboardInterrupt

        with (
            patch("wambridge.share_cli.start_share_playback", return_value=server),
            redirect_stdout(io.StringIO()) as output,
        ):
            status = main(["10.0.0.118", "track.mp3", "--volume", "3"])

        self.assertEqual(status, 0)
        self.assertIn("Stopping", output.getvalue())
        server.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
