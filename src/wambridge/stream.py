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
STARTUP_SILENCE_MS = 0
MAX_STARTUP_PAYLOAD_SIZE = 64 * 1024
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
    max_sample_rate: int | None = None
    """Highest rate confirmed on a physical M5, or None for a fixed-rate codec."""

    def args_for(self, sample_rate: int | None) -> tuple[str, ...]:
        """Return encoder arguments for one source rate.

        The rate is passed through untouched so a 44.1 kHz track is not
        needlessly resampled and a high-resolution one is not thrown away.
        Resampling is added only above the rate confirmed on the device.
        """

        if not sample_rate or self.max_sample_rate is None:
            return self.ffmpeg_args
        if sample_rate <= self.max_sample_rate:
            return self.ffmpeg_args
        return ("-ar", str(self.max_sample_rate), *self.ffmpeg_args)


OUTPUT_PROFILES: dict[str, OutputProfile] = {
    "flac": OutputProfile(
        extension="flac",
        content_type="audio/flac",
        # No fixed -ar or -sample_fmt. FLAC is lossless, so forcing 48000/s16
        # resampled every 44.1 kHz track for nothing and discarded anything
        # above CD depth. A physical M5 plays FLAC up to 96 kHz / 24-bit, and
        # FFmpeg negotiates a sample format the encoder supports on its own.
        ffmpeg_args=(
            "-vn",
            "-ac",
            "2",
            "-c:a",
            "flac",
            "-f",
            "flac",
        ),
        max_sample_rate=96000,
    ),
    "wav": OutputProfile(
        extension="wav",
        content_type="audio/wav",
        # Uncompressed 16-bit PCM, about twice FLAC's bitrate. The speaker's
        # prebuffer is partly bounded by bytes, so a fatter stream should hold
        # fewer seconds of audio and arrive at the ear sooner. `-fflags
        # +bitexact` drops FFmpeg's LIST/INFO chunk, leaving the plain 44-byte
        # header. Both size fields in it are 0xFFFFFFFF because the muxer
        # cannot seek back on a pipe; that is the streaming-WAV convention and
        # not a faked HTTP `Content-Length`, which the M5 does punish.
        # The rate is fixed rather than followed, unlike FLAC. WAV was confirmed
        # on a physical M5 at 44.1 kHz / 16-bit and at no other rate, and the
        # source rate is not always known: the file and URL paths call
        # `args_for(None)`, so a cap expressed as a maximum would not be applied
        # there at all and an unconfirmed 96 kHz stream would reach the speaker.
        # A fixed rate also keeps this profile at a constant 1411 kbps, which is
        # what makes it comparable against FLAC's variable bitrate.
        ffmpeg_args=(
            "-vn",
            "-ac",
            "2",
            "-ar",
            "44100",
            "-c:a",
            "pcm_s16le",
            "-fflags",
            "+bitexact",
            "-f",
            "wav",
        ),
    ),
    "wav24": OutputProfile(
        extension="wav",
        content_type="audio/wav",
        # The same lever as `wav`, pulled harder: 2117 kbps against 1411, so a
        # byte-bounded prebuffer should hold fewer seconds again. It also closes
        # the one place `wav` is worse than FLAC, which carries 24 bit through
        # this path while `wav` truncates to 16.
        #
        # Two unknowns at once, and they are worth separating if this fails on
        # hardware. Only 44.1 kHz / 16-bit WAV has ever been confirmed on this
        # firmware, and FFmpeg's WAV muxer additionally switches to
        # WAVE_FORMAT_EXTENSIBLE above 16 bits: the format tag becomes 0xFFFE
        # and the `fmt ` chunk grows from 16 bytes to 40. A speaker that refuses
        # this may be refusing the depth or the header shape, so try a 16-bit
        # extensible stream before concluding anything about 24-bit support.
        ffmpeg_args=(
            "-vn",
            "-ac",
            "2",
            "-ar",
            "44100",
            "-c:a",
            "pcm_s24le",
            "-fflags",
            "+bitexact",
            "-f",
            "wav",
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


def terminate_process(process: subprocess.Popen[bytes], *, timeout: float = 5.0) -> None:
    """Stop a helper process, escalating to kill when it ignores terminate.

    Nothing may return while an FFmpeg is still running: the physical test
    machine has already been taken down by leaked encoders.
    """
    if process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)


def _read_chunk(stream: BinaryIO, size: int) -> bytes:
    """Read currently available pipe data without waiting for a full buffer."""
    read1 = getattr(stream, "read1", None)
    if callable(read1):
        return read1(size)
    return stream.read(size)


def _contains_flac_audio_frame(payload: bytes) -> bool:
    """Return whether a native FLAC payload contains an audio frame."""
    if not payload.startswith(b"fLaC"):
        return False

    offset = 4
    while True:
        if len(payload) < offset + 4:
            return False
        block_header = payload[offset]
        block_size = int.from_bytes(payload[offset + 1 : offset + 4], "big")
        offset += 4
        if len(payload) < offset + block_size:
            return False
        offset += block_size
        if block_header & 0x80:
            break

    return any(
        payload[index] == 0xFF and payload[index + 1] & 0xFE == 0xF8
        for index in range(offset, len(payload) - 1)
    )


def _contains_wav_audio_frame(payload: bytes) -> bool:
    """Return whether a streamed WAV payload carries samples, not just a header."""
    if not payload.startswith(b"RIFF") or payload[8:12] != b"WAVE":
        return False

    offset = 12
    block_align = 1
    while len(payload) >= offset + 8:
        chunk_id = payload[offset : offset + 4]
        # Sizes are little-endian here, unlike FLAC's big-endian block headers.
        chunk_size = int.from_bytes(payload[offset + 4 : offset + 8], "little")
        offset += 8
        if chunk_id == b"fmt " and len(payload) >= offset + 14:
            # One frame is every channel's sample. A partial one is not audio,
            # and the FLAC check this mirrors looks for a whole frame too.
            block_align = int.from_bytes(payload[offset + 12 : offset + 14], "little") or 1
        elif chunk_id == b"data":
            # The muxer cannot seek back on a pipe, so this chunk's declared
            # size is 0xFFFFFFFF. Never skip past it; everything after the
            # header is audio.
            return len(payload) - offset >= block_align
        offset += chunk_size + (chunk_size % 2)

    return False


# Containers whose first bytes are a header rather than audio. Returning that
# header as the startup payload would fire AUDIO_STARTED, and with it the
# transport clock's anchor, before a single sample existed.
_AUDIO_FRAME_CHECKS = {
    "flac": _contains_flac_audio_frame,
    "wav": _contains_wav_audio_frame,
}


def _read_startup_payload(stream: BinaryIO, extension: str) -> bytes:
    """Read until output proves that encoded audio, not only headers, exists."""
    contains_audio = _AUDIO_FRAME_CHECKS.get(extension)
    payload = bytearray()
    while len(payload) < MAX_STARTUP_PAYLOAD_SIZE:
        remaining = MAX_STARTUP_PAYLOAD_SIZE - len(payload)
        chunk = _read_chunk(stream, min(STARTUP_CHUNK_SIZE, remaining))
        before = len(payload)
        payload.extend(chunk)
        # Exit on lack of progress, not on a falsy chunk. A stream that keeps
        # returning something truthy which adds no bytes would otherwise spin
        # forever, and this loop has no iteration limit to fall back on.
        if len(payload) == before:
            break
        if contains_audio is None or contains_audio(payload):
            return bytes(payload)

    if not payload:
        raise StreamError("FFmpeg produced no audio")
    if contains_audio is not None:
        raise StreamError(
            f"the source ended before FFmpeg produced a {extension.upper()} audio frame"
        )
    return bytes(payload)

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
            *self.profile.args_for(None),
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

    def _finish_request(self) -> None:
        """Record that the active HTTP stream request ended."""
        self.request_finished.set()

    def close(self) -> None:
        """Stop HTTP serving and any active FFmpeg process."""
        self._closing.set()
        self.audio_released.set()
        with self._process_lock:
            process = self._process
        if process and process.poll() is None:
            terminate_process(process, timeout=3)
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
                # The random path is the only thing standing between this LAN
                # server and the stream, so it is compared without leaking how
                # much of a guess was right.
                requested = self.path.encode("utf-8", errors="replace")
                if not secrets.compare_digest(requested, owner.path.encode()):
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
                    owner._finish_request()

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
            *self.profile.args_for(None),
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
        # Same proof the PCM path requires: a container header is not audio, and
        # `audio_started` below is what unmutes the speaker and anchors timing.
        try:
            first_chunk = _read_startup_payload(process.stdout, self.profile.extension)
        except StreamError as error:
            process.wait(timeout=5)
            raise StreamError(f"{error} (exit {process.returncode})") from error

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
            terminate_process(process)
            with self._process_lock:
                self._process = None
            if process.returncode not in {0, -15, 1}:
                LOGGER.error("FFmpeg exited with %s", process.returncode)

        if unexpected_eof:
            raise StreamError(
                "FFmpeg live stream ended unexpectedly "
                f"(exit {process.returncode})"
            )
