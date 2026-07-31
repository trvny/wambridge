from pathlib import Path


def replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    if text.count(old) != 1:
        raise SystemExit(f"Expected exactly one match in {path}: {text.count(old)}")
    path.write_text(text.replace(old, new), encoding="utf-8")


source = Path("foobar/foo_out_wam.cpp")
replace_once(
    source,
    'command += L" --sample-format f32le --format flac --startup-timeout 45";',
    'command += L" --sample-format f32le --format mp3 --startup-timeout 45";',
)
replace_once(
    source,
    '''            bool logPacing = false;
            double acceptedSeconds = 0.0;
            double writtenSeconds = 0.0;
            double queuedSeconds = 0.0;
''',
    '''            bool logPacing = false;
            unsigned acceptedMilliseconds = 0;
            unsigned writtenMilliseconds = 0;
            unsigned queuedMilliseconds = 0;
''',
)
replace_once(
    source,
    '''                        acceptedSeconds =
                            static_cast<double>(m_acceptedFrames) / sampleRate;
                        writtenSeconds =
                            static_cast<double>(m_writtenFrames) / sampleRate;
                        queuedSeconds =
                            static_cast<double>(queued_frames_locked()) /
                            sampleRate;
''',
    '''                        acceptedMilliseconds = static_cast<unsigned>(
                            m_acceptedFrames * 1000 / sampleRate
                        );
                        writtenMilliseconds = static_cast<unsigned>(
                            m_writtenFrames * 1000 / sampleRate
                        );
                        queuedMilliseconds = static_cast<unsigned>(
                            queued_frames_locked() * 1000 / sampleRate
                        );
''',
)
replace_once(
    source,
    '''                    "%s: pacing accepted=%.1fs written=%.1fs queued=%.1fs",
                    kComponentName,
                    acceptedSeconds,
                    writtenSeconds,
                    queuedSeconds
''',
    '''                    "%s: pacing accepted=%ums written=%ums queued=%ums",
                    kComponentName,
                    acceptedMilliseconds,
                    writtenMilliseconds,
                    queuedMilliseconds
''',
)
replace_once(source, '    "0.1.10",', '    "0.1.11",')

tests = Path("tests/test_foobar_source.py")
replace_once(
    tests,
    '''        self.assertIn("pacing accepted=", source)
''',
    '''        self.assertIn("pacing accepted=%ums", source)
        self.assertIn("--format mp3", source)
        self.assertNotIn("--format flac", source)
''',
)
replace_once(tests, '        self.assertIn(\'"0.1.10"\', source)', '        self.assertIn(\'"0.1.11"\', source)')
