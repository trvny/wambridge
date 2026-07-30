from io import BytesIO
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from wambridge.pcm_stream import PcmAudioStreamServer
from wambridge.stream import StreamError


class PcmAudioStreamServerTests(TestCase):
    @patch("wambridge.stream.shutil.which", return_value="C:/ffmpeg/bin/ffmpeg.exe")
    def test_builds_raw_pcm_input_arguments(self, _which_mock) -> None:
        pcm_input = BytesIO()
        server = PcmAudioStreamServer(
            pcm_input,
            sample_rate=44100,
            channels=2,
            sample_format="f32le",
        )
        try:
            self.assertEqual(
                server.input_args,
                (
                    "-f",
                    "f32le",
                    "-ar",
                    "44100",
                    "-ac",
                    "2",
                    "-i",
                    "pipe:0",
                ),
            )
            self.assertIs(server.pcm_input, pcm_input)
            self.assertFalse(server.encoder_started.is_set())
        finally:
            server.close()

    @patch("wambridge.stream.shutil.which", return_value="ffmpeg")
    def test_rejects_invalid_pcm_shape(self, _which_mock) -> None:
        with self.assertRaisesRegex(ValueError, "sample rate"):
            PcmAudioStreamServer(BytesIO(), sample_rate=0, channels=2)
        with self.assertRaisesRegex(ValueError, "channel count"):
            PcmAudioStreamServer(BytesIO(), sample_rate=48000, channels=0)
        with self.assertRaisesRegex(ValueError, "unsupported PCM format"):
            PcmAudioStreamServer(
                BytesIO(),
                sample_rate=48000,
                channels=2,
                sample_format="u8",
            )

    @patch("wambridge.pcm_stream._read_chunk")
    @patch("wambridge.pcm_stream.subprocess.Popen")
    @patch("wambridge.stream.shutil.which", return_value="ffmpeg")
    def test_applies_realtime_clock_before_pcm_input(
        self,
        _which_mock,
        popen_mock,
        read_mock,
    ) -> None:
        metadata = b"fLaC" + bytes([0x80, 0, 0, 34]) + bytes(34)
        read_mock.side_effect = [metadata + b"\xff\xf8\x00\x00", b""]
        process = SimpleNamespace(
            stdout=BytesIO(),
            returncode=0,
            poll=lambda: None,
            wait=lambda timeout: 0,
            terminate=lambda: None,
            kill=lambda: None,
        )
        popen_mock.return_value = process
        server = PcmAudioStreamServer(
            BytesIO(b"pcm"),
            sample_rate=48000,
            channels=2,
        )
        try:
            server._serve_audio(BytesIO())
            command = popen_mock.call_args.args[0]
            self.assertLess(command.index("-re"), command.index("-i"))
            self.assertEqual(command[command.index("-ar") + 1], "48000")
        finally:
            server.close()

    @patch("wambridge.pcm_stream._read_chunk")
    @patch("wambridge.pcm_stream.subprocess.Popen")
    @patch("wambridge.stream.shutil.which", return_value="ffmpeg")
    def test_rejects_header_only_flac_while_process_is_alive(
        self,
        _which_mock,
        popen_mock,
        read_mock,
    ) -> None:
        metadata_only = b"fLaC" + bytes([0x80, 0, 0, 34]) + bytes(34)
        read_mock.side_effect = [metadata_only, b""]
        process = SimpleNamespace(
            stdout=BytesIO(),
            returncode=0,
            poll=lambda: None,
            wait=lambda timeout: 0,
            terminate=lambda: None,
            kill=lambda: None,
        )
        popen_mock.return_value = process
        server = PcmAudioStreamServer(
            BytesIO(),
            sample_rate=48000,
            channels=2,
        )
        try:
            with self.assertRaisesRegex(StreamError, "FLAC audio frame"):
                server._serve_audio(BytesIO())
            self.assertTrue(server.encoder_started.is_set())
            self.assertFalse(server.audio_started.is_set())
        finally:
            server.close()

    @patch("wambridge.pcm_stream._read_chunk")
    @patch("wambridge.pcm_stream.subprocess.Popen")
    @patch("wambridge.stream.shutil.which", return_value="ffmpeg")
    def test_accepts_flac_after_metadata_and_frame_sync(
        self,
        _which_mock,
        popen_mock,
        read_mock,
    ) -> None:
        metadata = b"fLaC" + bytes([0x80, 0, 0, 34]) + bytes(34)
        frame = b"\xff\xf8\x00\x00"
        read_mock.side_effect = [metadata, frame, b""]
        process = SimpleNamespace(
            stdout=BytesIO(),
            returncode=0,
            poll=lambda: None,
            wait=lambda timeout: 0,
            terminate=lambda: None,
            kill=lambda: None,
        )
        popen_mock.return_value = process
        server = PcmAudioStreamServer(
            BytesIO(b"pcm"),
            sample_rate=48000,
            channels=2,
        )
        output = BytesIO()
        try:
            server._serve_audio(output)
            self.assertEqual(output.getvalue(), metadata + frame)
            self.assertTrue(server.encoder_started.is_set())
            self.assertTrue(server.audio_started.is_set())
        finally:
            server.close()
