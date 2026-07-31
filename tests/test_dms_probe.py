from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
from xml.etree import ElementTree

from wambridge.dms_probe import (
    MEDIA_SERVER_DEVICE_TYPE,
    SamsungDmsServer,
    SsdpAdvertiser,
    play_new_folder_control_apk,
    play_share_control_apk,
    set_ip_info_apk,
)
from wambridge.samsung import WamResponse


class SamsungDmsCommandTests(TestCase):
    @patch("wambridge.dms_probe.request")
    def test_registers_raw_uuid_with_apk_fields(self, request_mock) -> None:
        request_mock.return_value = WamResponse(method="IpInfo", result="ok", body="")

        set_ip_info_apk(
            "10.0.0.118",
            "12345678-1234-1234-1234-123456789abc",
            "10.0.0.103:3921",
        )

        request_mock.assert_called_once_with(
            "10.0.0.118",
            "SetIpInfo",
            [
                ("uuid", "12345678-1234-1234-1234-123456789abc", "str"),
                ("ip", "10.0.0.103:3921", "str"),
            ],
            port=55001,
            timeout=3.0,
        )

    @patch("wambridge.dms_probe.request")
    def test_sends_literal_share_control(self, request_mock) -> None:
        play_share_control_apk(
            "10.0.0.118",
            source_name="WAMBridge",
            device_udn="uuid:1234",
            object_id="ABC",
        )

        request_mock.assert_called_once_with(
            "10.0.0.118",
            "SetSharePlaybackControl",
            [
                ("playbackcontrol", "play", "str"),
                ("playertype", "allshare", "str"),
                ("sourcename", "WAMBridge", "cdata"),
                ("playtime", 0, "dec"),
                ("device_udn", "uuid:1234", "str"),
                ("objectid", "ABC", "str"),
            ],
            port=55001,
            timeout=3.0,
        )

    @patch("wambridge.dms_probe.request")
    def test_sends_literal_folder_fallback_and_typo(self, request_mock) -> None:
        play_new_folder_control_apk(
            "10.0.0.118",
            source_name="WAMBridge",
            device_udn="uuid:1234",
            object_id="ABC",
        )

        arguments = request_mock.call_args.args[2]
        self.assertIn(("playbackcontol", "play", "str"), arguments)
        self.assertEqual(request_mock.call_args.args[1], "SetNewFolderPlaybackControl")
        self.assertTrue(request_mock.call_args.kwargs["power_on"])


class SamsungDmsServerTests(TestCase):
    def test_uses_raw_uuid_and_samsung_description(self) -> None:
        with TemporaryDirectory() as directory:
            source = Path(directory) / "a&b.mp3"
            source.write_bytes(b"test")
            server = SamsungDmsServer(source, bind="127.0.0.1", port=0)
            try:
                self.assertEqual(server.udn, f"uuid:{server.uuid}")
                root = ElementTree.fromstring(server._device_description())
                namespace = {"d": "urn:schemas-upnp-org:device-1-0"}
                self.assertEqual(
                    root.findtext("d:device/d:deviceType", namespaces=namespace),
                    MEDIA_SERVER_DEVICE_TYPE,
                )
                self.assertEqual(
                    root.findtext("d:device/d:manufacturer", namespaces=namespace),
                    "SEC",
                )
            finally:
                server.close()


class SsdpAdvertiserTests(TestCase):
    def setUp(self) -> None:
        self.ssdp = SsdpAdvertiser(
            host_ip="10.0.0.103",
            location="http://10.0.0.103:3921/description.xml",
            udn="uuid:1234",
        )

    def test_alive_contains_location_and_usn(self) -> None:
        payload = self.ssdp._notify_payload("upnp:rootdevice", "ssdp:alive").decode()
        self.assertIn("LOCATION: http://10.0.0.103:3921/description.xml", payload)
        self.assertIn("USN: uuid:1234::upnp:rootdevice", payload)

    def test_matches_ssdp_all_and_specific_target(self) -> None:
        self.assertEqual(self.ssdp._matching_targets("ssdp:all"), self.ssdp.targets)
        self.assertEqual(
            self.ssdp._matching_targets(MEDIA_SERVER_DEVICE_TYPE),
            (MEDIA_SERVER_DEVICE_TYPE,),
        )
