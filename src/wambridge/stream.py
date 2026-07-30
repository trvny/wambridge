"""Local HTTP audio stream backed by FFmpeg."""

from __future__ import annotations

import logging
import secrets
import shutil
import subprocess
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import BinaryIO

LOGGER = logging.getLogger(__name__)
CHUNK_SIZE = 64 * 1024
STARTUP_CHUNK_SIZE = 4096
STARTUP_SILENCE_MS = 1500
_CONTINUOUS_SOURCES: ContextVar[frozenset[str]] = ContextVar(
    "wambridge_continuous_sources",
    default=frozenset(),
)


@dataclass(frozen=True, slots=True)
class OutputProfile:
    """FFmpeg output settings understood by Samsung WAM speakers."""

    extension: str
    content_type: str
    ffmpeg_args: tuple[str, ...]


OUTPUT_PROFILES: dict[str, OutputProfile] = {
    "flac": OutputProfile(
        extension="flac",
        content_type="audio/flac",
        ffmpeg_args=(
            "-vn",
            "-ac",
            "2",
            "-ar",
            "48000",
            "-sample_fmt",
            "s16",
            "-c:a",
            "flac",
            "-f",
            "flac",
        ),
    ),
    "mp3": OutputProfile(
        extension="mp3",
        content_type="audio/mpeg",
        ffmpeg_args=(
            "-vn",
            "-ac",
            "2",
            "-ar",
            "48000",
            "-c:a",
            "libmp3lame",
            "-b:a",
            "320k",
            "-f",
            "mp3",
        ),
    ),
}


class StreamError(RuntimeError):
    """Raised when the local stream cannot start or ends unexpectedly."""


@contextmanager
def continuous_source(source: str) -> Iterator[None]:
    """Mark one source as live for the duration of a bridge session."""
    sources = _CONTINUOUS_SOURCES.get()
    token = _CONTINUOUS_SOURCES.set(sources | {source})
    try:
        yield
    finally:
        _CONTINUOUS_SOURCES.reset(token)


def _read_chunk(stream: BinaryIO, size: int) -> bytes:
    """Read currently available pipe data without waiting for a full buffer."""
    read1 = getattr(stream, "read1", None)
    if callable(read1):
        return read1(size)
    return stream.read(size)


class AudioStreamServer:
    """Serve one tokenized real-time audio stream to a WAM speaker."""

    def __init__(
        self,
        source: str,
        *,
        profile: str = "flac",
        bind: str = "0.0.0.0",  # nosec B104 - WAM must reach the LAN server
        port: int = 0,
        ffmpeg: str = "ffmpeg",
    ) -> None:
        if profile not in OUTPUT_PROFILES:
            raise ValueError(f"Unknown output profile: {profile}")
        resolved_ffmpeg = shutil.which(ffmpeg)
        if not resolved_ffmpeg:
            raise StreamError(f"FFmpeg executable not found: {ffmpeg}")

        self.source = source
        self.profile = OUTPUT_PROFILES[profile]
        self.ffmpeg = resolved_ffmpeg
        self.continuous = source in _CONTINUOUS_SOURCES.get()
        self.token = secrets.token_urlsafe(24)
        self.request_started = threading.Event()
        self.request_finished = threading.Event()
        self.audio_released = threading.Event()
        self.audio_started = threading.Event()
        self.error: str | None = None
        self._process: subprocess.Popen[bytes] | None = None
        self._started = False
        self._closing = threading.Event()
        self._process_lock = threading.Lock()
        self._server = ThreadingHTTPServer((bind, port), self._make_handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        """Return the bound TCP port."""
        return int(self._server.server_address[1])

    @property
    def path(self) -> str:
        """Return the random URL path expected by the speaker."""
        return f"/stream/{self.token}.{self.profile.extension}"

    def url(self, host: str) -> str:
        """Build a URL reachable by the speaker."""
        return f"http://{host}:{self.port}{self.path}"

    def prepare(self, timeout: float = 20.0) -> None:
        """Verify that FFmpeg can decode and encode a short audio sample."""
        command = [
            self.ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            self.source,
            "-map",
            "0:a:0",
            "-t",
            "0.25",
            *self.profile.ffmpeg_args,
            "pipe:1",
        ]
        try:
            result = subprocess.run(  # nosec B603 - argv list, resolved executable
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=timeout,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired as error:
            raise StreamError(
                f"FFmpeg source check timed out after {timeout:g} seconds"
            ) from error
        if result.returncode != 0:
            details = result.stderr.decode("utf-8", errors="replace").strip()
            raise StreamError(
                f"FFmpeg cannot prepare the source: {details or 'unknown error'}"
            )

    def start(self) -> None:
        """Start accepting HTTP connections."""
        self._thread.start()
        self._started = True

    def release_audio(self) -> None:
        """Allow a connected speaker to receive audio after safety checks."""
        self.audio_released.set()

    def close(self) -> None:
        """Stop HTTP serving and any active FFmpeg process."""
        self._closing.set()
        self.audio_released.set()
        with self._process_lock:
            process = self._process
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        if self._started:
            self._server.shutdown()
            if self._thread.is_alive():
                self._thread.join(timeout=3)
        self._server.server_close()

    def _make_handler(self) -> type[BaseHTTPRequestHandler]:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
                if self.path != owner.path:
                    self.send_error(404)
                    return

                self.send_response(200)
                self.send_header("Content-Type", owner.profile.content_type)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.end_headers()
                owner.request_started.set()

                try:
                    owner.audio_released.wait()
                    if owner._closing.is_set():
                        return
                    owner._serve_audio(self.wfile)
                except Exception as error:  # HTTP worker boundary
                    owner.error = str(error)
                    LOGGER.exception("Audio stream failed")
                finally:
                    owner.request_finished.set()

            def log_message(self, format_string: str, *args: object) -> None:
                LOGGER.debug("HTTP: " + format_string, *args)

        return Handler

    def _serve_audio(self, output: BinaryIO) -> None:
        command = [
            self.ffmpeg,
            "-hide_banner",
            "-loglevel",
            "warning",
            "-re",
            "-i",
            self.source,
            "-af",
            f"adelay={STARTUP_SILENCE_MS}:all=1",
            *self.profile.ffmpeg_args,
            "pipe:1",
        ]
        LOGGER.info("Starting FFmpeg for %s", self.source)
        process = subprocess.Popen(  # nosec B603 - argv list, resolved executable
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        with self._process_lock:
            self._process = process

        assert process.stdout is not None
        first_chunk = _read_chunk(process.stdout, STARTUP_CHUNK_SIZE)
        if not first_chunk:
            process.wait(timeout=5)
            raise StreamError(f"FFmpeg produced no audio (exit {process.returncode})")

        unexpected_eof = False
        try:
            output.write(first_chunk)
            output.flush()
            self.audio_started.set()
            while chunk := _read_chunk(process.stdout, CHUNK_SIZE):
                output.write(chunk)
                output.flush()
            unexpected_eof = self.continuous and not self._closing.is_set()
        except (BrokenPipeError, ConnectionResetError):
            LOGGER.info("Speaker closed the stream")
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
            with self._process_lock:
                self._process = None
            if process.returncode not in {0, -15, 1}:
                LOGGER.error("FFmpeg exited with %s", process.returncode)

        if unexpected_eof:
            raise StreamError(
                "FFmpeg live stream ended unexpectedly "
                f"(exit {process.returncode})"
            )
