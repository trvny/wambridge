from pathlib import Path
from unittest import TestCase


ROOT = Path(__file__).parents[1]
PREFERENCES = ROOT / "foobar" / "wam_preferences.cpp"
SETTINGS = ROOT / "foobar" / "wam_settings.cpp"
SETTINGS_HEADER = ROOT / "foobar" / "wam_settings.h"
OUTPUT = ROOT / "foobar" / "foo_out_wam.cpp"
PROJECT = ROOT / "foobar" / "foo_out_wam.vcxproj"


class FoobarPreferencesSourceTests(TestCase):
    def test_preferences_page_is_built_under_output(self) -> None:
        source = PREFERENCES.read_text(encoding="utf-8")
        project = PROJECT.read_text(encoding="utf-8")

        self.assertIn('<ClCompile Include="wam_preferences.cpp" />', project)
        self.assertIn('<ClCompile Include="wam_settings.cpp" />', project)
        self.assertIn("class WamPreferencesPage : public preferences_page_v4", source)
        self.assertIn('const char* get_name() override { return "WAM Bridge"; }', source)
        self.assertIn(
            "GUID get_parent_guid() override { return preferences_page::guid_output; }",
            source,
        )

    def test_settings_parser_is_shared_by_runtime_and_preferences(self) -> None:
        prefs = PREFERENCES.read_text(encoding="utf-8")
        output = OUTPUT.read_text(encoding="utf-8")
        settings = SETTINGS.read_text(encoding="utf-8")

        self.assertIn('#include "wam_settings.h"', prefs)
        self.assertIn('#include "wam_settings.h"', output)
        self.assertIn("wam_settings::load_ini_values()", prefs)
        self.assertIn("wam_settings::load_effective_values()", output)
        self.assertIn("IniLoadResult load_ini_values()", settings)
        self.assertIn("Values load_effective_values()", settings)
        self.assertNotIn("int valid_int(", prefs)
        self.assertNotIn("std::wstring ini_value(", output)

    def test_preferences_keep_existing_ini_as_source_of_truth(self) -> None:
        settings = SETTINGS.read_text(encoding="utf-8")

        self.assertIn("WAMBridge", settings)
        self.assertIn("GetPrivateProfileStringW(", settings)
        self.assertIn("WritePrivateProfileStringW(", settings)
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
            self.assertIn(f'L"{key}"', settings)

    def test_environment_overrides_are_kept_visible(self) -> None:
        prefs = PREFERENCES.read_text(encoding="utf-8")
        settings = SETTINGS.read_text(encoding="utf-8")

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
            self.assertIn(f'L"{name}"', settings)
        self.assertIn("overrides are active and take precedence", prefs)

    def test_apply_is_marked_as_a_playback_restart_change(self) -> None:
        source = PREFERENCES.read_text(encoding="utf-8")

        self.assertIn("preferences_state::changed", source)
        self.assertIn("preferences_state::needs_restart_playback", source)
        self.assertIn("preferences_state::resettable", source)
        self.assertIn("populate(default_values());", source)

    def test_dark_mode_uses_foobar_core_hooks(self) -> None:
        source = PREFERENCES.read_text(encoding="utf-8")

        self.assertIn("<foobar2000/SDK/coreDarkMode.h>", source)
        self.assertIn("fb2k::CCoreDarkModeHooks", source)
        self.assertIn("AddDialogWithControls(m_window)", source)
        self.assertIn("preferences_state::dark_mode_supported", source)

    def test_invalid_ini_values_enable_apply_for_normalization(self) -> None:
        prefs = PREFERENCES.read_text(encoding="utf-8")
        settings = SETTINGS.read_text(encoding="utf-8")

        self.assertIn("needsNormalization", settings)
        self.assertIn("m_needsNormalization", prefs)
        self.assertIn("m_needsNormalization ||", prefs)
        self.assertIn("load_values(false, false, &result.needsNormalization)", settings)

    def test_startup_volume_uses_the_speakers_raw_range_everywhere(self) -> None:
        prefs = PREFERENCES.read_text(encoding="utf-8")
        settings = SETTINGS.read_text(encoding="utf-8")
        output = OUTPUT.read_text(encoding="utf-8")

        self.assertIn("Startup volume (raw 0-30, blank = unchanged)", prefs)
        self.assertIn("kMaximumRawVolume = 30", SETTINGS_HEADER.read_text(encoding="utf-8"))
        self.assertIn("parse_int(value, 0, kMaximumRawVolume", settings)
        self.assertNotIn("parsed <= 100", output)
        self.assertNotIn("0..100", output)

    def test_defaults_are_not_written_as_redundant_ini_values(self) -> None:
        settings = SETTINGS.read_text(encoding="utf-8")

        self.assertIn("const wchar_t* stored = value == defaultValue ? nullptr", settings)
        self.assertIn('write_setting(path, L"device", values.device, L"M5")', settings)
        self.assertIn("std::to_wstring(kDefaultVolumeMax)", settings)
        self.assertIn("std::to_wstring(kDefaultStartupSilenceMs)", settings)

    def test_nested_controls_are_keyboard_navigable(self) -> None:
        source = PREFERENCES.read_text(encoding="utf-8")

        self.assertIn("WS_EX_CONTROLPARENT", source)
        self.assertIn("WS_TABSTOP", source)

    def test_new_ini_files_are_created_as_unicode(self) -> None:
        settings = SETTINGS.read_text(encoding="utf-8")

        self.assertIn("ensure_unicode_config_file()", settings)
        self.assertIn("constexpr BYTE bom[] = {0xFF, 0xFE};", settings)
        self.assertIn("CREATE_NEW", settings)

    def test_save_failure_is_visible_to_the_user(self) -> None:
        source = PREFERENCES.read_text(encoding="utf-8")

        self.assertIn("popup_message::g_show(", source)
        self.assertIn("popup_message::icon_error", source)

    def test_layout_stays_compact(self) -> None:
        source = PREFERENCES.read_text(encoding="utf-8")

        self.assertIn("constexpr int kRowPitch = 27;", source)
        self.assertIn("place_row(m_helperLabel, m_helper, 10);", source)
