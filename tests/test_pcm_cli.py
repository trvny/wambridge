import os
from io import BytesIO, StringIO
from threading import Event
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import call, patch

from wambridge.pcm_cli import PlaybackWatcher, _stopped_line, build_parser, run
from wambridge.samsung import WamApiError
from wambridge.stream import StreamError
from wambridge.wam_events import WamEvent, WamEventError


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
    forced_rejection: str | None = None

    def __init__(self, *_args, **kwargs) -> None:
        self.armed = False
        self.stream_active = False
        self.startup_complete = False
        self.waited = False
        self.failure_checks = 0
        self.offered: list[str] = []
        self.volumes: list[int] = []
        self.pause_volumes: list[bool] = []
        self.released = False
        self.sleep_timer_cancellations = 0
        self.sleep_after_stop = kwargs.get("sleep_after_stop", 0)
        self.release_summary = "stop=sent sleep=off"
        self.__class__.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()

    def release(self) -> None:
        self.released = True

    def cancel_sleep_timer(self) -> None:
        self.sleep_timer_cancellations += 1

    def arm(self) -> None:
        self.armed = True

    def mark_stream_active(self) -> None:
        self.stream_active = True

    def mark_startup_complete(self) -> None:
        self.startup_complete = True

    def offer_stream(self, stream_url: str) -> None:
        self.offered.append(stream_url)

    def set_volume(self, level: int) -> None:
        self.volumes.append(level)

    def set_pause_volume(self, paused: bool) -> None:
        self.pause_volumes.append(paused)

    def wait_for_start(self, *, timeout: float) -> None:
        self.waited = True

    def raise_if_failed(self) -> None:
        self.failure_checks += 1

    def wait_for_response(self, method: str, *, timeout: float) -> str | None:
        if method == "SetVolume":
            return self.forced_rejection
        return None


class FakeControlConnection:
    """Stand in for the persistent 55001 connection the listener thread owns."""

    def __init__(
        self,
        watcher: PlaybackWatcher,
        *,
        rejection: str | None = None,
        rejection_method: str = "SetPlaybackControl",
        failing: bool = False,
        mute_state: str = "off",
        volume_state: int = 7,
    ) -> None:
        self._watcher = watcher
        self._rejection = rejection
        # Which command the rejection answers. Recording it against every method
        # made "the stop was refused" indistinguishable from "everything was
        # refused", so a test could not say which one it meant.
        self._rejection_method = rejection_method
        self._failing = failing
        self._mute_state = mute_state
        self._volume_state = volume_state
        self.sent: list[tuple[str, list | None, bool]] = []

    def send(
        self,
        *,
        method: str,
        arguments: list | None = None,
        api_type: str = "UIC",
        power_on: bool = False,
    ) -> None:
        if self._failing:
            raise WamEventError("Samsung WAM closed the persistent connection")
        self.sent.append((method, arguments, power_on))
        # What the listener thread would have recorded, without the thread.
        answer = self._rejection if method == self._rejection_method else None
        event = WamEvent(
            method=method,
            result="ng" if answer else "ok",
            user_identifier=CLIENT_UUID,
            error_code="3" if answer else None,
            values=(
                {"mute": self._mute_state}
                if method == "GetMute"
                else {"volume": str(self._volume_state)}
                if method == "GetVolume"
                else {}
            ),
        )
        self._watcher._response_events[method] = event
        self._watcher._results[method] = answer or ""
        if method == "SetMute" and answer is None and arguments:
            self._mute_state = str(arguments[0][1])
        if method == "SetVolume" and answer is None and arguments:
            self._volume_state = int(arguments[0][1])


class PcmCliTests(TestCase):
    def setUp(self) -> None:
        FakePlaybackWatcher.instances.clear()
        FakePlaybackWatcher.forced_rejection = None
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
        # The real reading depends on the host's socket table, and on a machine
        # that has no speaker it is neither zero nor stable.
        released_patcher = patch(
            "wambridge.pcm_cli.wait_until_released",
            return_value=0,
        )
        released_patcher.start()
        self.addCleanup(released_patcher.stop)

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
        lines = protocol.getvalue().splitlines()
        self.assertEqual(
            lines[:5],
            [
                "WAMBRIDGE STREAM_REQUESTED",
                "WAMBRIDGE ENCODER_STARTED",
                "WAMBRIDGE READY",
                "WAMBRIDGE AUDIO_STARTED",
                "WAMBRIDGE PLAYING volume=4",
            ],
        )
        # The port and token are fresh per session, so only the shape is pinned.
        # It has to come after PLAYING: before that the startup sequence owns
        # the volume and a level arriving mid-handshake fights the unmute.
        self.assertRegex(lines[5], r"^WAMBRIDGE CONTROL_PORT \d+ \S+$")
        # Last, and only after the server and the control socket are gone: this
        # line is what a morning-after console read has to be able to answer
        # "was anything still holding the speaker" from.
        self.assertEqual(lines[6], "WAMBRIDGE STOPPED stop=sent sleep=off holding=0")
        self.assertEqual(len(lines), 7)
        self.assertEqual(len(FakePlaybackWatcher.instances), 1)
        watcher = FakePlaybackWatcher.instances[0]
        self.assertTrue(watcher.released)
        self.assertTrue(watcher.armed)
        self.assertTrue(watcher.stream_active)
        self.assertTrue(watcher.startup_complete)
        self.assertFalse(watcher.waited)
        self.assertGreaterEqual(watcher.failure_checks, 2)
        self.assertEqual(
            watcher.offered,
            ["http://10.0.0.103:1234/stream/test.flac"],
        )
        self.assertEqual(watcher.volumes, [4, 4])
        volume_mock.assert_called_once_with("10.0.0.118", 0, port=55001)

    @patch("wambridge.pcm_cli.set_volume")
    @patch("wambridge.pcm_cli.get_volume", return_value=37)
    @patch("wambridge.pcm_cli.local_ip_for", return_value="10.0.0.103")
    @patch(
        "wambridge.pcm_cli.probe",
        return_value=SimpleNamespace(method="SpkName"),
    )
    @patch("wambridge.pcm_cli.select_speaker", return_value=("10.0.0.118", 55001))
    @patch("wambridge.pcm_cli.PcmAudioStreamServer", FakePcmServer)
    def test_rejected_unmute_fails_instead_of_reporting_playing(
        self,
        _select_mock,
        _probe_mock,
        _local_ip_mock,
        _get_volume_mock,
        volume_mock,
    ) -> None:
        # Startup mutes the speaker. If the speaker rejects the command that
        # undoes the mute, PLAYING would describe silence.
        FakePlaybackWatcher.forced_rejection = "Speaker rejected SetVolume (error 3)"
        protocol = StringIO()

        with self.assertRaisesRegex(StreamError, "would stay muted"):
            run(
                self._args("--volume", "4"),
                pcm_input=BytesIO(),
                protocol_output=protocol,
            )

        self.assertNotIn("PLAYING", protocol.getvalue())
        watcher = FakePlaybackWatcher.instances[0]
        self.assertFalse(watcher.startup_complete)
        self.assertEqual(
            volume_mock.call_args_list,
            [
                call("10.0.0.118", 0, port=55001),
                call("10.0.0.118", 37, port=55001, timeout=1.0),
            ],
        )

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
        lines = protocol.getvalue().splitlines()
        self.assertEqual(
            lines[:5],
            [
                "WAMBRIDGE STREAM_REQUESTED",
                "WAMBRIDGE ENCODER_STARTED",
                "WAMBRIDGE READY",
                "WAMBRIDGE AUDIO_STARTED",
                "WAMBRIDGE PLAYING volume=4",
            ],
        )
        # The port and token are fresh per session, so only the shape is pinned.
        # It has to come after PLAYING: before that the startup sequence owns
        # the volume and a level arriving mid-handshake fights the unmute.
        self.assertRegex(lines[5], r"^WAMBRIDGE CONTROL_PORT \d+ \S+$")
        self.assertEqual(lines[6], "WAMBRIDGE STOPPED stop=sent sleep=off holding=0")
        self.assertEqual(len(lines), 7)

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

        # A speaker found at 0 was muted on purpose, and the startup that failed
        # here is the one that would have undone it. The 0 goes back.
        self.assertEqual(
            volume_mock.call_args_list,
            [call("10.0.0.118", 0, port=55001, timeout=1.0)],
        )

    @patch("wambridge.pcm_cli.set_volume")
    @patch("wambridge.pcm_cli.get_volume", return_value=7)
    @patch("wambridge.pcm_cli.local_ip_for", return_value="10.0.0.103")
    @patch(
        "wambridge.pcm_cli.probe",
        return_value=SimpleNamespace(method="SpkName"),
    )
    @patch("wambridge.pcm_cli.select_speaker", return_value=("10.0.0.118", 55001))
    def test_reports_the_teardown_of_a_session_that_failed(
        self,
        _select_mock,
        _probe_mock,
        _local_ip_mock,
        _get_volume_mock,
        _volume_mock,
    ) -> None:
        # A run that ends badly is exactly the one that used to walk away from a
        # speaker still holding a playback session, so its teardown has to be
        # reported too, not only a clean stop's.
        class SilentServer(FakePcmServer):
            def start(self) -> None:
                pass

        args = self._args()
        args.startup_timeout = 0.01
        protocol = StringIO()

        with patch("wambridge.pcm_cli.PcmAudioStreamServer", SilentServer):
            with self.assertRaises(StreamError):
                run(args, pcm_input=BytesIO(), protocol_output=protocol)

        lines = protocol.getvalue().splitlines()
        self.assertTrue(FakePlaybackWatcher.instances[0].released)
        self.assertEqual(lines[-1], "WAMBRIDGE STOPPED stop=sent sleep=off holding=0")

    @patch("wambridge.pcm_cli.wait_until_released", return_value=None)
    def test_unreadable_socket_table_is_unknown_not_zero(self, _released_mock) -> None:
        self.assertEqual(
            _stopped_line(None, "10.0.0.118", sleep_after_stop=0),
            "WAMBRIDGE STOPPED stop=skipped sleep=off holding=unknown",
        )

    @patch("wambridge.pcm_cli.wait_until_released", return_value=0)
    def test_teardown_line_keeps_its_shape_when_startup_died_early(
        self,
        _released_mock,
    ) -> None:
        # No watcher was ever built, so the summary comes from the fallback. It
        # still has to separate "nobody asked for a timer" from "one was
        # configured and nothing got to arm it".
        self.assertEqual(
            _stopped_line(None, "10.0.0.118", sleep_after_stop=90),
            "WAMBRIDGE STOPPED stop=skipped sleep=skipped holding=0",
        )

    @patch("wambridge.pcm_cli.wait_until_released", return_value=0)
    def test_teardown_count_ignores_this_helpers_own_sockets(
        self,
        released_mock,
    ) -> None:
        # Measured 2026-08-15: a locally closed socket sits in FIN_WAIT for
        # 0.5-1.5 s, and the component serializes helper teardown ahead of the
        # replacement, so counting our own put that delay into every seek.
        _stopped_line(None, "10.0.0.118", sleep_after_stop=0)

        self.assertEqual(
            released_mock.call_args.kwargs["own_pid"],
            os.getpid(),
        )

    def _connected_watcher(
        self,
        *,
        sleep_after_stop: int = 0,
        clear_sleep_timer: bool = False,
        rejection: str | None = None,
        rejection_method: str = "SetPlaybackControl",
        failing: bool = False,
        stream_active: bool = True,
        mute_state: str = "off",
        volume_state: int = 7,
    ) -> tuple[PlaybackWatcher, FakeControlConnection]:
        watcher = PlaybackWatcher(
            "10.0.0.118",
            CLIENT_UUID,
            port=55001,
            sleep_after_stop=sleep_after_stop,
            clear_sleep_timer=clear_sleep_timer,
        )
        connection = FakeControlConnection(
            watcher,
            rejection=rejection,
            rejection_method=rejection_method,
            failing=failing,
            mute_state=mute_state,
            volume_state=volume_state,
        )
        watcher._connection = connection
        watcher._current_volume = volume_state
        # Default on, because every release test below describes a live session:
        # the speaker fetched the stream and this helper owns the playback.
        # `release()` skips an offer the speaker never took up, so a watcher that
        # never reached this point has nothing to end.
        if stream_active:
            watcher.mark_stream_active()
        return watcher, connection

    def test_pause_volume_uses_the_existing_connection(self) -> None:
        watcher, connection = self._connected_watcher(volume_state=7)

        watcher.set_pause_volume(True)
        watcher.set_pause_volume(False)

        self.assertEqual(
            connection.sent,
            [
                ("SetVolume", [("volume", 0, "dec")], True),
                ("SetVolume", [("volume", 7, "dec")], True),
            ],
        )

    def test_external_volume_event_refreshes_pause_restore_target(self) -> None:
        watcher, connection = self._connected_watcher(volume_state=12)
        event = WamEvent(
            method="VolumeLevel",
            result="ok",
            user_identifier=CLIENT_UUID,
            error_code=None,
            values={"volume": "3"},
        )

        watcher._observe_volume_event(event, external=True)
        watcher.set_pause_volume(True)
        watcher.set_pause_volume(False)

        self.assertEqual(
            connection.sent,
            [
                ("SetVolume", [("volume", 0, "dec")], True),
                ("SetVolume", [("volume", 3, "dec")], True),
            ],
        )
        self.assertEqual(watcher._current_volume, 3)

    def test_unmatched_zero_event_during_pause_keeps_restore_target(self) -> None:
        watcher, connection = self._connected_watcher(volume_state=3)
        watcher.set_pause_volume(True)
        event = WamEvent(
            method="VolumeLevel",
            result="ok",
            user_identifier=CLIENT_UUID,
            error_code=None,
            values={"volume": "0"},
        )

        watcher._observe_volume_event(event, external=True)
        watcher.set_pause_volume(False)

        self.assertEqual(
            connection.sent,
            [
                ("SetVolume", [("volume", 0, "dec")], True),
                ("SetVolume", [("volume", 3, "dec")], True),
            ],
        )

    def test_external_volume_event_while_paused_updates_target_and_reapplies_zero(self) -> None:
        watcher, connection = self._connected_watcher(volume_state=7)
        watcher.set_pause_volume(True)
        event = WamEvent(
            method="VolumeLevel",
            result="ok",
            user_identifier=CLIENT_UUID,
            error_code=None,
            values={"volume": "4"},
        )

        watcher._observe_volume_event(event, external=True)
        watcher.set_pause_volume(False)

        self.assertEqual(
            connection.sent,
            [
                ("SetVolume", [("volume", 0, "dec")], True),
                ("SetVolume", [("volume", 0, "dec")], True),
                ("SetVolume", [("volume", 4, "dec")], True),
            ],
        )
        self.assertEqual(watcher._current_volume, 4)

    def test_pause_volume_preserves_an_already_zero_speaker(self) -> None:
        watcher, connection = self._connected_watcher(volume_state=0)

        watcher.set_pause_volume(True)
        watcher.set_pause_volume(False)

        self.assertEqual(connection.sent, [])

    def test_slider_change_while_paused_updates_resume_target_only(self) -> None:
        watcher, connection = self._connected_watcher(volume_state=7)

        watcher.set_pause_volume(True)
        watcher.set_volume(4)
        watcher.set_pause_volume(False)

        self.assertEqual(
            connection.sent,
            [
                ("SetVolume", [("volume", 0, "dec")], True),
                ("SetVolume", [("volume", 4, "dec")], True),
            ],
        )

    def test_release_restores_pause_volume_before_stopping(self) -> None:
        watcher, connection = self._connected_watcher(volume_state=7)
        watcher.arm()

        watcher.set_pause_volume(True)
        watcher.release()

        self.assertEqual(
            connection.sent,
            [
                ("SetVolume", [("volume", 0, "dec")], True),
                ("SetVolume", [("volume", 7, "dec")], True),
                ("SetPlaybackControl", [("playbackcontrol", "pause", "str")], False),
            ],
        )

    def test_release_retries_and_reports_a_rejected_pause_restore(self) -> None:
        watcher, connection = self._connected_watcher(volume_state=7)
        watcher.arm()
        watcher.set_pause_volume(True)
        connection._rejection = "Speaker rejected SetVolume (error 3)"
        connection._rejection_method = "SetVolume"

        watcher.release()

        restore = ("SetVolume", [("volume", 7, "dec")], True)
        self.assertEqual(connection.sent.count(restore), 2)
        self.assertEqual(watcher.release_summary, "stop=sent restore=rejected sleep=off")
        self.assertEqual(watcher._pause_restore_volume, 7)

    def test_release_preserves_a_speaker_that_was_already_at_zero(self) -> None:
        watcher, connection = self._connected_watcher(volume_state=0)
        watcher.arm()

        watcher.set_pause_volume(True)
        watcher.release()

        self.assertEqual(
            connection.sent,
            [
                ("SetPlaybackControl", [("playbackcontrol", "pause", "str")], False),
            ],
        )

    def test_resume_rejection_marks_the_helper_failed(self) -> None:
        watcher, connection = self._connected_watcher(volume_state=7)
        watcher.set_pause_volume(True)
        connection._rejection = "Speaker rejected SetVolume (error 3)"
        connection._rejection_method = "SetVolume"

        with self.assertRaisesRegex(WamApiError, "rejected SetVolume"):
            watcher.set_pause_volume(False)

        self.assertIn("Could not restore speaker volume after pause", watcher._error)

    def test_mute_status_matches_the_pending_set_mute(self) -> None:
        # Kept as protocol regression: the M5 really replies MuteStatus to SetMute,
        # even though pause no longer uses mute because it closes the HTTP pull.
        watcher, _connection = self._connected_watcher()
        watcher._pending.append("SetMute")
        event = WamEvent(
            method="MuteStatus",
            result="ok",
            user_identifier=CLIENT_UUID,
            error_code=None,
            values={"mute": "on"},
        )

        self.assertEqual(watcher._match_pending(event), "SetMute")

    def test_pause_volume_surfaces_an_explicit_rejection(self) -> None:
        watcher, _connection = self._connected_watcher(
            rejection="Speaker rejected SetVolume (error 3)",
            rejection_method="SetVolume",
            volume_state=7,
        )

        with self.assertRaisesRegex(WamApiError, "rejected SetVolume"):
            watcher.set_pause_volume(True)

    def test_release_stops_playback_over_the_connection_already_open(self) -> None:
        watcher, connection = self._connected_watcher()
        watcher.arm()

        watcher.release()

        # UIC pause, because that is what this firmware answers on the URL path,
        # and no mute: a mute would hand the speaker back silent to whoever
        # picks it up next. No `pwron`, which would wake what this is releasing.
        self.assertEqual(
            connection.sent,
            [("SetPlaybackControl", [("playbackcontrol", "pause", "str")], False)],
        )
        self.assertEqual(watcher.release_summary, "stop=sent sleep=off")

    def test_release_arms_the_sleep_timer_only_when_asked(self) -> None:
        watcher, connection = self._connected_watcher(sleep_after_stop=120)
        watcher.arm()

        watcher.release()

        self.assertEqual(
            connection.sent[1],
            (
                "SetSleepTimer",
                [("option", "start", "str"), ("sleeptime", 120, "dec")],
                False,
            ),
        )
        self.assertEqual(watcher.release_summary, "stop=sent sleep=120s")

    def test_release_sends_nothing_when_no_stream_was_offered(self) -> None:
        watcher, connection = self._connected_watcher(sleep_after_stop=120)

        watcher.release()

        # Never armed means no playback session of ours exists to end, and a
        # stop would reach past this helper into whatever else is playing.
        self.assertEqual(connection.sent, [])
        # Both fields on every teardown line, whatever happened. `skipped`
        # rather than `off`, because a timer was configured and this session
        # simply had nothing to arm it after.
        self.assertEqual(watcher.release_summary, "stop=skipped sleep=skipped")

    def test_release_sends_nothing_when_the_speaker_refused_the_offer(self) -> None:
        watcher, connection = self._connected_watcher(sleep_after_stop=120)
        watcher.arm()
        # Arming happens before the offer, so it says a URL was sent, not that
        # the speaker took it. A matched rejection means the speaker is still
        # doing whatever it was doing - and `pause` would stop that instead.
        watcher._results["SetUrlPlayback"] = "Speaker rejected SetUrlPlayback"

        watcher.release()

        self.assertEqual(connection.sent, [])
        self.assertEqual(watcher.release_summary, "stop=skipped sleep=skipped")

    def test_release_happens_once_per_session(self) -> None:
        watcher, connection = self._connected_watcher()
        watcher.arm()

        watcher.release()
        watcher.release()

        self.assertEqual(len(connection.sent), 1)

    def test_release_records_a_rejection_without_raising(self) -> None:
        watcher, _connection = self._connected_watcher(
            sleep_after_stop=120,
            rejection="Speaker rejected SetPlaybackControl (error 3)",
        )
        watcher.arm()

        watcher.release()

        self.assertEqual(watcher.release_summary, "stop=rejected sleep=120s")

    def test_an_offer_the_speaker_never_took_up_is_not_released(self) -> None:
        # The matched rejection is the rare way to own nothing; this firmware
        # answers plenty of commands with silence, so the common way is an offer
        # that went unanswered. Pausing then reaches past this helper into
        # whatever the speaker is really doing.
        watcher, connection = self._connected_watcher(stream_active=False)
        watcher.arm()

        watcher.release()

        self.assertEqual(connection.sent, [])
        self.assertEqual(watcher.release_summary, "stop=skipped sleep=off")

    def test_summary_carries_both_fields_before_release_ever_runs(self) -> None:
        # A session whose __enter__ raises never reaches release(), and the
        # teardown line is still printed from the outer finally. It has to carry
        # the sleep field like every other one, or the morning-after line is
        # shorter for exactly the sessions that failed.
        for sleep_after_stop, expected in ((0, "sleep=off"), (120, "sleep=skipped")):
            with self.subTest(sleep_after_stop=sleep_after_stop):
                watcher = PlaybackWatcher(
                    "10.0.0.118",
                    CLIENT_UUID,
                    port=55001,
                    sleep_after_stop=sleep_after_stop,
                )

                self.assertEqual(
                    watcher.release_summary, f"stop=skipped {expected}"
                )

    def test_release_survives_a_speaker_that_has_gone_away(self) -> None:
        watcher, _connection = self._connected_watcher(failing=True)
        watcher.arm()

        # Teardown runs when something has already gone wrong. Turning that into
        # a second exception would lose the first one.
        watcher.release()

        self.assertEqual(watcher.release_summary, "stop=unreachable sleep=off")

    def test_a_pending_sleep_timer_is_cleared_before_the_next_stream(self) -> None:
        watcher, connection = self._connected_watcher(
            sleep_after_stop=120,
            clear_sleep_timer=True,
        )

        watcher.cancel_sleep_timer()

        # A seek stops one helper and starts another. Without this the timer the
        # first one armed would put the speaker into standby mid-track.
        self.assertEqual(
            connection.sent,
            [
                (
                    "SetSleepTimer",
                    [("option", "off", "str"), ("sleeptime", 0, "dec")],
                    False,
                )
            ],
        )

    def test_a_timer_is_still_cleared_after_the_setting_drops_to_zero(
        self,
    ) -> None:
        # The regression this flag exists for. Turning `sleep_after_stop` off
        # does not disarm what an earlier helper already armed, and reading the
        # decision off the setting made the one helper that could still clear it
        # decide the feature was disabled. The speaker then slept mid-track,
        # right after the listener had turned the feature off.
        watcher, connection = self._connected_watcher(
            sleep_after_stop=0,
            clear_sleep_timer=True,
        )

        watcher.cancel_sleep_timer()

        self.assertEqual(
            connection.sent,
            [
                (
                    "SetSleepTimer",
                    [("option", "off", "str"), ("sleeptime", 0, "dec")],
                    False,
                )
            ],
        )

    def test_no_sleep_timer_configured_leaves_the_speakers_own_timer_alone(
        self,
    ) -> None:
        # A default install must not touch a timer set from the Samsung app: the
        # speaker does not say who armed one, so the component only clears what
        # its own configuration has armed.
        watcher, connection = self._connected_watcher()

        watcher.cancel_sleep_timer()

        self.assertEqual(connection.sent, [])

    def test_a_refused_sleep_timer_is_not_reported_as_armed(self) -> None:
        watcher, _connection = self._connected_watcher(
            sleep_after_stop=120,
            rejection="Speaker rejected SetSleepTimer (error 3)",
            rejection_method="SetSleepTimer",
        )
        watcher.arm()

        watcher.release()

        # `_send_command` returns once the request is written, so without
        # matching the answer this said `sleep=120s` about a timer the speaker
        # had refused.
        self.assertEqual(watcher.release_summary, "stop=sent sleep=rejected")
