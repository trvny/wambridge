from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch

from wambridge.dms_browse import SamsungBrowseServer
from wambridge.dms_entry import _run_ladder


def browse_payload(object_id: str, flag: str) -> bytes:
    return f'''<?xml version="1.0"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">
 <s:Body>
  <u:Browse xmlns:u="urn:schemas-upnp-org:service:ContentDirectory:1">
   <ObjectID>{object_id}</ObjectID>
   <BrowseFlag>{flag}</BrowseFlag>
   <Filter>*</Filter>
   <StartingIndex>0</StartingIndex>
   <RequestedCount>1</RequestedCount>
   <SortCriteria></SortCriteria>
  </u:Browse>
 </s:Body>
</s:Envelope>'''.encode()


class SamsungBrowseServerTests(TestCase):
    def test_uses_numeric_item_id_and_standard_root(self) -> None:
        with TemporaryDirectory() as directory:
            source = Path(directory) / "track.mp3"
            source.write_bytes(b"test")
            server = SamsungBrowseServer(source, bind="127.0.0.1", port=0)
            try:
                self.assertEqual(server.object_id, "1")
                self.assertEqual(server.path, "/DLNA/1.mp3")

                metadata, returned, total, fields = server._browse_result(
                    "127.0.0.1",
                    browse_payload("0", "BrowseMetadata"),
                )
                self.assertEqual(fields["ObjectID"], "0")
                self.assertEqual((returned, total), (1, 1))
                self.assertIn("container", metadata)
                self.assertIn('childCount="1"', metadata)
            finally:
                server.close()

    def test_root_children_return_the_audio_item(self) -> None:
        with TemporaryDirectory() as directory:
            source = Path(directory) / "track.mp3"
            source.write_bytes(b"test")
            server = SamsungBrowseServer(source, bind="127.0.0.1", port=0)
            try:
                children, returned, total, fields = server._browse_result(
                    "127.0.0.1",
                    browse_payload("0", "BrowseDirectChildren"),
                )
                self.assertEqual(fields["BrowseFlag"], "BrowseDirectChildren")
                self.assertEqual((returned, total), (1, 1))
                self.assertIn('id="1"', children)
                self.assertIn(server.url("127.0.0.1"), children)
            finally:
                server.close()

    @patch("wambridge.dms_entry.sleep")
    @patch("wambridge.dms_cli._start_folder_fallback", return_value=True)
    @patch("wambridge.dms_cli._start_share", return_value=True)
    @patch("wambridge.dms_cli._register_server", return_value=True)
    def test_ladder_continues_after_browse_without_media(
        self,
        register_mock,
        share_mock,
        folder_mock,
        _sleep_mock,
    ) -> None:
        server = Mock()
        server.udn = "uuid:1234"
        server.uuid = "1234"
        server.has_contact = True
        server.browse_requested.is_set.return_value = True
        server.request_started.wait.side_effect = [False, False, False]
        ssdp = Mock()

        accepted = _run_ladder(
            "10.0.0.118",
            55001,
            server,
            "10.0.0.103",
            ssdp,
        )

        self.assertTrue(accepted)
        self.assertEqual(register_mock.call_count, 2)
        self.assertEqual(share_mock.call_count, 2)
        folder_mock.assert_called_once()
        self.assertEqual(ssdp.announce.call_count, 3)
