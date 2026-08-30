from pathlib import Path
from unittest import TestCase


SOURCE = Path(__file__).parents[1] / "foobar" / "wam_menu.cpp"
PCM_SOURCE = Path(__file__).parents[1] / "src" / "wambridge" / "pcm_cli.py"


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
            "Stop & mute",
            "Start sleep timer",
            "Cancel sleep timer",
            "Volume up",
            "Volume down",
            "Volume to safe level",
        ):
            self.assertIn(f'"{label}"', source)

    def test_sleep_timer_reuses_config_and_routes_through_active_helper(self) -> None:
        source = SOURCE.read_text(encoding="utf-8")
        pcm_source = PCM_SOURCE.read_text(encoding="utf-8")

        self.assertIn('L"WAMBRIDGE_SLEEP_AFTER_STOP"', source)
        self.assertIn('L"sleep_after_stop"', source)
        self.assertIn('L" --seconds "', source)
        self.assertIn("wam::send_sleep_timer_over_helper", source)
        self.assertIn("wam::note_menu_sleep_timer", source)
        self.assertIn('L"menu_sleep_timer_deadline"', source)
        self.assertIn("set_sleep_timer=watcher.set_sleep_timer", pcm_source)

    def test_sleep_timer_stops_foobar_before_speaker_deadline(self) -> None:
        source = SOURCE.read_text(encoding="utf-8")

        self.assertIn("kSleepTimerStopLeadSeconds = 2", source)
        self.assertIn("class SleepTimerStopCallback", source)
        self.assertIn("if (control->is_playing()) control->stop();", source)
        self.assertIn("sleep_timer_coordinator().arm(*deadline);", source)
        self.assertIn("m_generation.fetch_add(1);", source)
        self.assertIn("if (m_generation->load() != m_token) return;", source)
        self.assertIn("SleepTimerStopCallback>(&m_generation, token)", source)

    def test_sleep_timer_coordinator_survives_persistence_failure(self) -> None:
        source = SOURCE.read_text(encoding="utf-8")
        start = source.index("void note_menu_sleep_timer(int seconds)")
        end = source.index("bool menu_sleep_timer_active()", start)
        body = source[start:end]
        failure = body.index("if (!write_menu_sleep_deadline(seconds))")
        arm = body.index("sleep_timer_coordinator().arm", failure)

        self.assertNotIn("return;", body[failure:arm])

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
        volume_branch = source.index("out = ControlAction{L\"set-volume\", step, std::nullopt};")
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
