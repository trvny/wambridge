# External Integrations

## 1) Integration Inventory
| System | Type | Purpose | Auth model | Criticality | Evidence |
|---|---|---|---|---|---|
| Samsung WAM M5 | local HTTP/TCP UIC+CPM on 55001 | control, status, URL/share playback | none; trusted LAN | high | `src/wambridge/samsung.py` |
| SSDP | UDP multicast 239.255.255.250:1900 | speaker discovery / Android renderer discovery | none | high | `discovery.py`, `UpnpRenderer.kt` |
| FFmpeg | local process | decode/transcode/encode audio | local executable | high | `stream.py`, `pcm_stream.py` |
| TuneIn | HTTP API/playlist resolution | presets, catalogue IDs, current stream URLs | no app secret for `Tune.ashx`; speaker catalogue may expose partner data | medium | `tunein.py`, `catalogue.py` |
| foobar2000 | SDK/runtime host | desktop playback source and UI | local host API | high | `foobar/` |
| UPnP/DLNA control points | LAN HTTP/SSDP | feed Android MediaRenderer | local network routing rules | medium | `mobile/README.md`, `UpnpRenderer.kt` |
| GitHub | CI/releases | build and publish component/APK | Actions token + release signing secrets | delivery | `.github/workflows/release.yml` |

## 2) Data Stores
There is no database. Python persists versioned JSON device profiles, radio stations and a client UUID under per-user config paths. Alias-backed profile/station stores use temp-file + `os.replace`; client UUID persistence writes its JSON file directly. Android uses private `SharedPreferences`, including its renderer/radio state and serialized station list. Bundled station data has one maintained source, `src/wambridge/station_packs.json`, copied into Android assets during build.

## 3) Secrets and Credentials Handling
Android signing credentials come from GitHub secrets into `WAMBRIDGE_KEYSTORE_*`/key environment variables. Runtime speaker control has no protocol authentication. Random URL/control tokens protect desktop local servers. `GetStationData.stationurl` can contain TuneIn partner/serial data and must not be logged; probe scratch directories carrying raw XML are ignored.

## 4) Reliability and Failure Behavior
Network calls have explicit timeouts and bounded reads. Discovery falls back from SSDP to parallel local-/24 probing. CPM catalogue reads retry transient empty/refused responses. Radio playback has TuneIn-to-static-URL fallbacks. Before `SetUrlPlayback`, desktop and Android perform a TCP reachability check because offering a dead endpoint can wedge the speaker until power-cycle.

## 5) Observability for Integrations
Python logs protocol/stream failures and optional DEBUG diagnostics. The foobar helper exposes a line protocol (READY/AUDIO_STARTED/PLAYING/ERROR plus control endpoint) and optional clock counters. Android foreground services surface state in notifications. There is no remote metrics/tracing backend.

## 6) Evidence
- `src/wambridge/alias_store.py`
- `src/wambridge/samsung.py`
- `src/wambridge/tunein.py`
- `mobile/app/src/main/AndroidManifest.xml`
- `.github/workflows/mobile.yml`
