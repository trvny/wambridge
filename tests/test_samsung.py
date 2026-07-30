from unittest import TestCase
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from wambridge.samsung import (
    WamApiError,
    WamPlaybackStatus,
    WamResponse,
    build_api_url,
    build_command,
    get_mute,
    get_playback_status,
    get_volume,
    normalize_device_id,
    parse_response,
    pause_playback,
    resume_playback,
    set_mute,
    set_playback_control,
    set_volume,
    stop_playback,
)


class SamsungCommandTests(TestCase):
    def test_builds_get_command(self) -> None:
        self.assertEqual(build_command("GetSpkName"), "<name>GetSpkName</name>")

    def test_builds_power_on_command(self) -> None:
        self.assertEqual(
            build_command(
                "SetMute",
                [("mute", "on", "str")],
                power_on=True,
            ),
            '<pwron>on</pwron><name>SetMute</name>'
            '<p type="str" name="mute" val="on"/>',
        )

    def test_builds_set_url_playback_command(self) -> None:
        command = build_command(
            "SetUrlPlayback",
            [
                ("url", "http://192.168.1.2:8765/live.flac", "cdata"),
                ("buffersize", 0, "dec"),
            ],
        )

        self.assertIn("<name>SetUrlPlayback</name>", command)
        self.assertIn(
            "<![CDATA[http://192.168.1.2:8765/live.flac]]>",
            command,
        )
        self.assertIn('name="buffersize" val="0"', command)

    def test_builds_encoded_api_url(self) -> None:
        url = build_api_url("192.168.1.50", "GetSpkName")
        parsed = urlparse(url)

        self.assertEqual(parsed.netloc, "192.168.1.50:55001")
        self.assertEqual(parsed.path, "/UIC")
        self.assertEqual(
            parse_qs(parsed.query)["cmd"],
            ["<name>GetSpkName</name>"],
        )

    def test_builds_cpm_device_id_url(self) -> None:
        url = build_api_url(
            "10.0.0.118",
            "GetDeviceId",
            api_type="CPM",
        )
        self.assertEqual(urlparse(url).path, "/CPM")

    def test_parses_response_values(self) -> None:
        response = parse_response(
            "<CPM><method>DeviceId</method>"
            '<response result="ok">'
            "<device_id>A1:B2:C3:D4:E5:F6</device_id>"
            "</response></CPM>"
        )

        self.assertEqual(response.method, "DeviceId")
        self.assertEqual(
            response.values["device_id"],
            "A1:B2:C3:D4:E5:F6",
        )
        self.assertEqual(
            normalize_device_id(response.values["device_id"]),
            "A1B2C3D4E5F6",
        )

    def test_parses_parameter_style_values(self) -> None:
        response = parse_response(
            "<UIC><method>SpkName</method>"
            '<response result="ok">'
            '<p name="spkname" val="[Samsung] M5"/>'
            "</response></UIC>"
        )

        self.assertEqual(response.values["spkname"], "[Samsung] M5")

    @patch("wambridge.samsung.request")
    def test_gets_volume(self, request_mock) -> None:
        request_mock.return_value = WamResponse(
            method="VolumeLevel",
            result="ok",
            body="",
            values={"volume": "37"},
        )

        self.assertEqual(get_volume("10.0.0.118"), 37)
        request_mock.assert_called_once_with(
            "10.0.0.118",
            "GetVolume",
            port=55001,
            timeout=5.0,
        )

    @patch("wambridge.samsung.request")
    def test_sets_volume_with_power_on(self, request_mock) -> None:
        request_mock.return_value = WamResponse(
            method="VolumeLevel",
            result="ok",
            body="",
        )

        set_volume("10.0.0.118", 10)

        request_mock.assert_called_once_with(
            "10.0.0.118",
            "SetVolume",
            [("volume", 10, "dec")],
            port=55001,
            timeout=5.0,
            power_on=True,
        )

    def test_rejects_out_of_range_volume(self) -> None:
        with self.assertRaisesRegex(ValueError, "between 0 and 100"):
            set_volume("10.0.0.118", 101)

    @patch("wambridge.samsung.request")
    def test_rejects_invalid_reported_volume(self, request_mock) -> None:
        request_mock.return_value = WamResponse(
            method="VolumeLevel",
            result="ok",
            body="",
            values={"volume": "full"},
        )

        with self.assertRaisesRegex(WamApiError, "invalid volume"):
            get_volume("10.0.0.118")

    @patch("wambridge.samsung.request")
    def test_reads_mute_state(self, request_mock) -> None:
        request_mock.return_value = WamResponse(
            method="MuteStatus",
            result="ok",
            body="",
            values={"mute": "on"},
        )

        self.assertTrue(get_mute("10.0.0.118"))

    @patch("wambridge.samsung.request")
    def test_sets_mute_with_power_on(self, request_mock) -> None:
        request_mock.return_value = WamResponse(
            method="MuteStatus",
            result="ok",
            body="",
        )

        set_mute("10.0.0.118", True)

        request_mock.assert_called_once_with(
            "10.0.0.118",
            "SetMute",
            [("mute", "on", "str")],
            port=55001,
            timeout=5.0,
            power_on=True,
        )

    @patch("wambridge.samsung.get_radio_info")
    @patch("wambridge.samsung.get_play_status")
    def test_enriches_native_playback_status(
        self,
        play_status_mock,
        radio_info_mock,
    ) -> None:
        play_status_mock.return_value = WamPlaybackStatus(
            function="wifi",
            submode="cp",
        )
        radio_info_mock.return_value = WamPlaybackStatus(
            function="wifi",
            submode="cp",
            play_status="play",
            cp_name="TuneIn",
            title="Radio Paradise",
        )

        status = get_playback_status("10.0.0.118")

        self.assertTrue(status.is_native_cp)
        self.assertEqual(status.cp_name, "TuneIn")
        self.assertEqual(status.title, "Radio Paradise")

    @patch("wambridge.samsung.request")
    def test_sends_cpm_stop(self, request_mock) -> None:
        request_mock.return_value = WamResponse(
            method="PlaybackStatus",
            result="ok",
            body="",
        )

        set_playback_control(
            "10.0.0.118",
            "stop",
            api_type="CPM",
        )

        request_mock.assert_called_once_with(
            "10.0.0.118",
            "SetPlaybackControl",
            [("playbackcontrol", "stop", "str")],
            port=55001,
            timeout=10.0,
            api_type="CPM",
            power_on=True,
        )

    @patch("wambridge.samsung.set_playback_control")
    @patch("wambridge.samsung.get_playback_status")
    def test_pauses_tunein_through_cpm(
        self,
        status_mock,
        control_mock,
    ) -> None:
        status_mock.return_value = WamPlaybackStatus(
            submode="cp",
            cp_name="TuneIn",
        )

        pause_playback("10.0.0.118")

        control_mock.assert_called_once_with(
            "10.0.0.118",
            "pause",
            api_type="CPM",
            port=55001,
            timeout=10.0,
        )

    @patch("wambridge.samsung.set_mute")
    @patch("wambridge.samsung.set_playback_control")
    @patch("wambridge.samsung.get_playback_status")
    def test_stops_url_playback_with_pause_and_mute(
        self,
        status_mock,
        control_mock,
        mute_mock,
    ) -> None:
        status_mock.return_value = WamPlaybackStatus(
            submode="cp",
            cp_name="Unknown",
        )

        stop_playback("10.0.0.118")

        control_mock.assert_called_once_with(
            "10.0.0.118",
            "pause",
            api_type="UIC",
            port=55001,
            timeout=10.0,
        )
        mute_mock.assert_called_once_with(
            "10.0.0.118",
            True,
            port=55001,
            timeout=10.0,
        )

    @patch("wambridge.samsung.get_playback_status")
    def test_rejects_url_resume(self, status_mock) -> None:
        status_mock.return_value = WamPlaybackStatus(
            submode="cp",
            cp_name="Unknown",
        )

        with self.assertRaisesRegex(WamApiError, "cannot be resumed"):
            resume_playback("10.0.0.118")
