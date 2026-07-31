from pathlib import Path


def replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    if old not in text:
        raise RuntimeError(f"Expected text not found in {path}: {old[:80]!r}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


cpp = Path("foobar/foo_out_wam.cpp")
replace_once(
    cpp,
    '''        if (formatChanged) {
            console::printf(
                "%s: PCM %u Hz, %u channels, %.2f s buffer",
                kComponentName,
                sampleRate,
                channels,
                static_cast<double>(capacityFrames) / sampleRate
            );
        }
''',
    '''        if (formatChanged) {
            const unsigned bufferMilliseconds = static_cast<unsigned>(
                (static_cast<uint64_t>(capacityFrames) * 1000) / sampleRate
            );
            console::printf(
                "%s: PCM %u Hz, %u channels, %u ms buffer",
                kComponentName,
                sampleRate,
                channels,
                bufferMilliseconds
            );
        }
''',
)
replace_once(
    cpp,
    '''                } else if (line.rfind("WAMBRIDGE ERROR ", 0) == 0) {
                    console::printf(
                        "%s: helper %s",
                        kComponentName,
                        line.c_str()
                    );
                    set_failure_if_current(line.substr(16), generation);
                }
''',
    '''                } else if (line.rfind("WAMBRIDGE ERROR ", 0) == 0) {
                    console::printf(
                        "%s: helper %s",
                        kComponentName,
                        line.c_str()
                    );
                    set_failure_if_current(line.substr(16), generation);
                } else if (!line.empty()) {
                    console::printf(
                        "%s: helper %s",
                        kComponentName,
                        line.c_str()
                    );
                }
''',
)
replace_once(cpp, '    "0.1.8",\n', '    "0.1.9",\n')

test = Path("tests/test_foobar_source.py")
replace_once(
    test,
    '''        self.assertIn("end of input", source)
        self.assertIn('"0.1.8"', source)
''',
    '''        self.assertIn("end of input", source)
        self.assertIn("%u ms buffer", source)
        self.assertNotIn("%.2f s buffer", source)
        self.assertIn("} else if (!line.empty()) {", source)
        self.assertIn('"0.1.9"', source)
''',
)
