# Coding Conventions

## 1) Naming Rules

| Item | Rule | Example | Evidence |
|---|---|---|---|
| Python files/functions | snake_case | `resolve_tunein_station` | `src/wambridge/tunein.py` |
| Python types | PascalCase, immutable dataclasses where practical | `WamPlaybackStatus` | `samsung.py` |
| Android types/files | PascalCase Kotlin types; descriptive service/activity names | `RendererService` | `mobile/app/src/main/java/...` |
| C++ types | PascalCase classes, `m_` members, `g_` file/global state, `k` constants | `WamOutput`, `m_settings`, `kComponentName` | `foobar/*.cpp` |
| Environment | uppercase `WAMBRIDGE_*` | `WAMBRIDGE_FORMAT` | `foobar/wam_settings.cpp` |

## 2) Formatting and Linting
Python policy is Ruff, line length 100, target `py313`, configured rules `E,F,I,UP,B,SIM`. CI currently invokes Ruff with only `E,F,B,UP`, so `I` and `SIM` are configured locally but not enforced by the main build job. C++ builds at warning level 4 with warnings as errors. Android runs Android lint in CI.

## 3) Import and Module Conventions
Python imports are stdlib first and then relative package imports; `from __future__ import annotations` is common. There are no package-wide barrel exports. Platform adapters communicate through processes/network protocols rather than importing across language boundaries.

## 4) Error and Logging Conventions
Protocol/runtime failures use domain exceptions such as `WamApiError`, `StreamError`, `ProfileError`, and `StationError`; CLI boundaries log a concise error and return status 1. Logging uses stdlib `logging` with `LEVEL: message`; verbose mode enables DEBUG. Comments frequently record physical measurements and explicitly distinguish measured behavior from hypotheses. Sensitive TuneIn `stationurl` data is treated as credential-like and scratch captures are gitignored.

## 5) Testing Conventions
Python tests live under `tests/test_*.py` and use `unittest`/`unittest.mock`. Android JVM tests live under `mobile/app/src/test/...` and use JUnit 4. Several Python tests intentionally assert C++ source shape or documentation/code consistency, converting past regressions into executable constraints.

## 6) Evidence
- `pyproject.toml`
- `.github/workflows/build.yml`
- `foobar/foo_out_wam.vcxproj`
- `tests/test_foobar_source.py`
- `tests/test_docs_match_code.py`
