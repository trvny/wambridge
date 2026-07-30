from io import BytesIO, StringIO
from threading import Event
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, call, patch

from wambridge.pcm_cli import build_parser, run
from wambridge.samsung import WamApiError
from wambridge.stream import StreamError


class FakePcmServer:
    def __init__(self, *_args, **_kwargs) -> None:
        self.request_started = Event()
        self.encoder_started = Event()
        self.audio_started = Event()
        self.request_finished = Event()
        self.error = None
        self.closed = False
        self.released = False

    def start(self) -> None:
        self.request_started.set()
        self.encoder_started.set()
        self.audio_started.set()
        self.request_finished.set()

    def url(self, host: str) -> str:
        return f"http://{host}:1234/stream/test.flac"

    def release_audio(self) -> None:
        self.released = True

    def close(self) -> None:
        self.closed = True


class PcmCliTests(TestCase):
    def _args(self, *extra: str):
        return build_parser().parse_args(
            [
                "--device",
                "M5",
                "--sample-rate",
                "48000",
                "--channels",
                "2",
                *extra,
            ]
        )

    @patch("wambridge.pcm_cli.play_url")
    @patch("wambridge.pcm_cli.set_volume")
    @patch("wambridge.pcm_cli.get_volume", return_value=37)
    @patch("wambridge.pcm_cli.local_ip_for", return_value="10.0.0.103")
    @patch(
        "wambridge.pcm_cli.probe",
        return_value=SimpleNamespace(method="SpkName"),
    )
    @patch("wambridge.pcm_cli.select_speaker", return_value=("10.0.0.118", 55001))
    @patch("wambridge.pcm_cli.PcmAudioStreamServer", FakePcmServer)
    def test_emits_ready_then_playing(
        self,
        _select_mock,
        _probe_mock,
        _local_ip_mock,
        _get_volume_mock,
        volume_mock,
        play_url_mock,
    ) -> None:
        protocol = StringIO()
        sequence = Mock()
        sequence.attach_mock(volume_mock, "set_volume")
        sequence.attach_mock(play_url_mock, "play_url")

        result = run(
            self._args("--volume", "4"),
            pcm_input=BytesIO(),
            protocol_output=protocol,
        )

        self.assertEqual(result, 0)
        self.assertEqual(
            protocol.getvalue().splitlines(),
            ["WAMBRIDGE READY", "WAMBRIDGE PLAYING volume=4"],
        )
        self.assertEqual(
            volume_mock.call_args_list,
            [
                call("10.0.0.118", 0, port=55001),
                call("10.0.0.118", 4, port=55001),
                call("10.0.0.118", 4, port=55001),
            ],
        )
        self.assertEqual(
            sequence.mock_calls[:2],
            [
                call.set_volume("10.0.0.118", 0, port=55001),
                call.play_url(
                    "10.0.0.118",
                    "http://10.0.0.103:1234/stream/test.flac",
                    port=55001,
                ),
            ],
        )

    @patch("wambridge.pcm_cli.play_url")
    @patch("wambridge.pcm_cli.set_volume")
    @patch("wambridge.pcm_cli.get_volume", return_value=4)
    @patch("wambridge.pcm_cli.local_ip_for", return_value="10.0.0.103")
    @patch(
        "wambridge.pcm_cli.probe",
        return_value=SimpleNamespace(method="SpkName"),
    )
    @patch("wambridge.pcm_cli.select_speaker", return_value=("10.0.0.118", 55001))
    @patch("wambridge.pcm_cli.PcmAudioStreamServer", FakePcmServer)
    def test_accepts_pause_event_during_url_handoff(
        self,
        _select_mock,
        _probe_mock,
        _local_ip_mock,
        _get_volume_mock,
        _volume_mock,
        play_url_mock,
    ) -> None:
        play_url_mock.side_effect = WamApiError(
            "Samsung WAM rejected PausePlaybackEvent "
            "(error GetCurrentPlayTime fail)"
        )
        protocol = StringIO()

        result = run(
            self._args("--volume", "4"),
            pcm_input=BytesIO(),
            protocol_output=protocol,
        )

        self.assertEqual(result, 0)
        self.assertEqual(
            protocol.getvalue().splitlines(),
            ["WAMBRIDGE READY", "WAMBRIDGE PLAYING volume=4"],
        )

    @patch("wambridge.pcm_cli.play_url")
    @patch("wambridge.pcm_cli.set_volume")
    @patch("wambridge.pcm_cli.get_volume", return_value=7)
    @patch("wambridge.pcm_cli.local_ip_for", return_value="10.0.0.103")
    @patch(
        "wambridge.pcm_cli.probe",
        return_value=SimpleNamespace(method="SpkName"),
    )
    @patch("wambridge.pcm_cli.select_speaker", return_value=("10.0.0.118", 55001))
    @patch("wambridge.pcm_cli.PcmAudioStreamServer", FakePcmServer)
    def test_restores_volume_after_ambiguous_mute_request(
        self,
        _select_mock,
        _probe_mock,
        _local_ip_mock,
        _get_volume_mock,
        volume_mock,
        _play_url_mock,
    ) -> None:
        volume_mock.side_effect = [
            WamApiError("Cannot reach Samsung WAM at 10.0.0.118:55001: timed out"),
            None,
        ]

        with self.assertRaisesRegex(WamApiError, "timed out"):
            run(
                self._args("--volume", "4"),
                pcm_input=BytesIO(),
                protocol_output=StringIO(),
            )

        self.assertEqual(
            volume_mock.call_args_list,
            [
                call("10.0.0.118", 0, port=55001),
                call("10.0.0.118", 7, port=55001, timeout=1.0),
            ],
        )

    @patch("wambridge.pcm_cli.play_url")
    @patch("wambridge.pcm_cli.set_volume")
    @patch("wambridge.pcm_cli.get_volume", return_value=7)
    @patch("wambridge.pcm_cli.local_ip_for", return_value="10.0.0.103")
    @patch(
        "wambridge.pcm_cli.probe",
        return_value=SimpleNamespace(method="SpkName"),
    )
    @patch("wambridge.pcm_cli.select_speaker", return_value=("10.0.0.118", 55001))
    def test_restores_volume_when_speaker_never_requests_pcm(
        self,
        _select_mock,
        _probe_mock,
        _local_ip_mock,
        _get_volume_mock,
        volume_mock,
        _play_url_mock,
    ) -> None:
        class SilentServer(FakePcmServer):
            def start(self) -> None:
                pass

        args = self._args()
        args.startup_timeout = 0.01

        with patch("wambridge.pcm_cli.PcmAudioStreamServer", SilentServer):
            with self.assertRaisesRegex(
                StreamError,
                "did not request the PCM stream",
            ):
                run(
                    args,
                    pcm_input=BytesIO(),
                    protocol_output=StringIO(),
                )

        self.assertEqual(
            volume_mock.call_args_list,
            [
                call("10.0.0.118", 0, port=55001),
                call("10.0.0.118", 7, port=55001, timeout=1.0),
            ],
        )

    @patch("wambridge.pcm_cli.play_url")
    @patch("wambridge.pcm_cli.set_volume")
    @patch("wambridge.pcm_cli.get_volume", return_value=7)
    @patch("wambridge.pcm_cli.local_ip_for", return_value="10.0.0.103")
    @patch(
        "wambridge.pcm_cli.probe",
        return_value=SimpleNamespace(method="SpkName"),
    )
    @patch("wambridge.pcm_cli.select_speaker", return_value=("10.0.0.118", 55001))
    def test_restores_volume_when_pcm_pipe_closes_before_request(
        self,
        _select_mock,
        _probe_mock,
        _local_ip_mock,
        _get_volume_mock,
        volume_mock,
        _play_url_mock,
    ) -> None:
        class SilentServer(FakePcmServer):
            def start(self) -> None:
                pass

        args = self._args()
        args.startup_timeout = 45

        with (
            patch("wambridge.pcm_cli.PcmAudioStreamServer", SilentServer),
            patch("wambridge.pcm_cli._pcm_input_closed", return_value=True),
        ):
            with self.assertRaisesRegex(
                StreamError,
                "PCM input closed before the speaker requested",
            ):
                run(
                    args,
                    pcm_input=BytesIO(),
                    protocol_output=StringIO(),
                )

        self.assertEqual(
            volume_mock.call_args_list,
            [
                call("10.0.0.118", 0, port=55001),
                call("10.0.0.118", 7, port=55001, timeout=1.0),
            ],
        )

    @patch("wambridge.pcm_cli.play_url")
    @patch("wambridge.pcm_cli.set_volume")
    @patch("wambridge.pcm_cli.get_volume", return_value=0)
    @patch("wambridge.pcm_cli.local_ip_for", return_value="10.0.0.103")
    @patch(
        "wambridge.pcm_cli.probe",
        return_value=SimpleNamespace(method="SpkName"),
    )
    @patch("wambridge.pcm_cli.select_speaker", return_value=("10.0.0.118", 55001))
    def test_restores_muted_volume_when_startup_ends_after_ready(
        self,
        _select_mock,
        _probe_mock,
        _local_ip_mock,
        _get_volume_mock,
        volume_mock,
        _play_url_mock,
    ) -> None:
        class ReadyServer(FakePcmServer):
            def start(self) -> None:
                self.request_started.set()
                self.encoder_started.set()

        args = self._args("--volume", "4")
        args.startup_timeout = 0.01

        with patch("wambridge.pcm_cli.PcmAudioStreamServer", ReadyServer):
            with self.assertRaisesRegex(
                StreamError,
                "Timed out waiting for audio_started",
            ):
                run(
                    args,
                    pcm_input=BytesIO(),
                    protocol_output=StringIO(),
                )

        self.assertEqual(
            volume_mock.call_args_list,
            [
                call("10.0.0.118", 4, port=55001),
                call("10.0.0.118", 0, port=55001, timeout=1.0),
            ],
        )
