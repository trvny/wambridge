from argparse import Namespace
from contextlib import redirect_stdout
from io import StringIO
from unittest import TestCase
from unittest.mock import patch

from wambridge.cli import _print_status, _run_remote_action, build_parser
from wambridge.samsung import WamPlaybackStatus, WamStatus


class RemoteControlCliTests(TestCase):
    def test_parser_accepts_resume_alias(self) -> None:
        args = build_parser().parse_args(
            ["--device", "M5", "--resume"]
        )

        self.assertTrue(args.play)
        self.assertEqual(args.device, "M5")

    @patch("wambridge.cli.stop_playback")
    def test_runs_standby_action(self, stop_mock) -> None:
        args = Namespace(
            status=False,
            set_volume=None,
            mute=False,
            unmute=False,
            pause=False,
            play=False,
            stop=False,
            standby=True,
        )

        result = _run_remote_action(args, "10.0.0.118", 55001)

        self.assertEqual(result, 0)
        stop_mock.assert_called_once_with(
            "10.0.0.118",
            standby=True,
            port=55001,
        )

    @patch("wambridge.cli.get_status")
    def test_prints_status_snapshot(self, status_mock) -> None:
        status_mock.return_value = WamStatus(
            playback=WamPlaybackStatus(
                function="wifi",
                submode="cp",
                play_status="play",
                cp_name="TuneIn",
                title="Radio Paradise",
            ),
            volume=4,
            muted=False,
            power_status="0",
        )
        output = StringIO()

        with redirect_stdout(output):
            result = _print_status("10.0.0.118", port=55001)

        self.assertEqual(result, 0)
        self.assertIn("provider=TuneIn", output.getvalue())
        self.assertIn("volume=4", output.getvalue())
        self.assertIn("title=Radio Paradise", output.getvalue())
