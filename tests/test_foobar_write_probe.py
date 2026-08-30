from pathlib import Path
from unittest import TestCase

ROOT = Path(__file__).parents[1]
PROJECT = ROOT / "foobar" / "foo_out_wam.vcxproj"
PROBE = ROOT / "foobar" / "wam_write_probe.h"


class FoobarWriteProbeTests(TestCase):
    def test_probe_is_forced_only_into_output_source(self) -> None:
        project = PROJECT.read_text(encoding="utf-8")

        self.assertIn('<ClCompile Include="foo_out_wam.cpp">', project)
        self.assertIn(
            "<ForcedIncludeFiles>wam_write_probe.h;%(ForcedIncludeFiles)",
            project,
        )
        self.assertIn('<ClCompile Include="wam_menu.cpp" />', project)

    def test_probe_logs_only_the_first_few_pcm_writes(self) -> None:
        probe = PROBE.read_text(encoding="utf-8")

        self.assertIn("const unsigned call = calls.fetch_add(1);", probe)
        self.assertIn("if (call < 8)", probe)
        self.assertIn("PCM WriteFile #%u", probe)
        self.assertIn("#define WriteFile wambridge_probe_write_file", probe)

    def test_probe_avoids_length_modifiers_pfc_cannot_print(self) -> None:
        # console::printf is pfc's formatter. It printed "%lu" as a literal
        # "lu" and swallowed the number, so the probe logged no measurements.
        probe = PROBE.read_text(encoding="utf-8")
        format_line = "".join(
            line for line in probe.splitlines() if "PCM WriteFile" in line
        )

        self.assertNotIn("%lu", format_line)
        self.assertNotIn("%llu", format_line)
        self.assertIn("elapsedMs=%u", probe)
