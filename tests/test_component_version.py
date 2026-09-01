"""Keep the foobar component's declared version equal to the project's.

The number lives in `version` in pyproject.toml and nowhere else. This test
exists because it had escaped to four places at once: pyproject said 0.1.0, the
Android fallback 0.1.3, the newest tag v0.1.4-alpha, and the component itself
0.1.7 - and the component was the one nobody noticed, because an audit that
greps .toml, .kts, .md and .yml does not look in .cpp.
"""

import re
from pathlib import Path
from unittest import TestCase

ROOT = Path(__file__).parents[1]
PYPROJECT = ROOT / "pyproject.toml"
COMPONENT = ROOT / "foobar" / "foo_out_wam.cpp"


def project_version() -> str:
    for line in PYPROJECT.read_text(encoding="utf-8").splitlines():
        if line.startswith("version"):
            return line.split('"')[1]
    raise AssertionError("pyproject.toml has no version line")


def declaration() -> list[str]:
    source = COMPONENT.read_text(encoding="utf-8")
    start = source.index("DECLARE_COMPONENT_VERSION(")
    block = source[start : source.index(");", start)]
    return re.findall('"([^"]*)"', block)


class ComponentVersionTests(TestCase):
    def test_component_version_matches_the_project(self) -> None:
        name, version, *about = declaration()
        self.assertEqual(name, "WAM Bridge Output")
        self.assertEqual(
            version,
            project_version(),
            "the component version drifted from pyproject.toml again",
        )

    def test_the_about_box_says_where_this_came_from(self) -> None:
        # Asked for directly: the repository is not discoverable from a DLL
        # sitting in a components folder unless the component says so.
        about = "".join(declaration()[2:])
        self.assertIn("https://github.com/twojstar/wambridge", about)
        self.assertIn("Copyright", about)
        self.assertIn("2026 trvny", about)
        self.assertIn("ISC", about)

    def test_the_source_stays_ascii(self) -> None:
        # The copyright sign is written as explicit UTF-8 bytes rather than as
        # a character, so the file does not depend on the compiler being told
        # what encoding it is in.
        raw = COMPONENT.read_bytes()
        self.assertTrue(all(byte < 128 for byte in raw))
