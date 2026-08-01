from pathlib import Path
from unittest import TestCase


SOURCE = Path(__file__).parents[1] / "foobar" / "wam_menu.cpp"


class FoobarMenuSourceTests(TestCase):
    def test_commands_live_in_wam_bridge_popup(self) -> None:
        source = SOURCE.read_text(encoding="utf-8")

        self.assertIn("mainmenu_group_popup_factory g_wamMenuGroup(", source)
        self.assertIn("mainmenu_groups::playback", source)
        self.assertIn('"WAM Bridge"', source)
        self.assertIn("return kMenuGroupGuid;", source)
        self.assertNotIn('"WAM Bridge: Emergency stop"', source)
        for label in (
            "Emergency stop",
            "Standby",
            "Volume up",
            "Volume down",
            "Volume to safe level",
        ):
            self.assertIn(f'"{label}"', source)

    def test_control_action_logs_use_narrow_strings(self) -> None:
        source = SOURCE.read_text(encoding="utf-8")

        self.assertNotIn("%ls", source)
        self.assertIn('queued %s", kComponentName, label.c_str()', source)
        self.assertIn("const auto label = action_label(action.name);", source)
