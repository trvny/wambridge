# Codebase Concerns

## 1) Top Risks (Prioritized)
| Severity | Concern | Evidence | Impact | Suggested action |
|---|---|---|---|---|
| high | Dead `SetUrlPlayback` target can wedge recovery commands until power-cycle | `samsung.py`, `SamsungWamChannel.kt` | user-visible hard failure | preserve reachability guard on every URL-offer path |
| high | Physical firmware is stateful and timing-sensitive; extra 55001 clients/CPM bursts change behavior | `docs/WAM_PROTOCOL.md`, `AGENTS.md` | regressions can look nondeterministic | keep one connection where possible and validate transport changes on M5 |
| medium | Large transport/lifecycle state machines concentrate responsibility | `pcm_cli.py`, `foo_out_wam.cpp`, `UpnpRenderer.kt` | risky edits and race regressions | change narrowly and pin every observed failure with tests |
| medium | Compatibility evidence is one M5 firmware/model | `README.md`, `docs/WAM_PROTOCOL.md` | behavior on other WAM units is unknown | state the tested envelope; expand only with measurements |
| medium | Documentation can contradict newer measured history | `foobar/README.md` vs root/status docs | wrong next-step decisions | treat newest measured status as authoritative and extend drift tests |

## 2) Technical Debt
Finite DLNA/share playback is proven but remains a separate experimental path rather than the default transport. TuneIn preset write operations are still deliberately unproven on hardware. `foobar/README.md` still calls the already-merged PR #21 path a pre-merge candidate. Ruff config selects `I` and `SIM`, but the main CI Ruff command explicitly selects only `E,F,B,UP`.

## 3) Security Concerns
| Risk | Category | Evidence | Current mitigation | Gap |
|---|---|---|---|---|
| LAN control and audio are cleartext/no-auth by protocol | trust-boundary | `AndroidManifest.xml`, `samsung.py` | same-LAN design, random stream/control tokens, Android peer routing | hostile LAN can observe traffic; speaker API itself offers no auth |
| untrusted network/XML inputs | injection/resource abuse | `samsung.py` | host/XML-name validation, escaping, bounded 1 MiB reads, timeouts | keep every new raw command on these helpers |
| raw probe data may contain TuneIn partner/serial values | secret leakage | `catalogue.py`, `.gitignore` | scratch directories ignored; docs warn not to log `stationurl` | manual captures still require care |

## 4) Performance and Scaling Concerns
The product is latency-sensitive rather than throughput-scaled. The dominant delay lives in speaker buffering and TCP backpressure, not Python HTTP throughput. Local-/24 fallback discovery may probe up to 254 hosts with up to 64 workers. `STARTUP_SILENCE_MS` is intentionally 0; adding artificial startup buffering directly worsens audible latency.

## 5) Fragile/High-Churn Areas
Recent 90-day churn is highest in `README.md`, `docs/DEVELOPMENT_STATUS.md`, `docs/WAM_PROTOCOL.md`, `foobar/foo_out_wam.cpp`, `tests/test_foobar_source.py`, `src/wambridge/samsung.py`, `src/wambridge/pcm_cli.py`, and Android service/activity files. Safe changes should read the latest status/protocol notes first, preserve measured comments, and run unit/source-contract tests before hardware work.

## 6) `[ASK USER]` Questions
1. [ASK USER] Is the long-term compatibility target explicitly “tested M5/WAM550-551 first”, or should new work proactively target unmeasured Samsung WAM models too?
2. [ASK USER] Is Python 3.13 a real supported minimum that should be exercised in CI, or is `>=3.13` only a permissive package floor while CI intentionally tracks 3.14?
3. [ASK USER] Should `foobar/README.md` remain a historical PR #21 snapshot, or should its pre-merge wording be updated to current post-merge reality?

## 7) Evidence
- `docs/WAM_PROTOCOL.md`
- `docs/DEVELOPMENT_STATUS.md`
- `foobar/README.md`
- `.github/workflows/build.yml`
- scan/churn from `git log` on `origin/main`
