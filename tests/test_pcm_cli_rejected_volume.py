from unittest import TestCase

from wambridge.pcm_cli import PlaybackWatcher
from wambridge.samsung import WamApiError
from wambridge.wam_events import WamEvent


CLIENT_UUID = "00000000-0000-4000-8000-000000000001"


class SilentControlConnection:
    def send(self, **_kwargs) -> None:
        pass


class RejectedVolumeCacheTests(TestCase):
    def test_rejected_routed_volume_invalidates_optimistic_cache(self) -> None:
        watcher = PlaybackWatcher("10.0.0.118", CLIENT_UUID, port=55001)
        watcher._connection = SilentControlConnection()
        watcher._current_volume = 3

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
