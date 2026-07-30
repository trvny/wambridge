from argparse import ArgumentTypeError
from unittest import TestCase

from wambridge.cli import choose_start_volume, volume_level


class VolumeSafetyTests(TestCase):
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
