from pathlib import Path


def replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    if text.count(old) != 1:
        raise SystemExit(f"Expected exactly one match in {path}: {text.count(old)}")
    path.write_text(text.replace(old, new), encoding="utf-8")


replace_once(
    Path("src/wambridge/pcm_cli.py"),
    '''        volume_changed = True
        set_volume(speaker_ip, start_volume, port=speaker_port)
        _raise_if_pcm_input_closed(input_stream)
        print("WAMBRIDGE READY", file=output_stream, flush=True)
''',
    '''        print("WAMBRIDGE READY", file=output_stream, flush=True)
''',
)

replace_once(
    Path("tests/test_pcm_cli.py"),
    '''            [
                call("10.0.0.118", 0, port=55001),
                call("10.0.0.118", 4, port=55001),
                call("10.0.0.118", 4, port=55001),
            ],
''',
    '''            [
                call("10.0.0.118", 0, port=55001),
                call("10.0.0.118", 4, port=55001),
            ],
''',
)

replace_once(
    Path("tests/test_pcm_cli.py"),
    '''    def test_restores_muted_volume_when_startup_ends_after_ready(
''',
    '''    def test_keeps_muted_volume_when_startup_ends_after_ready(
''',
)

replace_once(
    Path("tests/test_pcm_cli.py"),
    '''        self.assertEqual(
            volume_mock.call_args_list,
            [
                call("10.0.0.118", 4, port=55001),
                call("10.0.0.118", 0, port=55001, timeout=1.0),
            ],
        )
''',
    '''        self.assertEqual(volume_mock.call_args_list, [])
''',
)

replace_once(
    Path("foobar/foo_out_wam.cpp"),
    '''    "0.1.9",
''',
    '''    "0.1.10",
''',
)

replace_once(
    Path("tests/test_foobar_source.py"),
    '''        self.assertIn('"0.1.9"', source)
''',
    '''        self.assertIn('"0.1.10"', source)
''',
)
