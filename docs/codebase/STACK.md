# Technology Stack

## 1) Runtime Summary
| Area | Value | Evidence |
|---|---|---|
| Core | Python >=3.13, stdlib-only production package | `pyproject.toml`, `src/wambridge/` |
| Desktop adapter | C++17 x64 Windows DLL for foobar2000 | `foobar/foo_out_wam.vcxproj` |
| Mobile adapter | Android app, Kotlin sources, AGP 9.3.2, Java 17 bytecode target | `mobile/build.gradle.kts`, `mobile/app/build.gradle.kts` |
| Packaging | setuptools + uv; PyInstaller for Windows helpers | `pyproject.toml`, `.github/workflows/build.yml` |

## 2) Production Frameworks and Dependencies
The Python package declares no third-party runtime dependencies. FFmpeg is an external executable used for transcoding/encoding. The foobar component depends on the foobar2000 SDK plus Win32/Winsock. Android declares no third-party runtime libraries in Gradle and uses Android platform APIs.

## 3) Development Toolchain
| Tool | Purpose | Evidence |
|---|---|---|
| uv | locked Python environment | `uv.lock`, `.github/workflows/build.yml` |
| Ruff | Python lint/format policy | `pyproject.toml` |
| unittest | Python tests | `tests/`, `.github/workflows/build.yml` |
| MSBuild/MSVC v142 | C++ component build | `foobar/foo_out_wam.vcxproj` |
| Gradle/AGP + JUnit 4 | Android build, lint and JVM tests | `mobile/`, `.github/workflows/mobile.yml` |

## 4) Key Commands
`uv sync --locked` · `uv run python -m unittest discover -s tests -v` · `uv run ruff check src tests foobar/wambridge_pcm_entry.py tools` · `cd mobile && ./gradlew :app:lintDebug :app:testDebugUnitTest :app:assembleDebug`

## 5) Environment and Config
Python user state defaults to `%LOCALAPPDATA%/WAMBridge` or XDG config and can be overridden with `WAMBRIDGE_CONFIG`, `WAMBRIDGE_STATIONS_CONFIG`, and `WAMBRIDGE_IDENTITY`. The foobar adapter accepts `WAMBRIDGE_*` overrides documented by `foobar/wam_settings.cpp`; Android release signing uses `WAMBRIDGE_KEYSTORE_*`/key variables in CI. Runtime constraints are same-LAN access to the speaker and FFmpeg for streamed audio.

## 6) Evidence
- `pyproject.toml`
- `foobar/foo_out_wam.vcxproj`
- `mobile/app/build.gradle.kts`
- `.github/workflows/build.yml`
- `.github/workflows/mobile.yml`
