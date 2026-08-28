from unittest import TestCase
from unittest.mock import patch

from wambridge.pcm_cli import PlaybackWatcher
from wambridge.samsung import WamApiError
from wambridge.stream import StreamError
from wambridge.wam_events import WamEvent


CLIENT_UUID = "00000000-0000-4000-8000-000000000001"


class SilentControlConnection:
    def send(self, **_kwargs) -> None:
        pass


class RejectedVolumeCacheTests(TestCase):
    def _watcher(self, volume: int = 3) -> PlaybackWatcher:
        watcher = PlaybackWatcher("10.0.0.118", CLIENT_UUID, port=55001)
        watcher._connection = SilentControlConnection()
        watcher._current_volume = volume
        return watcher

    def test_rejected_routed_volume_invalidates_optimistic_cache(self) -> None:
        watcher = self._watcher()

        watcher.set_volume(12)
        self.assertEqual(watcher._current_volume, 12)

        watcher._record_response(
            "SetVolume",
            WamEvent(
                method="SetVolume",
                result="ng",
                user_identifier=CLIENT_UUID,
                error_code="3",
                values={},
            ),
        )

        self.assertIsNone(watcher._current_volume)
        with self.assertRaisesRegex(WamApiError, "volume is unknown"):
            watcher.set_pause_volume(True)

    def test_new_volume_setter_supersedes_older_pending_setters(self) -> None:
        watcher = self._watcher()

        watcher.set_volume(12)
        watcher.set_volume(3)

        self.assertEqual(watcher._pending, ["SetVolume"])

    def test_silent_pause_setter_expires_before_external_volume_event(self) -> None:
        watcher = self._watcher()

        with patch("wambridge.pcm_cli._PAUSE_ACK_TIMEOUT", 0.01):
            watcher.set_pause_volume(True)

        self.assertEqual(watcher._pending, [])
        event = WamEvent(
            method="VolumeLevel",
            result="ok",
            user_identifier=CLIENT_UUID,
            error_code=None,
            values={"volume": "4"},
        )
        command = watcher._match_pending(event)
        self.assertIsNone(command)

        watcher._observe_volume_event(event, external=command is None)

        self.assertEqual(watcher._pause_restore_volume, 4)
        self.assertEqual(watcher._current_volume, 0)

    def test_rejected_reapply_zero_fails_the_paused_helper(self) -> None:
        watcher = self._watcher(volume=7)

        with patch("wambridge.pcm_cli._PAUSE_ACK_TIMEOUT", 0.01):
            watcher.set_pause_volume(True)

        external = WamEvent(
            method="VolumeLevel",
            result="ok",
            user_identifier=CLIENT_UUID,
            error_code=None,
            values={"volume": "4"},
        )
        watcher._observe_volume_event(external, external=True)
        rejection = WamEvent(
            method="SetVolume",
            result="ng",
            user_identifier=CLIENT_UUID,
            error_code="3",
            values={},
        )
        command = watcher._match_pending(rejection)
        self.assertEqual(command, "SetVolume")

        watcher._record_response(command, rejection)

        with self.assertRaisesRegex(StreamError, "Could not keep speaker paused"):
            watcher.raise_if_failed()
