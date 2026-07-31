from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch

from wambridge.dlna_cli import (
    _allow_async_timeout,
    _is_timeout_error,
    _missing_request_error,
    _secure_stop,
)
from wambridge.dlna_server import DlnaFileServer
from wambridge.samsung import WamApiError


class DlnaShutdownTests(TestCase):
    @patch("wambridge.dlna_cli.set_playback_control")
    @patch("wambridge.dlna_cli.set_volume")
    @patch("wambridge.dlna_cli.set_mute")
    def test_does_not_mutate_untouched_speaker(
        self,
        mute_mock,
        volume_mock,
        stop_mock,
    ) -> None:
        _secure_stop(
            speaker_ip="10.0.0.118",
            speaker_port=55001,
            previous_volume=None,
            previous_mute=None,
            speaker_touched=False,
            playback_touched=False,
        )

        mute_mock.assert_not_called()
        volume_mock.assert_not_called()
        stop_mock.assert_not_called()

    def test_reports_precise_missing_request_stage(self) -> None:
        with TemporaryDirectory() as directory:
            source = Path(directory) / "track.mp3"
            source.write_bytes(b"test")
            server = DlnaFileServer(source, bind="127.0.0.1")
            try:
                self.assertIn("did not contact", _missing_request_error(server))
                server.description_requested.set()
                self.assertIn("did not Browse", _missing_request_error(server))
                server.browse_requested.set()
                self.assertIn("did not request the MP3", _missing_request_error(server))
            finally:
                server.close()


class DlnaAsyncCommandTests(TestCase):
    def test_timeout_is_detected_through_wrapped_error(self) -> None:
        timeout = TimeoutError("timed out")
        error = RuntimeError("outer")
        error.__cause__ = timeout

        self.assertTrue(_is_timeout_error(error))

    def test_async_timeout_is_tolerated(self) -> None:
        action = Mock(side_effect=WamApiError("timed out"))

        _allow_async_timeout("SetIpInfo", action)

        action.assert_called_once_with()

    def test_non_timeout_error_is_raised(self) -> None:
        with self.assertRaises(WamApiError):
            _allow_async_timeout(
                "SetIpInfo",
                Mock(side_effect=WamApiError("rejected")),
            )
