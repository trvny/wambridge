from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch

from wambridge.dms_cli import DEFAULT_DMS_PORT, _run_ladder, build_parser
from wambridge.dms_probe import SamsungDmsServer


class DmsCliTests(TestCase):
    def test_uses_fixed_samsung_port_by_default(self) -> None:
        args = build_parser().parse_args(["track.mp3", "--speaker", "10.0.0.118"])
        self.assertEqual(args.http_port, DEFAULT_DMS_PORT)
        self.assertEqual(DEFAULT_DMS_PORT, 3921)

    @patch("wambridge.dms_cli._start_folder_fallback", return_value=True)
    @patch("wambridge.dms_cli._start_share", return_value=True)
    @patch("wambridge.dms_cli._register_server", return_value=True)
    def test_ladder_reaches_folder_fallback_after_no_contact(
        self,
        register_mock,
        share_mock,
        folder_mock,
    ) -> None:
        ssdp = Mock()
        with TemporaryDirectory() as directory:
            source = Path(directory) / "track.mp3"
            source.write_bytes(b"test")
            server = SamsungDmsServer(source, bind="127.0.0.1", port=0)
            try:
                with patch(
                    "wambridge.dms_cli._wait_for_contact",
                    side_effect=[False, True],
                ):
                    accepted = _run_ladder(
                        "10.0.0.118",
                        55001,
                        server,
                        "10.0.0.103",
                        ssdp,
                    )
            finally:
                server.close()

        self.assertTrue(accepted)
        register_mock.assert_called_once()
        share_mock.assert_called_once()
        folder_mock.assert_called_once()
        self.assertEqual(ssdp.announce.call_count, 2)
