"""What each speaker-facing output profile is for.

FLAC must follow the source instead of imposing 48000/s16: it is lossless, so a
fixed rate resampled every 44.1 kHz track for nothing and a fixed sample format
discarded anything above CD depth. A physical M5 plays FLAC up to 96 kHz /
24-bit. WAV exists for the opposite reason - it is deliberately fatter, to test
whether the speaker's partly byte-bounded prebuffer then holds fewer seconds.
"""

from __future__ import annotations

import unittest

from wambridge.stream import OUTPUT_PROFILES


class FlacProfileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.profile = OUTPUT_PROFILES["flac"]

    def test_no_fixed_rate_or_sample_format(self) -> None:
        self.assertNotIn("-ar", self.profile.ffmpeg_args)
        self.assertNotIn("-sample_fmt", self.profile.ffmpeg_args)

    def test_cd_rate_is_passed_through_untouched(self) -> None:
        self.assertNotIn("-ar", self.profile.args_for(44100))

    def test_high_resolution_is_passed_through_untouched(self) -> None:
        args = self.profile.args_for(96000)
        self.assertNotIn("-ar", args)

    def test_above_confirmed_maximum_is_resampled(self) -> None:
        args = self.profile.args_for(192000)
        self.assertEqual(args[:2], ("-ar", "96000"))

    def test_unknown_rate_is_left_alone(self) -> None:
        # URL sources are probed by FFmpeg, so the rate is not known upfront.
        self.assertEqual(self.profile.args_for(None), self.profile.ffmpeg_args)

    def test_still_encodes_flac(self) -> None:
        args = self.profile.args_for(44100)
        self.assertEqual(args[args.index("-c:a") + 1], "flac")


class WavProfileTests(unittest.TestCase):
    """The WAV profile exists to be fatter than FLAC, not better sounding."""

    def setUp(self) -> None:
        self.profile = OUTPUT_PROFILES["wav"]

    def test_serves_uncompressed_pcm(self) -> None:
        args = self.profile.args_for(44100)
        self.assertEqual(args[args.index("-c:a") + 1], "pcm_s16le")
        self.assertEqual(args[args.index("-f") + 1], "wav")

    def test_header_stays_bit_exact(self) -> None:
        # FFmpeg's LIST/INFO chunk carries the encoder version and would move
        # the audio start around between FFmpeg builds for no gain.
        self.assertEqual(
            self.profile.ffmpeg_args[self.profile.ffmpeg_args.index("-fflags") + 1],
            "+bitexact",
        )

    def test_rate_is_fixed_at_the_only_confirmed_one(self) -> None:
        # Only 44.1/16 WAV was confirmed on a physical M5. A cap expressed as a
        # maximum would not survive the file and URL paths, which do not know
        # the source rate and call args_for(None).
        for rate in (None, 44100, 48000, 96000):
            with self.subTest(rate=rate):
                args = self.profile.args_for(rate)
                self.assertEqual(args[args.index("-ar") + 1], "44100")
                self.assertEqual(args.count("-ar"), 1)

    def test_served_as_audio_wav(self) -> None:
        self.assertEqual(self.profile.extension, "wav")
        self.assertEqual(self.profile.content_type, "audio/wav")


class Wav24ProfileTests(unittest.TestCase):
    """The same lever as wav, pulled harder, and untried on hardware."""

    def setUp(self) -> None:
        self.profile = OUTPUT_PROFILES["wav24"]

    def test_serves_24_bit_pcm_at_the_confirmed_rate(self) -> None:
        args = self.profile.args_for(None)
        self.assertEqual(args[args.index("-c:a") + 1], "pcm_s24le")
        self.assertEqual(args[args.index("-ar") + 1], "44100")

    def test_is_fatter_than_the_16_bit_profile(self) -> None:
        # 44100 * 3 * 2 against 44100 * 2 * 2. The whole point is the byte rate.
        self.assertEqual(44100 * 3 * 2 * 8 // 1000, 2116)
        self.assertEqual(OUTPUT_PROFILES["wav"].ffmpeg_args.count("pcm_s24le"), 0)

    def test_shares_the_wav_container(self) -> None:
        self.assertEqual(self.profile.extension, "wav")
        self.assertEqual(self.profile.content_type, "audio/wav")

    def test_rate_is_fixed_like_the_16_bit_profile(self) -> None:
        for rate in (None, 44100, 96000):
            with self.subTest(rate=rate):
                self.assertEqual(self.profile.args_for(rate).count("-ar"), 1)


class Mp3ProfileTests(unittest.TestCase):
    def test_lossy_profile_keeps_its_fixed_rate(self) -> None:
        # MP3 is lossy and already resamples; nothing is gained by following
        # the source, so its behaviour is deliberately unchanged.
        profile = OUTPUT_PROFILES["mp3"]
        self.assertIn("-ar", profile.ffmpeg_args)
        self.assertEqual(profile.args_for(96000), profile.ffmpeg_args)


if __name__ == "__main__":
    unittest.main()
