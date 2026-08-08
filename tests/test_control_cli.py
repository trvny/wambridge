import argparse
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import call, patch

from wambridge.control_cli import (
    ControlError,
    Target,
    change_volume,
    emergency_stop,
    raw_volume,
    resolve_target,
    set_safe_volume,
    standby,
    wait_until_released,
)
from wambridge.samsung import WamApiError


class ControlParserTests(TestCase):
    def test_parses_raw_m5_volume(self) -> None:
        self.assertEqual(raw_volume("0"), 0)
        self.assertEqual(raw_volume("30"), 30)
        with self.assertRaisesRegex(argparse.ArgumentTypeError, "between 0 and 30"):
            raw_volume("31")


class TargetResolutionTests(TestCase):
    def test_uses_direct_speaker(self) -> None:
        args = SimpleNamespace(speaker="10.0.0.118", port=55001)

        self.assertEqual(resolve_target(args), Target("10.0.0.118", 55001))

    @patch("wambridge.control_cli.resolve_device")
    @patch("wambridge.control_cli.ProfileStore")
    def test_resolves_default_alias(self, store_class, resolve_mock) -> None:
        store = store_class.return_value
        resolve_mock.return_value = SimpleNamespace(last_ip="10.0.0.118", port=55001)
        args = SimpleNamespace(
            speaker=None,
            device=None,
            config=None,
            discovery_timeout=4.0,
            interfaces=None,
            no_scan=False,
        )

        target = resolve_target(args)

        self.assertEqual(target, Target("10.0.0.118", 55001))
        resolve_mock.assert_called_once_with(
            "M5",
            store=store,
            timeout=4.0,
            local_addresses=None,
            scan=True,
        )


class EmergencyControlTests(TestCase):
    @patch("wambridge.control_cli.get_volume", return_value=3)
    @patch("wambridge.control_cli.get_mute", return_value=False)
    @patch("wambridge.control_cli.set_volume")
    @patch("wambridge.control_cli.set_mute")
    @patch("wambridge.control_cli.stop_playback")
    def test_emergency_stop_retries_then_recovers(
        self,
        stop_mock,
        mute_mock,
        volume_mock,
        _get_mute_mock,
        _get_volume_mock,
    ) -> None:
        stop_mock.side_effect = [WamApiError("timed out"), object()]

        lines = emergency_stop(
            Target("10.0.0.118", 55001),
            safe_volume=3,
            retries=2,
            retry_delay=0,
        )

        self.assertIn("action=emergency-stop", lines)
        self.assertIn("verified=yes", lines)
        self.assertEqual(stop_mock.call_count, 2)
        mute_mock.assert_called_once_with(
            "10.0.0.118",
            False,
            port=55001,
            timeout=3.0,
        )
        volume_mock.assert_called_once_with(
            "10.0.0.118",
            3,
            port=55001,
            timeout=3.0,
        )

    @patch("wambridge.control_cli.get_volume", return_value=9)
    @patch("wambridge.control_cli.get_mute", return_value=True)
    @patch("wambridge.control_cli.set_volume")
    @patch("wambridge.control_cli.set_mute")
    @patch("wambridge.control_cli.stop_playback")
    def test_emergency_stop_rejects_verified_wrong_state(
        self,
        _stop_mock,
        _mute_mock,
        _volume_mock,
        _get_mute_mock,
        _get_volume_mock,
    ) -> None:
        with self.assertRaisesRegex(ControlError, "could not be verified"):
            emergency_stop(
                Target("10.0.0.118", 55001),
                safe_volume=3,
                retries=1,
                retry_delay=0,
            )

    @patch("wambridge.control_cli.get_mute", side_effect=WamApiError("timed out"))
    @patch("wambridge.control_cli.set_mute")
    @patch("wambridge.control_cli.stop_playback")
    def test_standby_accepts_unavailable_verification_after_sent_commands(
        self,
        stop_mock,
        mute_mock,
        _get_mute_mock,
    ) -> None:
        lines = standby(
            Target("10.0.0.118", 55001),
            retries=1,
            retry_delay=0,
        )

        self.assertIn("action=standby", lines)
        self.assertIn("verified=no", lines)
        stop_mock.assert_called_once_with(
            "10.0.0.118",
            standby=True,
            port=55001,
            timeout=3.0,
        )
        mute_mock.assert_called_once_with(
            "10.0.0.118",
            True,
            port=55001,
            timeout=3.0,
        )


class StandbyReleaseTests(TestCase):
    """Standby must say whether anything is still holding the speaker.

    A silent standby is how the M5 was left lit for hours: the commands landed,
    nothing complained, and a killed session was still attached.
    """

    @patch("wambridge.control_cli.attached_connections_to", return_value=0)
    @patch("wambridge.control_cli.get_mute", return_value=True)
    @patch("wambridge.control_cli.set_mute")
    @patch("wambridge.control_cli.stop_playback")
    def test_reports_nothing_attached(self, _stop, _set_mute, _get_mute, _held) -> None:
        lines = standby(Target("10.0.0.118", 55001), retries=1, retry_delay=0)

        self.assertIn("holding=0", lines)
        self.assertFalse([line for line in lines if line.startswith("warning=")])

    @patch("wambridge.control_cli.attached_connections_to", return_value=2)
    @patch("wambridge.control_cli.get_mute", return_value=True)
    @patch("wambridge.control_cli.set_mute")
    @patch("wambridge.control_cli.stop_playback")
    def test_warns_when_connections_remain(self, _stop, _set_mute, _get_mute, _held) -> None:
        # The hold never clears here, so the release timeout has to be injected:
        # patching time.sleep alone would busy-spin until the real deadline.
        lines = standby(
            Target("10.0.0.118", 55001),
            retries=1,
            retry_delay=0,
            release_timeout=0,
        )

        self.assertIn("holding=2", lines)
        self.assertTrue(
            any("still attached" in line for line in lines),
            f"expected a warning naming the hold, got {lines}",
        )
        # The mute and the stop both landed, so this is information, not failure.
        self.assertIn("verified=yes", lines)

    @patch("wambridge.control_cli.attached_connections_to", return_value=None)
    @patch("wambridge.control_cli.get_mute", return_value=True)
    @patch("wambridge.control_cli.set_mute")
    @patch("wambridge.control_cli.stop_playback")
    def test_unreadable_table_is_unknown_not_zero(
        self, _stop, _set_mute, _get_mute, _held
    ) -> None:
        lines = standby(Target("10.0.0.118", 55001), retries=1, retry_delay=0)

        self.assertIn("holding=unknown", lines)
        self.assertNotIn("holding=0", lines)

    @patch("wambridge.control_cli.time.sleep")
    @patch("wambridge.control_cli.attached_connections_to", side_effect=[2, 1, 0])
    def test_waits_for_a_tearing_down_helper_to_let_go(self, held_mock, _sleep) -> None:
        self.assertEqual(wait_until_released("10.0.0.118", timeout=5, poll=0), 0)
        self.assertEqual(held_mock.call_count, 3)

    @patch("wambridge.control_cli.time.sleep")
    @patch("wambridge.control_cli.attached_connections_to", return_value=1)
    def test_gives_up_and_reports_the_hold(self, _held_mock, _sleep) -> None:
        self.assertEqual(wait_until_released("10.0.0.118", timeout=0, poll=0), 1)


class VolumeControlTests(TestCase):
    @patch("wambridge.control_cli.set_volume")
    @patch("wambridge.control_cli.get_volume", return_value=30)
    def test_volume_up_clamps_to_raw_maximum(self, _get_mock, set_mock) -> None:
        lines = change_volume(
            Target("10.0.0.118", 55001),
            1,
            retries=1,
            retry_delay=0,
        )

        self.assertIn("volume=30", lines)
        set_mock.assert_called_once_with(
            "10.0.0.118",
            30,
            port=55001,
            timeout=3.0,
        )

    @patch("wambridge.control_cli.set_volume")
    def test_sets_safe_volume_without_unmuting(self, set_mock) -> None:
        lines = set_safe_volume(
            Target("10.0.0.118", 55001),
            3,
            retries=1,
            retry_delay=0,
        )

        self.assertEqual(lines, ["action=safe-volume", "volume=3"])
        self.assertEqual(
            set_mock.mock_calls,
            [call("10.0.0.118", 3, port=55001, timeout=3.0)],
        )
