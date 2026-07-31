from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from wambridge.dlna_server import DlnaFileServer, parse_byte_range


class ByteRangeTests(TestCase):
    def test_parses_regular_and_suffix_ranges(self) -> None:
        self.assertEqual(parse_byte_range("bytes=2-5", 10), (2, 5))
        self.assertEqual(parse_byte_range("bytes=7-", 10), (7, 9))
        self.assertEqual(parse_byte_range("bytes=-3", 10), (7, 9))


class DlnaFileServerTests(TestCase):
    def test_serves_content_length_and_byte_ranges(self) -> None:
        with TemporaryDirectory() as directory:
            source = Path(directory) / "track.mp3"
            source.write_bytes(b"0123456789")
            server = DlnaFileServer(source, bind="127.0.0.1")
            server.start()
            try:
                request = Request(
                    server.url("127.0.0.1"),
                    headers={"Range": "bytes=2-5"},
                )
                with urlopen(request, timeout=2) as response:
                    body = response.read()
                    self.assertEqual(response.status, 206)
                    self.assertEqual(response.headers["Content-Length"], "4")
                    self.assertEqual(
                        response.headers["Content-Range"],
                        "bytes 2-5/10",
                    )
                    self.assertEqual(response.headers["Accept-Ranges"], "bytes")
                    self.assertEqual(
                        response.headers["transferMode.dlna.org"],
                        "Streaming",
                    )
                self.assertEqual(body, b"2345")
                self.assertTrue(server.request_started.is_set())
            finally:
                server.close()

    def test_rejects_unsatisfiable_range(self) -> None:
        with TemporaryDirectory() as directory:
            source = Path(directory) / "track.mp3"
            source.write_bytes(b"0123456789")
            server = DlnaFileServer(source, bind="127.0.0.1")
            server.start()
            try:
                request = Request(
                    server.url("127.0.0.1"),
                    headers={"Range": "bytes=99-100"},
                )
                with self.assertRaises(HTTPError) as raised:
                    urlopen(request, timeout=2)
                self.assertEqual(raised.exception.code, 416)
                self.assertEqual(
                    raised.exception.headers["Content-Range"],
                    "bytes */10",
                )
            finally:
                server.close()
