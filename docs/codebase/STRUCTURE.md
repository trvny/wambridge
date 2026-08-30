# Codebase Structure

## 1) Top-Level Map
| Path | Purpose | Evidence |
|---|---|---|
| `src/wambridge/` | protocol client, discovery, CLI, streaming, persistence | `pyproject.toml`, package sources |
| `foobar/` | native foobar2000 output component and helper bootstrap | `foobar/README.md`, `foo_out_wam.vcxproj` |
| `mobile/` | Android MediaRenderer/radio adapter | `mobile/README.md`, `mobile/app/src/` |
| `tests/` | Python unit/contract/source-shape tests | `tests/test_*.py` |
| `tools/wam-probes/` | disposable physical-speaker experiments | `tools/wam-probes/README.md` |
| `docs/` | measured protocol/status/component/mobile continuity notes | `docs/README.md` |
| `.github/` | CI, releases, review automation, dependency updates | `.github/workflows/`, `.github/dependabot.yml` |

## 2) Entry Points
Primary Python entry is `wambridge.radio_cli:main`; `python -m wambridge` delegates there. Secondary console scripts are `wambridge-control`, `wambridge-events`, `wambridge-pcm`, and `wambridge-share`. foobar loads `foo_out_wam.dll`; Android starts from `MainActivity` and foreground `RendererService`/`RadioService`.

## 3) Module Boundaries
| Boundary | Owns | Must not own |
|---|---|---|
| `samsung.py` + protocol modules | WAM wire format and device semantics | UI policy |
| CLI modules | argument parsing, orchestration, terminal UX | duplicate transport implementations |
| stream modules | FFmpeg + local HTTP delivery | speaker discovery/profile storage |
| foobar adapter | foobar SDK lifecycle, PCM handoff, Windows settings | independent Samsung protocol source of truth |
| mobile adapter | Android lifecycle, UPnP surface, local proxy | importing desktop implementation |
| probes | experiments and raw evidence | supported product paths |

## 4) Naming and Organization Rules
Python uses snake_case modules/functions and PascalCase dataclasses/classes. Android uses PascalCase `.kt` files/classes. C++ groups adapter concerns by `wam_*` files and PascalCase classes. The repository is one product with several platform adapters, not a workspace-framework monorepo.

## 5) Evidence
- `README.md`
- `pyproject.toml`
- `src/wambridge/__main__.py`
- `foobar/foo_out_wam.cpp`
- `mobile/app/src/main/AndroidManifest.xml`
