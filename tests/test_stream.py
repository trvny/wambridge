from io import BytesIO
from subprocess import CompletedProcess
from unittest import TestCase
from unittest.mock import Mock, patch

from wambridge.stream import (
    AudioStreamServer,
    StreamError,
    _read_chunk,
    continuous_source,
)


class ReadOnePipe:
    def __init__(self) -> None:
        self.requested_size: int | None = None

    def read1(self, size: int) -> bytes:
        self.requested_size = size
        return b"fLaC"

    def read(self, _size: int) -> bytes:
        raise AssertionError("read() should not be used when read1() is available")


class AudioStreamServerTests(TestCase):
    @patch("wambridge.stream.shutil.which", return_value="C:/ffmpeg/bin/ffmpeg.exe")
    @patch("wambridge.stream.subprocess.run")
    def test_prepare_rejects_invalid_audio(self, run_mock, _which_mock) -> None:
        run_mock.return_value = CompletedProcess([], 1, stderr=b"Invalid data")
        server = AudioStreamServer("broken.opus")
        try:
            with self.assertRaisesRegex(StreamError, "Invalid data"):
                server.prepare()
        finally:
            server.close()

    @patch("wambridge.stream.shutil.which", return_value="C:/ffmpeg/bin/ffmpeg.exe")
    def test_audio_stays_gated_until_released(self, _which_mock) -> None:
        server = AudioStreamServer("track.opus")
        try:
            self.assertFalse(server.audio_released.is_set())
            server.release_audio()
            self.assertTrue(server.audio_released.is_set())
        finally:
            server.close()

    @patch("wambridge.stream.shutil.which", return_value="C:/ffmpeg/bin/ffmpeg.exe")
    @patch("wambridge.stream.subprocess.Popen")
    def test_continuous_source_reports_unexpected_eof(
        self,
        popen_mock,
        _which_mock,
    ) -> None:
        process = Mock()
        process.stdout = BytesIO(b"fLaC")
        process.poll.return_value = 0
        process.wait.return_value = 0
        process.returncode = 0
        popen_mock.return_value = process

        with continuous_source("https://radio.example/live"):
            server = AudioStreamServer("https://radio.example/live")
        try:
            with self.assertRaisesRegex(StreamError, "ended unexpectedly"):
                server._serve_audio(BytesIO())
        finally:
            server.close()

    @patch("wambridge.stream.shutil.which", return_value="C:/ffmpeg/bin/ffmpeg.exe")
    def test_continuous_marker_is_scoped(self, _which_mock) -> None:
        with continuous_source("https://radio.example/live"):
            live_server = AudioStreamServer("https://radio.example/live")
        regular_server = AudioStreamServer("https://radio.example/live")
        try:
            self.assertTrue(live_server.continuous)
            self.assertFalse(regular_server.continuous)
        finally:
            live_server.close()
            regular_server.close()

    def test_reads_available_pipe_data_without_filling_buffer(self) -> None:
        pipe = ReadOnePipe()

        chunk = _read_chunk(pipe, 4096)  # type: ignore[arg-type]

        self.assertEqual(chunk, b"fLaC")
        self.assertEqual(pipe.requested_size, 4096)
