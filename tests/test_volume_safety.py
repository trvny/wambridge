from argparse import ArgumentTypeError
from unittest import TestCase

from wambridge.cli import (
    RECOVERY_VOLUME,
    choose_start_volume,
    recovers_from_silence,
    volume_level,
)


class VolumeSafetyTests(TestCase):
    def test_muted_speaker_does_not_produce_a_silent_stream(self) -> None:
        # Startup mutes the speaker and restores it once audio flows, so a
        # helper killed in between leaves it at 0. Obeying that reading starts
        # the next stream silent while everything else reports playing.
        self.assertEqual(choose_start_volume(0, None, 10), RECOVERY_VOLUME)

    def test_explicit_zero_is_still_honoured(self) -> None:
        # Asking for silence is a choice; being left in it is not.
        self.assertEqual(choose_start_volume(5, 0, 10), 0)

    def test_recovery_never_exceeds_the_clamp(self) -> None:
        self.assertEqual(choose_start_volume(0, None, 1), 1)

    def test_callers_can_ask_whether_a_level_was_invented(self) -> None:
        # The abort path needs the same answer this function gives, and asking
        # it twice in two places is how the two drift apart.
        self.assertTrue(recovers_from_silence(0, None))
        self.assertFalse(recovers_from_silence(0, 0))
        self.assertFalse(recovers_from_silence(0, 4))
        self.assertFalse(recovers_from_silence(4, None))

    def test_clamps_current_volume_to_safe_maximum(self) -> None:
        self.assertEqual(choose_start_volume(100, None, 10), 10)

    def test_preserves_quieter_current_volume(self) -> None:
        self.assertEqual(choose_start_volume(4, None, 10), 4)

    def test_explicit_volume_overrides_clamp(self) -> None:
        self.assertEqual(choose_start_volume(4, 25, 10), 25)

    def test_parses_valid_volume(self) -> None:
        self.assertEqual(volume_level("0"), 0)
        self.assertEqual(volume_level("100"), 100)

    def test_rejects_invalid_volume(self) -> None:
        with self.assertRaises(ArgumentTypeError):
            volume_level("101")
