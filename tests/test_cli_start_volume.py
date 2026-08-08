"""Choosing the level a stream starts at.

Startup mutes the speaker and restores it once audio flows, so a helper killed
in between leaves the speaker at 0. A replacement that simply follows the
speaker's own reading would then play silently while every other signal says it
is playing.
"""

from __future__ import annotations

import unittest

from wambridge.cli import RECOVERY_VOLUME, choose_start_volume


class ChooseStartVolumeTests(unittest.TestCase):
    def test_explicit_level_wins(self) -> None:
        self.assertEqual(choose_start_volume(20, 7, 10), 7)

    def test_explicit_zero_is_still_honoured(self) -> None:
        # Asking for silence is a choice; being left in it is not.
        self.assertEqual(choose_start_volume(5, 0, 10), 0)

    def test_current_level_is_clamped(self) -> None:
        self.assertEqual(choose_start_volume(20, None, 10), 10)

    def test_current_level_below_the_clamp_is_kept(self) -> None:
        self.assertEqual(choose_start_volume(4, None, 10), 4)

    def test_muted_speaker_does_not_produce_a_silent_stream(self) -> None:
        self.assertEqual(choose_start_volume(0, None, 10), RECOVERY_VOLUME)

    def test_recovery_never_exceeds_the_clamp(self) -> None:
        self.assertEqual(choose_start_volume(0, None, 1), 1)


if __name__ == "__main__":
    unittest.main()
