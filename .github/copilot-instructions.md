# Copilot instructions for trvny/wambridge

Trust these instructions first. Only search the repo when something here is incomplete or
found to be wrong. Also read `AGENTS.md` (traps from physical-speaker testing) and, for
playback or protocol work, `docs/WAM_PROTOCOL.md` and `docs/DEVELOPMENT_STATUS.md`.

## What this repository is

Windows-first bridge for streaming audio over Wi-Fi to Samsung Wireless Audio Multiroom
(WAM) speakers, developed against one physical Shape M5 (`SPK-WAM550`). Three components:

1. **Python CLI package** (`src/wambridge/`, `pyproject.toml`, `uv.lock`): SSDP discovery,
   saved device profiles, streaming local files/URLs via a tokenized local HTTP server +
   FFmpeg transcoding + Samsung's `SetUrlPlayback` API (speaker TCP port `55001`), remote
   control, radio stations, TuneIn presets. Console scripts: `wambridge` (main, maps to
   `wambridge.radio_cli:main`), `wambridge-control`, `wambridge-events`, `wambridge-pcm`,
   `wambridge-share`. Zero runtime dependencies; stdlib only. Python >= 3.14.
2. **foobar2000 output component** (`foobar/`): C++17 DLL (`foo_out_wam`) for foobar2000
   2.x x64, built with MSBuild against the foobar2000 SDK. Pipes decoded `f32le` PCM into
   the bundled `wambridge-pcm` helper (PyInstaller onedir build of
   `foobar/wambridge_pcm_entry.py`).
3. **Android adapter** (`mobile/`): Kotlin app, Gradle 9.5.0, AGP 9.3.1, Java 17,
   compileSdk 37, targetSdk 36, minSdk 26, Gradle wrapper 9.5.0 checked in. UPnP MediaRenderer
   facade proxying to the M5. Sources in
   `mobile/app/src/main/java/io/github/trvny/wambridge/mobile/`.

`tools/` holds PyInstaller entry points (`wambridge_control_entry.py`,
`wambridge_events_entry.py`) and `tools/wam-probes/` — one-shot hardware diagnostic
scripts, NOT covered by tests and not user tools. `docs/` holds living lab notes.

## Build, test, lint — exact commands

Environment: Windows/PowerShell is the primary target; Python CI runs on `windows-2022`
with Python 3.14 via uv. Use `uv` (there is a `uv.lock`; dev group provides `ruff` and
`pyinstaller`). Always run `uv sync --locked` before anything else.

```powershell
uv sync --locked                                          # bootstrap, always first
uv run ruff check src tests foobar/wambridge_pcm_entry.py tools
uv run python -m compileall -q src tests foobar/wambridge_pcm_entry.py tools
uv run python -m unittest discover -s tests -v            # the full test suite
uv run wambridge-control --help                           # smoke checks used by CI
uv run wambridge-events --help
```

Ruff config is in `pyproject.toml`: line-length 100, target py314, lint select
`E,F,I,UP,B,SIM` — CI runs bare `ruff check`, so it enforces the full configured set.
Tests are stdlib `unittest`, plain function/`TestCase` style, all
offline — no speaker or network needed. There is no pytest config; use the unittest
command above, not `pytest`.

PyInstaller helper builds (CI does these; locally optional, slow):

```powershell
uv run python -m PyInstaller --noconfirm --clean --onedir --name wambridge-pcm `
  --distpath dist/helper --workpath build/pyinstaller --specpath build/pyinstaller `
  foobar/wambridge_pcm_entry.py
uv run python -m PyInstaller --noconfirm --clean --onefile --name wambridge-control `
  --distpath dist/control --workpath build/pyinstaller-control `
  --specpath build/pyinstaller-control tools/wambridge_control_entry.py
uv run python -m PyInstaller --noconfirm --clean --onefile --name wambridge-events `
  --distpath dist/events --workpath build/pyinstaller-events `
  --specpath build/pyinstaller-events tools/wambridge_events_entry.py
```

C++ component (Windows only, needs the foobar2000 SDK extracted and MSBuild with the
v142 toolset; CI pins `VCToolsVersion=14.29.30133`):

```powershell
7z x SDK-2025-03-07.7z -oexternal/foobar-sdk -y   # from https://www.foobar2000.org/downloads/SDK-2025-03-07.7z
& $msbuild foobar/foo_out_wam.vcxproj /t:Rebuild /m /p:Configuration=Release `
  /p:Platform=x64 /p:VCToolsVersion=14.29.30133 "/p:FoobarSdkRoot=<sdk root>"
```

The project treats **warnings as errors** (`/WX`, WarningLevel 4). C++ sources must stay
ASCII (a test enforces this for `foo_out_wam.cpp`). `console::printf` is pfc's formatter:
use `%u`/`%s`, never `%lu`/`%llu`.

Android (Linux/macOS/Windows; use the checked-in Gradle 9.5.0 wrapper with JDK 17):

```bash
cd mobile && ./gradlew :app:lintDebug :app:testDebugUnitTest :app:assembleDebug
```

## CI workflows (replicate before opening a PR)

- `.github/workflows/build.yml` — runs on PRs touching `foobar/**`, `pyproject.toml`,
  `uv.lock`, `src/**`, `tests/**`, `tools/**`. Sequence: `uv sync --locked` → ruff +
  compileall → unittest + two `--help` smoke checks → three PyInstaller builds (each
  `--help`-smoke-tested) → SDK download + MSBuild → packaging. Docs-only changes do not
  trigger it.
- `.github/workflows/mobile.yml` — runs on PRs touching `mobile/**`:
  `./gradlew :app:lintDebug :app:testDebugUnitTest :app:assembleDebug` with Java 17 and the checked-in Gradle 9.5.0 wrapper.
- `.github/workflows/release.yml` — push to main rebuilds the rolling `alpha` prerelease;
  do not edit release mechanics.

## Layout and cross-cutting rules that tests enforce

- **Single source of version truth: `version` in `pyproject.toml`.**
  `tests/test_component_version.py` requires the `DECLARE_COMPONENT_VERSION` block in
  `foobar/foo_out_wam.cpp` to equal it. Keep the fallback `wamVersionName` in
  `mobile/app/build.gradle.kts` equal too. Note `src/wambridge/__init__.py.__version__`
  currently lags behind on purpose/legacy — do not bump versions unless asked.
- `tests/test_docs_match_code.py` pins README/`docs/DEVELOPMENT_STATUS.md` claims to the
  code: struck (`~~...~~`) open items must stay struck, the URL path must not gate on the
  `cp` submode (`require_local_playback_mode` must not appear in `src/wambridge/cli.py`),
  and menu labels like "Emergency stop"/"Standby" must exist in `foobar/wam_menu.cpp`.
  If you change behavior described in docs, update the docs in the same change.
- Test style: stdlib `unittest`, one file per module (`tests/test_<module>.py`).
  Model `Popen().stdout` with a real `BytesIO`, not a bare `MagicMock`.
- Source map: `src/wambridge/cli.py` (main streaming session flow), `samsung.py` (WAM API
  client), `stream.py` (HTTP server + FFmpeg, `OUTPUT_PROFILES`: flac/mp3/wav),
  `discovery.py` (SSDP + port-55001 fallback scan), `profiles.py`/`alias_store.py` (saved
  devices), `stations.py`/`station_packs.py`/`tunein.py` (radio), `pcm_stream.py` +
  `pcm_cli.py` (foobar helper path), `control_cli.py`/`control_channel.py`,
  `event_cli.py`/`wam_events.py`, `share.py`/`share_cli.py` (experimental DLNA share).
- `connections.py`, `identity.py`, `cli_common.py` are shared plumbing.

## Hardware traps (from AGENTS.md — do not "fix" these by reasoning alone)

- Unimplemented speaker commands (`GetPowerStatus`, `GetLedStatus`, `GetStandbyMode`,
  `GetFeature`, `GetPowerSaving`, `GetAutoPowerDown`, `GetSpkStatus`) are answered with
  silence, i.e. a full timeout each. Never use them to decide liveness; use `GetSpkName`.
- Raw M5 volume steps are `0..30` (`3` ≈ 10%); values above 30 clamp silently.
- Active PCM playback has exactly one owner of TCP `55001` and one FFmpeg on PCM stdin;
  extra listeners/encoders break the stream.
- Always terminate timed-out child processes (runaway FFmpeg has exhausted the test
  machine's RAM).
- `process_samples` in the C++ output must accept every frame offered — it returns void
  and dropped remainders cannot be reported.
- Reporting output failure throws `exception_output_invalidated` and foobar builds a fresh
  output object per attempt, so retry budgets must live at file scope, not in the object.

## Practical notes

- User config lives under `%LOCALAPPDATA%\WAMBridge\` (`devices.json`, `stations.json`,
  `foobar.ini`) — tests never touch it.
- FFmpeg must be on `PATH` for real streaming, but the test suite does not need it.
- Never commit probe scratch output (`_scratch/` is git-ignored; it can contain speaker
  serials).
- README ends with auto-generated "Mininewsy"/quote sections between marker comments —
  leave those blocks alone.
- Code-review norm: no review comments on docs-only/cosmetic changes unless they break
  generated/validated content.
