from pathlib import Path
from unittest import TestCase


ROOT = Path(__file__).parents[1]
SOURCE = ROOT / "foobar" / "wam_preferences.cpp"
PROJECT = ROOT / "foobar" / "foo_out_wam.vcxproj"


class FoobarPreferencesSourceTests(TestCase):
    def test_preferences_page_is_built_under_output(self) -> None:
        source = SOURCE.read_text(encoding="utf-8")
        project = PROJECT.read_text(encoding="utf-8")

        self.assertIn('<ClCompile Include="wam_preferences.cpp" />', project)
        self.assertIn("class WamPreferencesPage : public preferences_page_v4", source)
        self.assertIn('const char* get_name() override { return "WAM Bridge"; }', source)
        self.assertIn(
            "GUID get_parent_guid() override { return preferences_page::guid_output; }",
            source,
        )
        self.assertIn(
            "preferences_page_factory_t<WamPreferencesPage> g_preferencesFactory;",
            source,
        )

    def test_preferences_keep_the_existing_ini_as_source_of_truth(self) -> None:
        source = SOURCE.read_text(encoding="utf-8")

        self.assertIn('L"\\\\WAMBridge\\\\foobar.ini"', source)
        self.assertIn("GetPrivateProfileStringW(", source)
        self.assertIn("WritePrivateProfileStringW(", source)
        for key in (
            "device",
            "format",
            "volume",
            "hardware_volume",
            "volume_max",
            "start_volume_max",
            "startup_silence",
            "buffer_extra",
            "sleep_after_stop",
            "diagnostics",
            "helper",
        ):
            self.assertIn(f'L"{key}"', source)

    def test_environment_overrides_are_kept_visible(self) -> None:
        source = SOURCE.read_text(encoding="utf-8")

        for name in (
            "WAMBRIDGE_PCM",
            "WAMBRIDGE_CONTROL",
            "WAMBRIDGE_DEVICE",
            "WAMBRIDGE_VOLUME",
            "WAMBRIDGE_FORMAT",
            "WAMBRIDGE_DIAGNOSTICS",
            "WAMBRIDGE_HARDWARE_VOLUME",
            "WAMBRIDGE_VOLUME_MAX",
            "WAMBRIDGE_START_VOLUME_MAX",
            "WAMBRIDGE_STARTUP_SILENCE",
            "WAMBRIDGE_BUFFER_EXTRA",
            "WAMBRIDGE_SLEEP_AFTER_STOP",
        ):
            self.assertIn(f'L"{name}"', source)
        self.assertIn("overrides are active and take precedence", source)

    def test_apply_is_marked_as_a_playback_restart_change(self) -> None:
        source = SOURCE.read_text(encoding="utf-8")

        self.assertIn("preferences_state::changed", source)
        self.assertIn("preferences_state::needs_restart_playback", source)
        self.assertIn("preferences_state::resettable", source)
        self.assertIn("populate(default_values());", source)

    def test_defaults_are_not_written_as_redundant_ini_values(self) -> None:
        source = SOURCE.read_text(encoding="utf-8")

        self.assertIn("const wchar_t* stored = value == defaultValue ? nullptr", source)
        self.assertIn('write_setting(path, L"device", values.device, L"M5")', source)
        self.assertIn('write_setting(path, L"format", values.format, L"flac")', source)
        self.assertIn("std::to_wstring(kDefaultVolumeMax)", source)
        self.assertIn("std::to_wstring(kDefaultStartupSilenceMs)", source)

    def test_startup_volume_uses_the_speakers_raw_range(self) -> None:
        source = SOURCE.read_text(encoding="utf-8")

        self.assertIn("Startup volume (raw 0-30, blank = unchanged)", source)
        self.assertIn("parsed >= 0 && parsed <= 30", source)
        self.assertIn("clamped_int(volume, 0, 0, 30)", source)

    def test_nested_controls_are_keyboard_navigable(self) -> None:
        source = SOURCE.read_text(encoding="utf-8")

        self.assertIn("WS_EX_CONTROLPARENT", source)
        self.assertIn("WS_TABSTOP", source)

    def test_new_ini_files_are_created_as_unicode(self) -> None:
        source = SOURCE.read_text(encoding="utf-8")

        self.assertIn("ensure_unicode_config_file()", source)
        self.assertIn("constexpr BYTE bom[] = {0xFF, 0xFE};", source)
        self.assertIn("CREATE_NEW", source)

    def test_save_failure_is_visible_to_the_user(self) -> None:
        source = SOURCE.read_text(encoding="utf-8")

        self.assertIn("popup_message::g_show(", source)
        self.assertIn("popup_message::icon_error", source)

    def test_layout_stays_compact(self) -> None:
        source = SOURCE.read_text(encoding="utf-8")

        self.assertIn("constexpr int kRowPitch = 27;", source)
        self.assertIn("place_row(m_helperLabel, m_helper, 10);", source)
