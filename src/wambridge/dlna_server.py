"""Range-capable HTTP server for one DLNA audio file."""

from __future__ import annotations

import logging
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .dlna import MP3_CONTENT_FEATURES, MP3_PROTOCOL_INFO

LOGGER = logging.getLogger(__name__)
COPY_CHUNK_SIZE = 64 * 1024


class ByteRangeError(ValueError):
    """Raised when an HTTP Range header cannot be satisfied."""


def parse_byte_range(value: str | None, size: int) -> tuple[int, int] | None:
    """Parse one RFC 7233 byte range and return inclusive bounds."""

    if value is None:
        return None
    if not value.startswith("bytes=") or "," in value:
        raise ByteRangeError("Only one byte range is supported")
    spec = value[6:].strip()
    if "-" not in spec:
        raise ByteRangeError("Invalid byte range")
    first, last = (part.strip() for part in spec.split("-", 1))
    if not first:
        try:
            suffix_length = int(last)
        except ValueError as error:
            raise ByteRangeError("Invalid suffix range") from error
        if suffix_length <= 0:
            raise ByteRangeError("Invalid suffix range")
        start = max(0, size - suffix_length)
        return start, size - 1
    try:
        start = int(first)
        end = size - 1 if not last else int(last)
    except ValueError as error:
        raise ByteRangeError("Invalid byte range") from error
    if start < 0 or end < start or start >= size:
        raise ByteRangeError("Unsatisfiable byte range")
    return start, min(end, size - 1)


class _DlnaHttpServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class DlnaFileServer:
    """Expose one local MP3 with DLNA headers and HTTP byte ranges."""

    def __init__(
        self,
        source: str | Path,
        *,
        bind: str = "0.0.0.0",  # nosec B104 - speaker must reach the LAN server
        port: int = 0,
    ) -> None:
        path = Path(source).expanduser().resolve()
        if not path.is_file():
            raise ValueError(f"DLNA source is not a file: {path}")
        if path.suffix.casefold() != ".mp3":
            raise ValueError("Initial DLNA playback supports local MP3 files only")

        self.source = path
        self.size = path.stat().st_size
        if self.size == 0:
            raise ValueError("DLNA source file is empty")
        self.token = secrets.token_hex(16).upper()
        self.request_started = threading.Event()
        self.request_finished = threading.Event()
        self._started = False
        self._closing = threading.Event()
        self._lock = threading.Lock()
        self._request_count = 0
        self._bytes_sent = 0
        self._server = _DlnaHttpServer((bind, port), self._make_handler())
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            daemon=True,
        )

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    @property
    def path(self) -> str:
        return f"/DLNA/{self.token}.mp3"

    @property
    def protocol_info(self) -> str:
        return MP3_PROTOCOL_INFO

    @property
    def request_count(self) -> int:
        with self._lock:
            return self._request_count

    @property
    def bytes_sent(self) -> int:
        with self._lock:
            return self._bytes_sent

    def url(self, host: str) -> str:
        return f"http://{host}:{self.port}{self.path}"

    def start(self) -> None:
        self._thread.start()
        self._started = True

    def close(self) -> None:
        self._closing.set()
        if self._started:
            self._server.shutdown()
            if self._thread.is_alive():
                self._thread.join(timeout=3)
        self._server.server_close()

    def _record_request(self, bytes_sent: int) -> None:
        with self._lock:
            self._request_count += 1
            self._bytes_sent += bytes_sent

    def _make_handler(self) -> type[BaseHTTPRequestHandler]:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_HEAD(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
                self._serve_file(include_body=False)

            def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
                self._serve_file(include_body=True)

            def _serve_file(self, *, include_body: bool) -> None:
                if urlsplit(self.path).path != owner.path:
                    self.send_error(404)
                    return

                try:
                    byte_range = parse_byte_range(
                        self.headers.get("Range"),
                        owner.size,
                    )
                except ByteRangeError:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{owner.size}")
                    self.send_header("Content-Length", "0")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    return

                if byte_range is None:
                    start, end = 0, owner.size - 1
                    status = 200
                else:
                    start, end = byte_range
                    status = 206
                length = end - start + 1

                self.send_response(status)
                self.send_header("Content-Type", "audio/mpeg")
                self.send_header("Content-Length", str(length))
                self.send_header("Accept-Ranges", "bytes")
                if status == 206:
                    self.send_header(
                        "Content-Range",
                        f"bytes {start}-{end}/{owner.size}",
                    )
                self.send_header("transferMode.dlna.org", "Streaming")
                self.send_header(
                    "contentFeatures.dlna.org",
                    MP3_CONTENT_FEATURES,
                )
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.send_header("EXT", "")
                self.end_headers()

                if not include_body:
                    return

                owner.request_started.set()
                sent = 0
                try:
                    with owner.source.open("rb") as stream:
                        stream.seek(start)
                        remaining = length
                        while remaining and not owner._closing.is_set():
                            chunk = stream.read(min(COPY_CHUNK_SIZE, remaining))
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                            sent += len(chunk)
                            remaining -= len(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    LOGGER.info("Speaker closed DLNA file request")
                finally:
                    owner._record_request(sent)
                    owner.request_finished.set()

            def log_message(
                self,
                format_string: str,
                *args: object,
            ) -> None:
                LOGGER.debug("DLNA HTTP: " + format_string, *args)

        return Handler
