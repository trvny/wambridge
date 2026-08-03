"""Serve one local file to a Samsung WAM speaker over the share playback path.

The speaker pulls the object over HTTP from ``/DLNA/<object id>`` after being
pointed at it with ``SetSharePlaybackControl``. Both details were measured on a
physical M5; see ``docs/WAM_PROTOCOL.md``.

``parse_byte_range`` is carried over from the earlier DLNA experiment because
range support is mandatory: MP4 containers make the speaker issue three requests
while it locates the ``moov`` atom.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

LOGGER = logging.getLogger(__name__)

COPY_CHUNK_SIZE = 64 * 1024
DEFAULT_SHARE_PORT = 49200
SHARE_PATH_PREFIX = "/DLNA/"

# DLNA.ORG_OP=01 advertises range support, which the speaker uses for MP4.
_DLNA_FLAGS = "01700000000000000000000000000000"

_MEDIA_TYPES: dict[str, tuple[str, str]] = {
    ".mp3": ("audio/mpeg", "MP3"),
    ".wav": ("audio/wav", "LPCM"),
    ".flac": ("audio/flac", "FLAC"),
    ".m4a": ("audio/mp4", "AAC_ISO_320"),
    ".aac": ("audio/aac", "AAC_ISO_320"),
}


class ByteRangeError(ValueError):
    """Raised when an HTTP Range header cannot be satisfied."""


class UnsupportedMediaError(ValueError):
    """Raised for a container the tested firmware cannot play."""


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


def media_type(path: Path) -> tuple[str, str]:
    """Return the MIME type and DLNA profile name for a media file."""

    suffix = path.suffix.lower()
    try:
        return _MEDIA_TYPES[suffix]
    except KeyError:
        raise UnsupportedMediaError(
            f"{suffix or path.name} is not supported by the tested firmware"
        ) from None


def object_id_for(path: Path, *, salt: bytes | None = None) -> str:
    """Build the flat object identifier the official application uses.

    The captured session used ``<uppercase hash>.<extension>`` served from a
    single flat namespace, with no parent hierarchy. The shape is kept, but the
    digest is salted with fresh random bytes unless a salt is supplied: the
    identifier is the whole of the media server's access control, and a plain
    hash of the file path is reproducible by anyone who can guess the path.
    """

    material = secrets.token_bytes(16) if salt is None else salt
    digest = hashlib.sha256(material + str(path.resolve()).encode("utf-8")).hexdigest()
    return f"{digest[:32].upper()}{path.suffix.lower()}"


def content_features(profile: str) -> str:
    """Build the ``contentFeatures.dlna.org`` header value."""

    return (
        f"DLNA.ORG_PN={profile};DLNA.ORG_OP=01;DLNA.ORG_CI=0;"
        f"DLNA.ORG_FLAGS={_DLNA_FLAGS}"
    )


class ShareServer:
    """Serve exactly one media file at ``/DLNA/<object id>``."""

    def __init__(self, media_path: Path, *, port: int = DEFAULT_SHARE_PORT) -> None:
        self.path = Path(media_path)
        if not self.path.is_file():
            raise FileNotFoundError(self.path)
        self.mime, self.profile = media_type(self.path)
        self.object_id = object_id_for(self.path)
        self.size = self.path.stat().st_size
        self.requested = threading.Event()
        self._requests = 0
        self._lock = threading.Lock()
        self._server = ThreadingHTTPServer(("0.0.0.0", port), self._build_handler())  # noqa: S104
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    @property
    def request_count(self) -> int:
        with self._lock:
            return self._requests

    def url(self, host_ip: str) -> str:
        return f"http://{host_ip}:{self.port}{SHARE_PATH_PREFIX}{self.object_id}"

    def start(self) -> None:
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        LOGGER.info("Serving %s as %s", self.path.name, self.object_id)

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5.0)

    def __enter__(self) -> ShareServer:
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _note_request(self) -> None:
        with self._lock:
            self._requests += 1
        self.requested.set()

    def _build_handler(self) -> type[BaseHTTPRequestHandler]:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_HEAD(self) -> None:  # noqa: N802
                self._serve(include_body=False)

            def do_GET(self) -> None:  # noqa: N802
                self._serve(include_body=True)

            def _serve(self, *, include_body: bool) -> None:
                requested = unquote(urlsplit(self.path).path)
                expected = f"{SHARE_PATH_PREFIX}{owner.object_id}"
                if not secrets.compare_digest(
                    requested.encode("utf-8", errors="replace"),
                    expected.encode(),
                ):
                    self.send_error(404)
                    return
                owner._note_request()
                try:
                    byte_range = parse_byte_range(self.headers.get("Range"), owner.size)
                except ByteRangeError:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{owner.size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return

                if byte_range is None:
                    start, end, status = 0, owner.size - 1, 200
                else:
                    start, end = byte_range
                    status = 206
                length = end - start + 1

                self.send_response(status)
                self.send_header("Content-Type", owner.mime)
                self.send_header("Content-Length", str(length))
                self.send_header("Accept-Ranges", "bytes")
                if status == 206:
                    self.send_header(
                        "Content-Range", f"bytes {start}-{end}/{owner.size}"
                    )
                self.send_header("transferMode.dlna.org", "Streaming")
                self.send_header(
                    "contentFeatures.dlna.org", content_features(owner.profile)
                )
                self.end_headers()
                if not include_body:
                    return
                self._copy(start, length)

            def _copy(self, start: int, length: int) -> None:
                remaining = length
                try:
                    with owner.path.open("rb") as source:
                        source.seek(start)
                        while remaining > 0:
                            chunk = source.read(min(COPY_CHUNK_SIZE, remaining))
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                            remaining -= len(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    # The speaker closes the socket on stop or seek.
                    LOGGER.debug("Speaker closed the media connection")

            def log_message(self, *_args: object) -> None:
                return

        return Handler
