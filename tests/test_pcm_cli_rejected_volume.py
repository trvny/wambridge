from unittest import TestCase
from unittest.mock import patch

from wambridge.pcm_cli import PlaybackWatcher
from wambridge.samsung import WamApiError
from wambridge.stream import StreamError
from wambridge.wam_events import WamEvent

CLIENT_UUID = "00000000-0000-4000-8000-000000000001"


class SilentControlConnection:
    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []

    def send(self, **kwargs) -> None:
        self.sent.append(kwargs)




class ImmediateRejectingControlConnection:
    def __init__(self, watcher: PlaybackWatcher) -> None:
        self._watcher = watcher

    def send(self, *, method: str, **_kwargs) -> None:
        event = WamEvent(
            method=method,
            result="ng",
            user_identifier=CLIENT_UUID,
            error_code="3",
            values={},
        )
        command = self._watcher._match_pending(event)
        if command is not None:
            self._watcher._record_response(command, event)


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

    def test_fast_rejection_cannot_be_overwritten_by_optimistic_cache(self) -> None:
        watcher = self._watcher()
        watcher._connection = ImmediateRejectingControlConnection(watcher)

        watcher.set_volume(12)

        self.assertIsNone(watcher._current_volume)

    def test_mismatched_volume_event_stays_external_while_zero_is_pending(self) -> None:
        watcher = self._watcher(volume=7)
        with patch("wambridge.pcm_cli._PAUSE_ACK_TIMEOUT", 0.01):
            watcher.set_pause_volume(True)

        first_external = WamEvent(
            method="VolumeLevel",
            result="ok",
            user_identifier=CLIENT_UUID,
            error_code=None,
            values={"volume": "4"},
        )
        watcher._observe_volume_event(first_external, external=True)
        self.assertEqual(watcher._pending_volume_level, 0)

        second_external = WamEvent(
            method="VolumeLevel",
            result="ok",
            user_identifier=CLIENT_UUID,
            error_code=None,
            values={"volume": "5"},
        )
        command = watcher._match_pending(second_external)
        self.assertIsNone(command)

        watcher._observe_volume_event(second_external, external=True)

        self.assertEqual(watcher._pause_restore_volume, 5)
        self.assertEqual(watcher._current_volume, 0)
        self.assertEqual(watcher._pending, ["SetVolume"])
        self.assertEqual(watcher._pending_volume_level, 0)

    def test_initial_pause_rejection_keeps_restore_debt_without_async_failure(self) -> None:
        watcher = self._watcher(volume=7)
        watcher._connection = ImmediateRejectingControlConnection(watcher)

        with self.assertRaisesRegex(WamApiError, "rejected SetVolume"):
            watcher.set_pause_volume(True)

        self.assertEqual(watcher._error, "")
        self.assertFalse(watcher._pause_volume_active)
        self.assertEqual(watcher._pause_restore_volume, 7)
        watcher.raise_if_failed()

        # The reply may have belonged to an older superseded slider, so keep a
        # safety restore target. Routed pause itself is inactive, however: a later
        # slider must still reach the speaker and become the newest restore target.
        connection = SilentControlConnection()
        watcher._connection = connection
        watcher.set_volume(12)
        self.assertEqual(watcher._current_volume, 12)
        self.assertEqual(watcher._pause_restore_volume, 12)
        self.assertEqual(watcher._pending, ["SetVolume"])

        watcher.set_pause_volume(False)
        sent_levels = [
            call["arguments"][0][1]
            for call in connection.sent
            if call.get("method") == "SetVolume"
        ]
        self.assertEqual(sent_levels, [12, 12])
        self.assertIsNone(watcher._pause_restore_volume)

    def test_external_change_during_initial_pause_wait_is_rezeroed_before_unlock(self) -> None:
        watcher = self._watcher(volume=7)
        connection = watcher._connection
        self.assertIsInstance(connection, SilentControlConnection)
        external = WamEvent(
            method="VolumeLevel",
            result="ok",
            user_identifier=CLIENT_UUID,
            error_code=None,
            values={"volume": "4"},
        )

        def observe_during_wait(_method: str, *, timeout: float) -> None:
            self.assertGreater(timeout, 0)
            watcher._observe_volume_event(external, external=True)
            self.assertTrue(watcher._pause_rezero_pending)
            return None

        with patch.object(watcher, "wait_for_response", side_effect=observe_during_wait):
            watcher.set_pause_volume(True)

        sent_levels = [
            call["arguments"][0][1]
            for call in connection.sent
            if call.get("method") == "SetVolume"
        ]
        self.assertEqual(sent_levels, [0, 0])
        self.assertEqual(watcher._pause_restore_volume, 4)
        self.assertEqual(watcher._current_volume, 0)
        self.assertFalse(watcher._pause_rezero_pending)

    def test_deferred_rezero_at_unlock_boundary_is_drained(self) -> None:
        watcher = self._watcher(volume=7)
        connection = watcher._connection
        self.assertIsInstance(connection, SilentControlConnection)
        watcher._pause_volume_active = True
        watcher._pause_restore_volume = 4
        watcher._pause_rezero_pending = True
        watcher._current_volume = 4

        watcher._drain_pause_rezero_after_unlock()

        sent_levels = [
            call["arguments"][0][1]
            for call in connection.sent
            if call.get("method") == "SetVolume"
        ]
        self.assertEqual(sent_levels, [0])
        self.assertEqual(watcher._current_volume, 0)
        self.assertFalse(watcher._pause_rezero_pending)

    def test_external_change_during_restore_is_not_overwritten_after_wait(self) -> None:
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

        def observe_during_restore(_method: str, *, timeout: float) -> None:
            self.assertGreater(timeout, 0)
            watcher._observe_volume_event(external, external=True)
            return None

        with patch.object(watcher, "wait_for_response", side_effect=observe_during_restore):
            watcher.set_pause_volume(False)

        self.assertFalse(watcher._pause_volume_active)
        self.assertIsNone(watcher._pause_restore_volume)
        self.assertEqual(watcher._current_volume, 4)

    def test_external_rezero_is_cancelled_when_restore_owns_write_lane(self) -> None:
        watcher = self._watcher(volume=7)
        with patch("wambridge.pcm_cli._PAUSE_ACK_TIMEOUT", 0.01):
            watcher.set_pause_volume(True)

        watcher._volume_write_lock.acquire()
        try:
            event = WamEvent(
                method="VolumeLevel",
                result="ok",
                user_identifier=CLIENT_UUID,
                error_code=None,
                values={"volume": "4"},
            )
            watcher._observe_volume_event(event, external=True)
        finally:
            watcher._volume_write_lock.release()

        # Resume/teardown owns the newer operation, so the listener must not
        # enqueue a stale raw-0 write behind it. The external level remains the
        # restore target and no SetVolume request was left pending.
        self.assertEqual(watcher._pause_restore_volume, 4)
        self.assertEqual(watcher._current_volume, 4)
        self.assertEqual(watcher._pending, [])

        with patch("wambridge.pcm_cli._PAUSE_ACK_TIMEOUT", 0.01):
            watcher.set_pause_volume(False)

        self.assertIsNone(watcher._pause_restore_volume)
        self.assertEqual(watcher._current_volume, 4)
