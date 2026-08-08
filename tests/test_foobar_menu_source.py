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
        self.assertIn("character >= 0 && character <= 0x7f", source)
        self.assertIn("static_cast<char>(character)", source)

    def test_slider_levels_are_coalesced_not_queued(self) -> None:
        # A drag produces one request per pixel and each queued one would
        # spawn its own control-helper process against the shared 55001 port.
        source = SOURCE.read_text(encoding="utf-8")

        self.assertIn("void request_volume(int step)", source)
        self.assertIn("m_pendingVolume = step;", source)
        # Replaced, never appended: only the newest level matters.
        self.assertNotIn("m_pendingVolume.push", source)
        self.assertIn("kVolumeSendInterval", source)
        self.assertIn("m_cv.wait_until(lock, ready);", source)
        # A level equal to the last one sent must not spend the control port.
        self.assertIn("if (step == m_lastSentVolume) continue;", source)

    def test_menu_actions_outrank_slider_levels(self) -> None:
        # Emergency stop must not wait behind a drag, and a stale level is
        # worth dropping rather than delivering late.
        source = SOURCE.read_text(encoding="utf-8")

        queue_branch = source.index("out = std::move(m_queue.front());")
        volume_branch = source.index("out = ControlAction{L\"set-volume\", step};")
        self.assertLess(queue_branch, volume_branch)

    def test_volume_requests_are_clamped_to_the_measured_range(self) -> None:
        source = SOURCE.read_text(encoding="utf-8")

        self.assertIn("namespace wam {", source)
        self.assertIn("void request_volume_step(int step)", source)
        self.assertIn("std::max(0, std::min(kMaximumRawVolume, step))", source)

    def test_set_volume_passes_the_level_to_the_helper(self) -> None:
        source = SOURCE.read_text(encoding="utf-8")

        self.assertIn('command += L" --level ";', source)
        self.assertIn("command += std::to_wstring(*action.level);", source)
