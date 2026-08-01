"""Tests for the share playback media server and its identifier rules."""

from __future__ import annotations

import unittest
import urllib.error
import urllib.request
from pathlib import Path
from tempfile import TemporaryDirectory

from wambridge.samsung import play_share, register_share_source
from wambridge.share import (
    ByteRangeError,
    ShareServer,
    UnsupportedMediaError,
    content_features,
    media_type,
    object_id_for,
    parse_byte_range,
)


class ParseByteRangeTests(unittest.TestCase):
    def test_absent_header_returns_none(self) -> None:
        self.assertIsNone(parse_byte_range(None, 100))

    def test_open_ended_range(self) -> None:
        self.assertEqual(parse_byte_range("bytes=0-", 100), (0, 99))

    def test_closed_range_is_clamped(self) -> None:
        self.assertEqual(parse_byte_range("bytes=10-500", 100), (10, 99))

    def test_suffix_range(self) -> None:
        self.assertEqual(parse_byte_range("bytes=-20", 100), (80, 99))

    def test_multiple_ranges_rejected(self) -> None:
        with self.assertRaises(ByteRangeError):
            parse_byte_range("bytes=0-1,5-6", 100)

    def test_start_beyond_size_rejected(self) -> None:
        with self.assertRaises(ByteRangeError):
            parse_byte_range("bytes=200-", 100)


class MediaTypeTests(unittest.TestCase):
    def test_known_containers(self) -> None:
        self.assertEqual(media_type(Path("a.mp3")), ("audio/mpeg", "MP3"))
        self.assertEqual(media_type(Path("a.flac")), ("audio/flac", "FLAC"))
        self.assertEqual(media_type(Path("a.wav")), ("audio/wav", "LPCM"))

    def test_opus_is_rejected(self) -> None:
        # Measured: the firmware retries five times and reports ErrorEvent.
        with self.assertRaises(UnsupportedMediaError):
            media_type(Path("a.opus"))

    def test_content_features_advertises_range_support(self) -> None:
        self.assertIn("DLNA.ORG_OP=01", content_features("MP3"))

    def test_object_id_is_flat_and_keeps_extension(self) -> None:
        object_id = object_id_for(Path("a.mp3"))
        self.assertTrue(object_id.endswith(".mp3"))
        self.assertNotIn("/", object_id)
        self.assertEqual(object_id, object_id.upper().replace(".MP3", ".mp3"))


class IdentifierRuleTests(unittest.TestCase):
    """The uuid: prefix makes the firmware ignore the command silently."""

    def test_play_share_rejects_prefixed_udn(self) -> None:
        with self.assertRaises(ValueError):
            play_share("10.0.0.1", device_udn="uuid:abc", object_id="A.mp3")

    def test_registration_rejects_prefixed_uuid(self) -> None:
        with self.assertRaises(ValueError):
            register_share_source("10.0.0.1", "uuid:abc", "10.0.0.2:49200")

    def test_registration_requires_host_and_port(self) -> None:
        with self.assertRaises(ValueError):
            register_share_source("10.0.0.1", "abc", "10.0.0.2")


class ShareServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.path = Path(self._tmp.name) / "track.mp3"
        self.path.write_bytes(bytes(range(256)) * 4)
        self.server = ShareServer(self.path, port=0)
        self.server.start()
        self.base = f"http://127.0.0.1:{self.server.port}"

    def tearDown(self) -> None:
        self.server.close()
        self._tmp.cleanup()

    def _url(self) -> str:
        return f"{self.base}/DLNA/{self.server.object_id}"

    def test_serves_object_under_dlna_prefix(self) -> None:
        with urllib.request.urlopen(self._url(), timeout=5) as response:
            body = response.read()
            self.assertEqual(response.status, 200)
            self.assertEqual(body, self.path.read_bytes())
            self.assertEqual(response.headers["Accept-Ranges"], "bytes")
            self.assertIn("DLNA.ORG_PN=MP3", response.headers["contentFeatures.dlna.org"])

    def test_root_path_is_not_served(self) -> None:
        # Serving from the root is what produced URL_OPEN_FAIL on the device.
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(f"{self.base}/{self.server.object_id}", timeout=5)
        self.assertEqual(caught.exception.code, 404)

    def test_range_request_returns_partial_content(self) -> None:
        request = urllib.request.Request(self._url(), headers={"Range": "bytes=10-19"})
        with urllib.request.urlopen(request, timeout=5) as response:
            self.assertEqual(response.status, 206)
            self.assertEqual(response.read(), self.path.read_bytes()[10:20])
            self.assertEqual(
                response.headers["Content-Range"],
                f"bytes 10-19/{self.server.size}",
            )

    def test_unsatisfiable_range_returns_416(self) -> None:
        request = urllib.request.Request(
            self._url(), headers={"Range": "bytes=99999-"}
        )
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(caught.exception.code, 416)

    def test_requests_are_counted(self) -> None:
        self.assertFalse(self.server.requested.is_set())
        with urllib.request.urlopen(self._url(), timeout=5) as response:
            response.read()
        self.assertTrue(self.server.requested.is_set())
        self.assertEqual(self.server.request_count, 1)


if __name__ == "__main__":
    unittest.main()
