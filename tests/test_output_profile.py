"""The FLAC profile must follow the source instead of imposing 48000/s16.

FLAC is lossless, so a fixed rate resampled every 44.1 kHz track for nothing and
a fixed sample format discarded anything above CD depth. A physical M5 plays
FLAC up to 96 kHz / 24-bit.
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


class Mp3ProfileTests(unittest.TestCase):
    def test_lossy_profile_keeps_its_fixed_rate(self) -> None:
        # MP3 is lossy and already resamples; nothing is gained by following
        # the source, so its behaviour is deliberately unchanged.
        profile = OUTPUT_PROFILES["mp3"]
        self.assertIn("-ar", profile.ffmpeg_args)
        self.assertEqual(profile.args_for(96000), profile.ffmpeg_args)


if __name__ == "__main__":
    unittest.main()
