"""Tests for the event listener CLI: argument rules, target choice, formatting."""

from __future__ import annotations

import argparse
import io
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch

from wambridge.event_cli import (
    build_parser,
    client_uuid,
    format_event,
    main,
    run,
    select_speaker,
)
from wambridge.profiles import ProfileError
from wambridge.wam_events import WamEvent


def make_event(**overrides: object) -> WamEvent:
    fields: dict[str, object] = {
        "method": "PlayStatus",
        "result": "ok",
        "user_identifier": "public",
        "error_code": None,
        "values": {},
        "body": "<pwron>on</pwron>",
    }
    fields.update(overrides)
    return WamEvent(**fields)  # type: ignore[arg-type]


class ClientUuidTests(unittest.TestCase):
    def test_normalizes_a_valid_uuid(self) -> None:
        self.assertEqual(
            client_uuid("00000000000040008000000000000001"),
            "00000000-0000-4000-8000-000000000001",
        )

    def test_rejects_a_non_uuid(self) -> None:
        with self.assertRaisesRegex(argparse.ArgumentTypeError, "valid UUID"):
            client_uuid("not-a-uuid")


class ParserTests(unittest.TestCase):
    def test_defaults(self) -> None:
        args = build_parser().parse_args([])

        self.assertIsNone(args.speaker)
        self.assertIsNone(args.client_uuid)
        self.assertEqual(args.port, 55001)
        self.assertEqual(args.duration, 0.0)
        self.assertFalse(args.raw)

    def test_speaker_and_device_are_mutually_exclusive(self) -> None:
        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["--speaker", "10.0.0.118", "--device", "M5"])

    def test_interfaces_accumulate(self) -> None:
        args = build_parser().parse_args(["--interface", "10.0.0.5", "--interface", "10.0.0.6"])

        self.assertEqual(args.interfaces, ["10.0.0.5", "10.0.0.6"])


class SelectSpeakerTests(unittest.TestCase):
    def _args(self, **overrides: object) -> argparse.Namespace:
        defaults: dict[str, object] = {
            "speaker": None,
            "device": None,
            "port": 55001,
            "discovery_timeout": 4.0,
            "interfaces": None,
            "no_scan": False,
        }
        defaults.update(overrides)
        return argparse.Namespace(**defaults)

    def test_direct_speaker_skips_discovery(self) -> None:
        with patch("wambridge.cli_common.discover") as discover_mock:
            self.assertEqual(
                select_speaker(self._args(speaker="10.0.0.118"), store=object()),
                ("10.0.0.118", 55001),
            )
        discover_mock.assert_not_called()

    @patch("wambridge.cli_common.resolve_device")
    def test_saved_device_uses_its_own_port(self, resolve_mock) -> None:
        resolve_mock.return_value = SimpleNamespace(
            alias="M5", device_id="abc", last_ip="10.0.0.118", port=55002
        )
        store = object()

        self.assertEqual(
            select_speaker(self._args(device="M5"), store), ("10.0.0.118", 55002)
        )
        resolve_mock.assert_called_once_with(
            "M5", store=store, timeout=4.0, local_addresses=None, scan=True
        )

    @patch("wambridge.cli_common.discover", return_value=[SimpleNamespace(ip="10.0.0.118")])
    def test_single_discovered_speaker_is_used(self, discover_mock) -> None:
        self.assertEqual(select_speaker(self._args(no_scan=True), object()), ("10.0.0.118", 55001))
        discover_mock.assert_called_once_with(
            timeout=4.0, local_addresses=None, port=55001, scan=False
        )

    @patch("wambridge.cli_common.discover", return_value=[])
    def test_no_speaker_found(self, _discover_mock) -> None:
        with self.assertRaisesRegex(RuntimeError, "No Samsung WAM speaker found"):
            select_speaker(self._args(), object())

    @patch(
        "wambridge.cli_common.discover",
        return_value=[SimpleNamespace(ip="10.0.0.118"), SimpleNamespace(ip="10.0.0.119")],
    )
    def test_ambiguous_discovery_names_the_candidates(self, _discover_mock) -> None:
        with self.assertRaisesRegex(RuntimeError, "10.0.0.118, 10.0.0.119"):
            select_speaker(self._args(), object())


class FormatEventTests(unittest.TestCase):
    def test_summary_holds_method_result_and_values(self) -> None:
        summary = format_event(
            make_event(error_code="71", values={"volume": "3", "user_identifier": "public"})
        )

        self.assertIn("PlayStatus", summary)
        self.assertIn("result=ok", summary)
        self.assertIn("errCode=71", summary)
        self.assertIn("user=public", summary)
        self.assertIn("volume=3", summary)
        self.assertEqual(summary.count("public"), 1)

    def test_unknown_method_is_labelled(self) -> None:
        summary = format_event(make_event(method=None, result=None, user_identifier=None))

        self.assertIn("Unknown", summary)
        self.assertNotIn("result=", summary)

    def test_long_values_are_collapsed_and_truncated(self) -> None:
        summary = format_event(make_event(values={"title": "a b\n  c"}))
        self.assertIn("title=a b c", summary)

        long_summary = format_event(make_event(values={"title": "x" * 200}))
        self.assertIn(f"title={'x' * 117}...", long_summary)


class RunTests(unittest.TestCase):
    def _args(self, **overrides: object) -> argparse.Namespace:
        defaults: dict[str, object] = {
            "speaker": "10.0.0.118",
            "device": None,
            "port": 55001,
            "client_uuid": "00000000-0000-4000-8000-000000000001",
            "duration": 1.5,
            "raw": False,
            "config": None,
            "discovery_timeout": 4.0,
            "interfaces": None,
            "no_scan": False,
        }
        defaults.update(overrides)
        return argparse.Namespace(**defaults)

    def test_negative_duration_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "duration cannot be negative"):
            run(self._args(duration=-1.0))

    @patch("wambridge.event_cli.ProfileStore")
    @patch("wambridge.event_cli.listen_events")
    def test_prints_one_summary_per_event(self, listen_mock, _store_class) -> None:
        listen_mock.return_value = iter([make_event()])
        output = io.StringIO()

        with redirect_stdout(output):
            self.assertEqual(run(self._args()), 0)

        printed = output.getvalue()
        self.assertIn("mobileUUID 00000000-0000-4000-8000-000000000001", printed)
        self.assertIn("PlayStatus", printed)
        self.assertNotIn("<pwron>", printed)
        listen_mock.assert_called_once_with(
            "10.0.0.118",
            "00000000-0000-4000-8000-000000000001",
            port=55001,
            duration=1.5,
        )

    @patch("wambridge.event_cli.ProfileStore")
    @patch("wambridge.event_cli.listen_events")
    def test_raw_mode_also_prints_the_body(self, listen_mock, _store_class) -> None:
        listen_mock.return_value = iter([make_event()])
        output = io.StringIO()

        with redirect_stdout(output):
            run(self._args(raw=True))

        self.assertIn("<pwron>on</pwron>", output.getvalue())

    @patch("wambridge.event_cli.ProfileStore")
    @patch("wambridge.event_cli.listen_events")
    def test_generates_an_identity_when_none_is_given(self, listen_mock, _store_class) -> None:
        listen_mock.return_value = iter([])

        with redirect_stdout(io.StringIO()):
            run(self._args(client_uuid=None))

        self.assertNotEqual(listen_mock.call_args.args[1], "")


class MainTests(unittest.TestCase):
    @patch("wambridge.event_cli.run", return_value=0)
    def test_returns_the_run_status(self, run_mock) -> None:
        self.assertEqual(main(["--speaker", "10.0.0.118"]), 0)
        self.assertEqual(run_mock.call_args.args[0].speaker, "10.0.0.118")

    @patch("wambridge.event_cli.run", side_effect=KeyboardInterrupt)
    def test_ctrl_c_exits_130(self, _run_mock) -> None:
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main([]), 130)

    @patch("wambridge.event_cli.run", side_effect=ProfileError("no such device"))
    def test_expected_errors_exit_1(self, _run_mock) -> None:
        self.assertEqual(main([]), 1)


if __name__ == "__main__":
    unittest.main()
