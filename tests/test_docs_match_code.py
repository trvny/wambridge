"""Pin the documentation to the code where the claim is mechanically checkable.

Written 2026-08-19 after an audit found three claims that had been false for a
while and one that had been repeated into a second document. Every case below
is a real one that rotted, not a hypothetical: the pattern is a document saying
a thing is missing long after it landed, which is worse than no document,
because the open list is where the next piece of work gets chosen.

This can only ever catch the checkable kind. A claim about behaviour on the
speaker still needs the speaker.
"""

import re
from pathlib import Path
from unittest import TestCase

ROOT = Path(__file__).parents[1]
README = ROOT / "README.md"
STATUS = ROOT / "docs" / "DEVELOPMENT_STATUS.md"
PREFERENCES = ROOT / "foobar" / "wam_preferences.cpp"
MENU = ROOT / "foobar" / "wam_menu.cpp"
TUNEIN = ROOT / "src" / "wambridge" / "tunein.py"
CLI = ROOT / "src" / "wambridge" / "cli.py"
SAMSUNG = ROOT / "src" / "wambridge" / "samsung.py"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class DocsMatchCodeTests(TestCase):
    def test_preferences_page_is_not_described_as_missing(self) -> None:
        # Claimed absent in both README and open item 11 long after it landed.
        self.assertIn("preferences_page_instance", read(PREFERENCES))
        self.assertNotIn("no preferences page yet", read(README))
        self.assertNotRegex(
            read(STATUS),
            r"(?m)^11\. Add a proper foobar preferences page",
            "open item 11 is done; it must stay struck",
        )

    def test_listing_presets_is_not_described_as_missing(self) -> None:
        # Open item 12 said nothing lists what the speaker holds while
        # `wambridge --tunein-list` had been doing exactly that.
        tunein = read(TUNEIN)
        self.assertIn("def get_tunein_presets(", tunein)
        self.assertIn("GetPresetList", tunein)
        self.assertNotIn("nothing lists what the speaker has", read(STATUS))

    def test_the_menu_actions_the_status_file_advertises_exist(self) -> None:
        menu = read(MENU)
        for label in ("Emergency stop", "Standby", "Volume up", "Volume down"):
            self.assertIn(f'"{label}"', menu)

    def test_struck_items_are_not_quietly_reopened(self) -> None:
        # Items are struck with ~~...~~ rather than deleted, so that the
        # history of what was believed stays readable. Re-adding one as an
        # open item is how the rot started.
        status = read(STATUS)
        for number in (1, 5, 11, 13):
            match = re.search(rf"(?m)^{number}\. (.*)$", status)
            self.assertIsNotNone(match, f"open item {number} vanished entirely")
            assert match is not None
            self.assertTrue(
                match.group(1).lstrip().startswith("~~"),
                f"open item {number} was struck and is open again",
            )

    def test_the_url_path_does_not_gate_on_the_cp_submode(self) -> None:
        # `cp` is the submode SetUrlPlayback runs in - measured twice, most
        # recently 2026-08-19 with an internet stream audible for its whole run
        # while GetFunc reported `cp`. A gate here refuses the start that
        # follows every previous URL playback, and DEVELOPMENT_STATUS has said
        # so since before the gate existed. It was added anyway once.
        self.assertIn(
            "no URL startup gate or power-cycle advice",
            read(STATUS),
            "the rule this test enforces was removed from the status file",
        )
        self.assertNotIn("require_local_playback_mode", read(CLI))

    def test_the_cp_guard_does_not_advertise_a_power_cycle(self) -> None:
        # Recovery is SetFunc to another source and back to wifi. The guard
        # told users to unplug the speaker long after that stopped being true.
        samsung = read(SAMSUNG)
        self.assertIn("def require_local_playback_mode(", samsung)
        self.assertNotIn("power-cycle the speaker and retry", samsung)
