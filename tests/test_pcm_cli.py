from io import BytesIO, StringIO
from threading import Event
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import call, patch

from wambridge.pcm_cli import PlaybackWatcher, build_parser, run
from wambridge.samsung import WamApiError
from wambridge.stream import StreamError
from wambridge.wam_events import WamEvent


CLIENT_UUID = "00000000-0000-4000-8000-000000000001"


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


class FakePlaybackWatcher:
    instances: list["FakePlaybackWatcher"] = []

    def __init__(self, *_args, **_kwargs) -> None:
        self.armed = False
        self.stream_active = False
        self.waited = False
        self.failure_checks = 0
        self.offered: list[str] = []
        self.volumes: list[int] = []
        self.__class__.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def arm(self) -> None:
        self.armed = True

    def mark_stream_active(self) -> None:
        self.stream_active = True

    def offer_stream(self, stream_url: str) -> None:
        self.offered.append(stream_url)

    def set_volume(self, level: int) -> None:
        self.volumes.append(level)

    def wait_for_start(self, *, timeout: float) -> None:
        self.waited = True

    def raise_if_failed(self) -> None:
        self.failure_checks += 1


class PcmCliTests(TestCase):
    def setUp(self) -> None:
        FakePlaybackWatcher.instances.clear()
        patcher = patch(
            "wambridge.pcm_cli.PlaybackWatcher",
            FakePlaybackWatcher,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        uuid_patcher = patch(
            "wambridge.pcm_cli.load_client_uuid",
            return_value=CLIENT_UUID,
        )
        uuid_patcher.start()
        self.addCleanup(uuid_patcher.stop)

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
    ) -> None:
        protocol = StringIO()

        result = run(
            self._args("--volume", "4"),
            pcm_input=BytesIO(),
            protocol_output=protocol,
        )

        self.assertEqual(result, 0)
        self.assertEqual(
            protocol.getvalue().splitlines(),
            [
                "WAMBRIDGE STREAM_REQUESTED",
                "WAMBRIDGE ENCODER_STARTED",
                "WAMBRIDGE READY",
                "WAMBRIDGE AUDIO_STARTED",
                "WAMBRIDGE PLAYING volume=4",
            ],
        )
        self.assertEqual(len(FakePlaybackWatcher.instances), 1)
        watcher = FakePlaybackWatcher.instances[0]
        self.assertTrue(watcher.armed)
        self.assertTrue(watcher.stream_active)
        self.assertFalse(watcher.waited)
        self.assertGreaterEqual(watcher.failure_checks, 2)
        self.assertEqual(
            watcher.offered,
            ["http://10.0.0.103:1234/stream/test.flac"],
        )
        self.assertEqual(watcher.volumes, [4, 4])
        volume_mock.assert_called_once_with("10.0.0.118", 0, port=55001)

    def test_watcher_correlates_only_start_events_after_arming(self) -> None:
        watcher = PlaybackWatcher("10.0.0.118", CLIENT_UUID.upper(), port=55001)
        own_event = WamEvent(
            method="StartPlaybackEvent",
            result="ok",
            user_identifier=CLIENT_UUID,
            error_code=None,
        )
        public_event = WamEvent(
            method="StartPlaybackEvent",
            result="ok",
            user_identifier="public",
            error_code=None,
        )

        self.assertFalse(watcher._belongs_to_attempt(own_event))
        self.assertFalse(watcher._belongs_to_attempt(public_event))

        watcher.arm()

        self.assertTrue(watcher._belongs_to_attempt(own_event))
        self.assertTrue(watcher._belongs_to_attempt(public_event))
        for identifier in (CLIENT_UUID, "public"):
            self.assertFalse(
                watcher._belongs_to_attempt(
                    WamEvent(
                        method="ErrorEvent",
                        result="ng",
                        user_identifier=identifier,
                        error_code="71",
                    )
                )
            )
        self.assertFalse(
            watcher._belongs_to_attempt(
                WamEvent(
                    method="StartPlaybackEvent",
                    result="ok",
                    user_identifier="00000000-0000-4000-8000-000000000099",
                    error_code=None,
                )
            )
        )
        self.assertFalse(
            watcher._belongs_to_attempt(
                WamEvent(
                    method="StartPlaybackEvent",
                    result="ok",
                    user_identifier=None,
                    error_code=None,
                )
            )
        )

    @patch("wambridge.pcm_cli.set_volume")
    @patch("wambridge.pcm_cli.get_volume", return_value=4)
    @patch("wambridge.pcm_cli.local_ip_for", return_value="10.0.0.103")
    @patch(
        "wambridge.pcm_cli.probe",
        return_value=SimpleNamespace(method="SpkName"),
    )
    @patch("wambridge.pcm_cli.select_speaker", return_value=("10.0.0.118", 55001))
    @patch("wambridge.pcm_cli.PcmAudioStreamServer", FakePcmServer)
    def test_emits_playing_without_start_playback_event(
        self,
        _select_mock,
        _probe_mock,
        _local_ip_mock,
        _get_volume_mock,
        _volume_mock,
    ) -> None:
        class MissingEventWatcher(FakePlaybackWatcher):
            def wait_for_start(self, *, timeout: float) -> None:
                raise AssertionError("URL playback must not wait for StartPlaybackEvent")

        protocol = StringIO()
        with patch("wambridge.pcm_cli.PlaybackWatcher", MissingEventWatcher):
            result = run(
                self._args("--volume", "4"),
                pcm_input=BytesIO(),
                protocol_output=protocol,
            )

        self.assertEqual(result, 0)
        self.assertEqual(
            protocol.getvalue().splitlines(),
            [
                "WAMBRIDGE STREAM_REQUESTED",
                "WAMBRIDGE ENCODER_STARTED",
                "WAMBRIDGE READY",
                "WAMBRIDGE AUDIO_STARTED",
                "WAMBRIDGE PLAYING volume=4",
            ],
        )

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
            [call("10.0.0.118", 0, port=55001, timeout=1.0)],
        )
