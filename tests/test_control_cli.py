import argparse
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import call, patch

from wambridge.control_cli import (
    ControlError,
    Target,
    build_parser,
    change_volume,
    emergency_stop,
    raw_volume,
    resolve_target,
    run,
    set_exact_volume,
    set_safe_volume,
    standby,
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

    @patch("wambridge.control_cli.get_volume")
    @patch("wambridge.control_cli.set_volume")
    def test_set_volume_does_not_read_the_speaker_first(
        self,
        set_mock,
        get_mock,
    ) -> None:
        # The slider already knows the level it wants. Reading first would
        # double the traffic a drag puts on the shared control port.
        lines = set_exact_volume(
            Target("10.0.0.118", 55001),
            7,
            action="set-volume",
            retries=1,
            retry_delay=0,
        )

        self.assertEqual(lines, ["action=set-volume", "volume=7"])
        get_mock.assert_not_called()
        self.assertEqual(
            set_mock.mock_calls,
            [call("10.0.0.118", 7, port=55001, timeout=3.0)],
        )

    @patch("wambridge.control_cli.set_volume", side_effect=WamApiError("nope"))
    def test_set_volume_failure_is_an_error(self, _set_mock) -> None:
        with self.assertRaisesRegex(ControlError, "Could not set volume"):
            set_exact_volume(
                Target("10.0.0.118", 55001),
                7,
                action="set-volume",
                retries=1,
                retry_delay=0,
            )

    def test_set_volume_level_stays_inside_the_measured_range(self) -> None:
        parser = build_parser()

        self.assertEqual(parser.parse_args(["set-volume", "--level", "30"]).level, 30)
        with self.assertRaises(SystemExit):
            parser.parse_args(["set-volume", "--level", "31"])

    def test_set_volume_without_a_level_is_rejected(self) -> None:
        args = build_parser().parse_args(["set-volume", "--speaker", "10.0.0.118"])
        with self.assertRaisesRegex(ControlError, "requires --level"):
            run(args)
