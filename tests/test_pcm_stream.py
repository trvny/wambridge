from io import BytesIO
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from wambridge.pcm_stream import PcmAudioStreamServer, _read_startup_payload
from wambridge.stream import StreamError


def _wav_header(extra_chunks: bytes = b"") -> bytes:
    """Build a streamed WAV header, sizes unknown exactly as FFmpeg leaves them."""
    unknown = b"\xff\xff\xff\xff"
    fmt = (
        b"fmt "
        + (16).to_bytes(4, "little")
        + (1).to_bytes(2, "little")  # PCM
        + (2).to_bytes(2, "little")  # channels
        + (44100).to_bytes(4, "little")
        + (176400).to_bytes(4, "little")
        + (4).to_bytes(2, "little")
        + (16).to_bytes(2, "little")
    )
    return b"RIFF" + unknown + b"WAVE" + fmt + extra_chunks + b"data" + unknown


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

    @patch("wambridge.pcm_stream.subprocess.Popen")
    @patch("wambridge.stream.shutil.which", return_value="ffmpeg")
    def test_applies_realtime_clock_before_pcm_input(
        self,
        _which_mock,
        popen_mock,
    ) -> None:
        metadata = b"fLaC" + bytes([0x80, 0, 0, 34]) + bytes(34)
        process = SimpleNamespace(
            stdout=BytesIO(metadata + b"\xff\xf8\x00\x00"),
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
            # The speaker paces itself through TCP backpressure; an FFmpeg clock
            # on an already real-time pipe only adds drift.
            self.assertNotIn("-re", command)
            self.assertEqual(command[command.index("-ar") + 1], "48000")
        finally:
            server.close()

    @patch("wambridge.pcm_stream.subprocess.Popen")
    @patch("wambridge.stream.shutil.which", return_value="ffmpeg")
    def test_rejects_header_only_flac_while_process_is_alive(
        self,
        _which_mock,
        popen_mock,
    ) -> None:
        metadata_only = b"fLaC" + bytes([0x80, 0, 0, 34]) + bytes(34)
        process = SimpleNamespace(
            stdout=BytesIO(metadata_only),
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

    @patch("wambridge.pcm_stream.subprocess.Popen")
    @patch("wambridge.stream.shutil.which", return_value="ffmpeg")
    def test_second_request_is_refused_while_one_encoder_serves(
        self,
        _which_mock,
        popen_mock,
    ) -> None:
        # Every encoder inherits the same stdin, so only one may run. The speaker
        # issues a second request right after the first while the first is still
        # the live one, so the newcomer must be refused -- retiring the older one
        # kills the stream actually being served and starves its replacement.
        terminated: list[str] = []
        live = SimpleNamespace(
            stdout=BytesIO(),
            returncode=0,
            poll=lambda: None,  # still running
            wait=lambda timeout: 0,
            terminate=lambda: terminated.append("live"),
            kill=lambda: terminated.append("killed"),
        )
        popen_mock.side_effect = AssertionError("must not start a second encoder")

        server = PcmAudioStreamServer(
            BytesIO(),
            sample_rate=44100,
            channels=2,
        )
        try:
            server._process = live
            with self.assertRaisesRegex(StreamError, "already being served"):
                server._serve_audio(BytesIO())
            self.assertEqual(terminated, [])
            self.assertIs(server._process, live)
        finally:
            server.close()

    @patch("wambridge.pcm_stream.subprocess.Popen")
    @patch("wambridge.stream.shutil.which", return_value="ffmpeg")
    def test_accepts_flac_after_metadata_and_frame_sync(
        self,
        _which_mock,
        popen_mock,
    ) -> None:
        metadata = b"fLaC" + bytes([0x80, 0, 0, 34]) + bytes(34)
        frame = b"\xff\xf8\x00\x00"
        process = SimpleNamespace(
            stdout=BytesIO(metadata + frame),
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


class StartupPayloadProgressTests(TestCase):
    """The startup read must end on lack of progress, not on a falsy chunk.

    A stream returning something truthy that adds no bytes used to spin forever:
    the loop only broke on a falsy chunk and had no iteration limit. Mocking
    ``Popen().stdout`` with a bare ``MagicMock`` hit exactly that and consumed
    25 GB before the machine ran out of commit charge.
    """

    def test_truthy_chunk_that_adds_no_bytes_terminates(self) -> None:
        class NeverGrows:
            """Truthy on every read, but yields nothing when extended."""

            def __init__(self) -> None:
                self.reads = 0

            def read1(self, _size: int) -> object:
                self.reads += 1
                if self.reads > 10_000:  # pragma: no cover - guards the guard
                    raise AssertionError("read loop did not terminate")
                return _TruthyEmpty()

        class _TruthyEmpty:
            def __bool__(self) -> bool:
                return True

            def __iter__(self):
                return iter(())

        stream = NeverGrows()
        with self.assertRaises(StreamError):
            _read_startup_payload(stream, "flac")
        self.assertLess(stream.reads, 10_000)

    def test_empty_stream_still_reports_no_audio(self) -> None:
        with self.assertRaises(StreamError) as caught:
            _read_startup_payload(BytesIO(b""), "flac")
        self.assertIn("no audio", str(caught.exception))

    def test_non_flac_payload_is_returned(self) -> None:
        self.assertEqual(_read_startup_payload(BytesIO(b"abc"), "mp3"), b"abc")

    def test_wav_header_alone_is_not_audio(self) -> None:
        # A bare header would otherwise fire AUDIO_STARTED, anchoring the
        # transport clock before a single sample was encoded.
        with self.assertRaisesRegex(StreamError, "WAV audio frame"):
            _read_startup_payload(BytesIO(_wav_header()), "wav")

    def test_wav_payload_with_samples_is_returned(self) -> None:
        payload = _wav_header() + b"\x01\x02\x03\x04"
        self.assertEqual(_read_startup_payload(BytesIO(payload), "wav"), payload)

    def test_wav_partial_frame_is_not_audio(self) -> None:
        # 16-bit stereo is four bytes per frame; three of them are not a sample.
        with self.assertRaisesRegex(StreamError, "WAV audio frame"):
            _read_startup_payload(BytesIO(_wav_header() + b"\x01\x02\x03"), "wav")

    def test_wav_skips_chunks_before_the_data_chunk(self) -> None:
        # FFmpeg writes a LIST/INFO chunk unless asked for a bit-exact header,
        # and an odd-sized chunk is followed by a pad byte.
        extra = b"LIST" + (5).to_bytes(4, "little") + b"INFOx" + b"\x00"
        payload = _wav_header(extra_chunks=extra) + b"\x01\x02\x03\x04"
        self.assertEqual(_read_startup_payload(BytesIO(payload), "wav"), payload)


class StartupSilenceTests(TestCase):
    def _command(self, **kwargs: object) -> list[str]:
        with patch("wambridge.stream.shutil.which", return_value="ffmpeg"):
            server = PcmAudioStreamServer(
                BytesIO(b""),
                sample_rate=48000,
                channels=2,
                **kwargs,
            )
        try:
            return server.encoder_command()
        finally:
            server.close()

    def test_default_omits_startup_silence_filter(self) -> None:
        command = self._command()

        self.assertNotIn("-af", command)
        self.assertFalse([arg for arg in command if arg.startswith("adelay=")])

    def test_zero_drops_the_filter_instead_of_passing_zero(self) -> None:
        # adelay=0 would still build a filter graph for nothing.
        command = self._command(startup_silence_ms=0)

        self.assertNotIn("-af", command)
        self.assertFalse([arg for arg in command if arg.startswith("adelay=")])

    def test_custom_value_reaches_ffmpeg(self) -> None:
        command = self._command(startup_silence_ms=250)

        self.assertIn("adelay=250:all=1", command)

    def test_out_of_range_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "between 0 and 10000"):
            self._command(startup_silence_ms=10001)
