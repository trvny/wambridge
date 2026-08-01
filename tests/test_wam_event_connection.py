from unittest import TestCase
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from wambridge.wam_events import WamEventConnection


def command_name(request: bytes) -> str:
    target = request.split(b"\r\n", 1)[0].split(b" ", 2)[1].decode()
    return parse_qs(urlsplit(target).query)["cmd"][0]


class WamEventConnectionTests(TestCase):
    @patch("wambridge.wam_events.socket.create_connection")
    def test_probe_and_playback_use_the_same_socket(self, create_mock) -> None:
        control_socket = Mock()
        create_mock.return_value = control_socket

        with WamEventConnection(
            "10.0.0.118",
            "00000000-0000-4000-8000-000000000001",
            port=55001,
        ) as connection:
            connection.send(
                method="SetUrlPlayback",
                arguments=[
                    ("url", "http://10.0.0.103/live.flac", "cdata"),
                ],
            )

        create_mock.assert_called_once_with(
            ("10.0.0.118", 55001),
            timeout=5.0,
        )
        self.assertEqual(control_socket.sendall.call_count, 2)
        probe, playback = [call.args[0] for call in control_socket.sendall.call_args_list]
        self.assertIn("<name>GetFunc</name>", command_name(probe))
        self.assertIn("<name>SetUrlPlayback</name>", command_name(playback))
        self.assertIn(b"Connection: keep-alive", probe)
        self.assertIn(b"Connection: keep-alive", playback)
        control_socket.close.assert_called_once_with()
