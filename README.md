![Samsung](https://img.shields.io/badge/Samsung-1428A0?logo=samsung&logoColor=fff&style=for-the-badge) ![Python](https://img.shields.io/badge/Python-3776AB?logo=python&logoColor=fff&style=for-the-badge)

[![Build](https://github.com/trvny/wambridge/actions/workflows/build.yml/badge.svg)](https://github.com/trvny/wambridge/actions/workflows/build.yml)

# WAM Bridge

<img src="https://images.samsung.com/is/image/samsung/pl_WAM550-EN_014_Front_black?$330_330_JPG$" width="128" alt="Samsung Shape M5"><img src="https://images.samsung.com/is/image/samsung/pl_WAM551-EN_014_Front_white?$330_330_JPG$" width="128" alt="Samsung Shape M5">

Windows-first bridge for streaming audio over Wi-Fi to Samsung Wireless Audio
Multiroom speakers, including Shape M5 (`WAM550`/`WAM551`).

The stable CLI path serves a tokenized local stream and starts it through
Samsung's `SetUrlPlayback` API. The foobar output is experimental while its
fixed-anchor clock is validated on a physical M5. Finite share/DLNA playback is
protocol-proven but not integrated.

## Status

- CLI discovery, saved devices, playback controls, custom radio stations and
  native TuneIn presets are implemented.
- GitHub Actions builds bundled helpers and the foobar2000 2.x x64 component.
- `Playback → WAM Bridge` provides emergency stop, standby and raw physical
  volume actions.
- The physical M5 produces audible output from foobar's `f32le → FLAC` path.
- PR #21 fixes host latency and capacity accounting. It remains unmerged until
  a complete 3–5 minute track, stable seekbar, transitions and cleanup pass on
  hardware.
- Helper isolation PR #2 is merged. Manual pacing PR #4 and the large share
  experiment PR #7 are closed; their measured conclusions remain documented.

Current architecture, failed approaches and continuation notes are in
[`docs/DEVELOPMENT_STATUS.md`](docs/DEVELOPMENT_STATUS.md). Measured protocol
facts from a physical `SPK-WAM550` are in
[`docs/WAM_PROTOCOL.md`](docs/WAM_PROTOCOL.md).

## Foobar2000 output

The [`Build`](https://github.com/trvny/wambridge/actions/workflows/build.yml)
workflow produces the `foo_out_wam-x64` artifact containing:

- `foo_out_wam.fb2k-component`
- `foo_out_wam.dll`
- a source archive

Open the `.fb2k-component` file with foobar2000 2.x x64, then select:

```text
Preferences → Playback → Output → Samsung M5 (Wi-Fi)
```

The artifact is for development testing, not a stable release. Configuration,
known limitations and the physical checklist are documented in
[`foobar/README.md`](foobar/README.md).

## Requirements

- Python 3.13+
- FFmpeg available in `PATH`
- computer and speaker reachable in the same LAN
- Windows 10/11 and foobar2000 2.x x64 for the native output component
- Windows Firewall access for the bridge on private networks

## Install the CLI

```powershell
git clone https://github.com/trvny/wambridge.git
cd wambridge
py -m venv .venv
.\.venv\Scripts\Activate.ps1
py -m pip install -e .
```

Confirm FFmpeg is visible:

```powershell
ffmpeg -version
```

## Discover speakers

```powershell
wambridge --discover
```

Discovery sends repeated SSDP requests through active IPv4 adapters. When old
firmware stays silent, it can fall back to checking Samsung's API on port
`55001` in nearby `/24` networks.

Useful diagnostics:

```powershell
wambridge --discover --verbose
wambridge --discover --interface 192.168.1.25
wambridge --discover --no-scan
```

## Saved devices

Save a speaker under an alias instead of relying on its DHCP address:

```powershell
wambridge --speaker 10.0.0.118 --remember M5
wambridge --list-devices
wambridge --device M5 --probe
wambridge "D:\Music\track.opus" --device M5
```

The profile stores the stable Samsung `device_id` and caches the latest working
IP. If the address changes, WAM Bridge searches the LAN for the same device and
updates the profile.

On Windows profiles are stored in:

```text
%LOCALAPPDATA%\WAMBridge\devices.json
```

The foobar2000 component passes the configured alias to the same helper and
profile resolver.

Remove a saved profile:

```powershell
wambridge --forget M5
```

## Startup volume safety

The tested Shape M5 firmware uses raw API volume steps `0..30`. Values above 30
are silently clamped to maximum while still returning success. The current
client has not yet implemented model-aware percentage conversion, so treat
volume arguments as raw M5 steps:

- `3` is approximately 10 percent,
- `6` is approximately 20 percent,
- `30` is maximum.

Old WAM firmware may jump to a high level while switching to URL playback. WAM
Bridge mutes the speaker, starts the stream with 1.5 seconds of silence and then
applies the requested bounded step after decoding begins.

Choose a cautious explicit level:

```powershell
wambridge "D:\Music\track.opus" --device M5 --volume 3
```

Change only the startup ceiling while preserving quieter current settings:

```powershell
wambridge "D:\Music\track.opus" --device M5 --max-start-volume 3
```

## Remote control

```powershell
wambridge --device M5 --status
wambridge --device M5 --set-volume 3
wambridge --device M5 --mute
wambridge --device M5 --unmute
wambridge --device M5 --pause
wambridge --device M5 --play
wambridge --device M5 --stop
wambridge --device M5 --standby
```

Native providers such as TuneIn use their CPM commands. DLNA uses UIC pause and
resume. Samsung URL playback cannot be resumed reliably, so starting the source
again creates a new session.

## Radio stations

Save a direct HTTP or HTTPS audio stream. Fallback URLs are tried in order:

```powershell
wambridge --radio-add paradise "https://primary.example/radio.mp3" `
  "https://backup.example/radio.ogg"
wambridge --radio-list
wambridge --radio-play paradise --device M5 --volume 3
wambridge --radio-remove paradise
```

Station definitions are stored in:

```text
%LOCALAPPDATA%\WAMBridge\stations.json
```

Import the bundled BBC Radio 1, PR3 Trójka and PR4 Czwórka pack:

```powershell
wambridge --radio-import top3
wambridge --radio-list
wambridge --radio-play bbc1 --device M5 --volume 3
wambridge --radio-play trojka --device M5 --volume 3
wambridge --radio-play czworka --device M5 --volume 3
```

These are WAM Bridge stations and do not overwrite the three presets selected
by the physical button on the speaker.

## Native TuneIn presets

Read and start TuneIn presets already stored by the speaker:

```powershell
wambridge --device M5 --tunein-list
wambridge --device M5 --tunein-play 0 --volume 3
wambridge --device M5 --tunein-play "Radio Paradise" --volume 3
```

Changing the speaker's TuneIn account or preset list still belongs to Samsung's
plugin because no reliable write API is known.

## Direct playback

```powershell
wambridge --speaker 192.168.1.50 --probe
wambridge "https://example.net/radio-stream" --speaker 192.168.1.50
wambridge "D:\Music\track.opus" --speaker 192.168.1.50
wambridge "D:\Music\track.ogg" --speaker 192.168.1.50
```

Use MP3 output when FLAC is unstable on a particular firmware:

```powershell
wambridge "D:\Music\track.opus" --speaker 192.168.1.50 --format mp3
```

When exactly one WAM speaker is discovered, `--speaker` may be omitted.

## Notes

- The local URL stream uses HTTP/1.0 without chunked transfer for old-firmware
  compatibility.
- The URL contains a random session token and exists only while the command is
  running.
- Do not expose port `55001` or a bridge HTTP port to the internet.
- The tested M5 does not expose a standard UPnP AVTransport renderer. See the
  protocol notes before restarting generic UPnP work.
- `SetUrlPlayback` may freeze malformed firmware when the served body is not
  playable audio. The bridge exposes only FFmpeg output and returns `404` for
  other paths.

## Validate

```powershell
py -m unittest discover -s tests -v
```

---

## 📰 Mininewsy

<!--README_FEED:START-->
- ["To nie będzie bimbrownia". Inwestor stanowczo odpowiada mieszkańcom - Przelom.pl](https://news.google.com/atom/articles/CBMisgFBVV95cUxPeWJIRnpQaW4wUUhxV0lGSE0xdklPZ0hleGxZYVhlQUdvcHdzSzFJR2NjRHlsVmJWVFpRa1J3X0lER3BOR3JVSkRodl9QMzN2UHlleWtZRlRyT2p3UU9OYkZQNUtDX3ltMUZTc3VWY1ZxRTZNbU1mbFB1bXdnbDRORC1ZX2hsYzd1eHNYVW9Tb3pzRlYtaEp5WWZLM254V09OZDZrdEw4MWlyVFBZN3NtTjFR?oc=5)
- [FIFA has scrapped $20 billion World Cup sell-off plan, New York Post reports](https://news.google.com/rss/articles/CBMiuwFBVV95cUxNbXdUU2FsekNqTWc1TWdVd09xUXpTTXNScnVjczNNcGVzbERTWmVwOWNGMGlOd2J2dWQwem4yOU4yb0Y0VThodnEzcWJTRjRBZjI2RUhsNDdNXzNtMmVCa2tBNVp2U3lraTE2ZjFObG9WYThxaGJBWVpTSjE2YV9TTnAwNWVXT0d0MjVTcWJlRUs2bjN5bUJzVEU0bG5NNlZ0X1pwSUZPRTF3TjN2QjNWWDRGeWR6SzhTd1pn?oc=5)
- [US bars imports from 43 more companies over China's alleged forced labor involving Uyghurs](https://news.google.com/rss/articles/CBMiwgFBVV95cUxOSzRCdVNpdm4wZldHbEt1WTduUnNQTnh6N2didEJVLTQ5RjBYSGtQemRacGtrRFdjOUt0Ukh0X0lyLS03cDA2YVRWd3piM3V6N1VZZkprcU52QzBJWW10XzRvTnZQUnBwTURxbkJabXduaV82Q0wyd2xWQW5DNERZM2poRlAtM2xmV0tNSGt4LWc0RkVhSTRSQktmeWRfMjlrR0pGQW93Nm9OUnoteEU4b3RQSWxNN2NZaEwxbHdSYUVQdw?oc=5)
- [Exxon, Chevron warn of continued high fuel prices from Iran war](https://news.google.com/rss/articles/CBMiqgFBVV95cUxNMjdEbTVuZzlMdmNabW90dk04NEdXMUdBUFNzR2FpcDJYZE9RWnlQdXFNV1NtZkVWZ2J1M0dzTXhwVGJFYlNsUkp2d211bFB4eXRQQXk3RWJ5c1hBZ1BUYnF3VXNiSzFweUpEaktkOUsyLVBISHlZY2dneU5RTnkyeXNTejVUZlpJa3hURjFsMGZkSVUxVUw4QUg0N3pqZjg0UlZVdTNGSG9kZw?oc=5)
- [US, Israel planning to bombard energy-related targets in Iran, CBS reports](https://news.google.com/rss/articles/CBMivAFBVV95cUxQNXpvc21vbVJNVjc3NWg0ZnppblFCM3dmQXFVVTNhc1A1YkhoSTA4Q0tFZ0hQbFN0WUdtRlBza0J1dzBFenk3NW85U0ZWLXA0N1llLVFCNUdmRy1JU2NzOTE2bTFTdmlYUXdZNUphTE1uSE84ZXA0WjBYNkUtX2NBbWpUS08taFBRc0xNdGtoV0F6SHpfdU15dFB1WllYNnVwQkxFWThYelpLZnQyRml2Q0MwLVZ2blFDWl9raA?oc=5)
- [EXCLUSIVE: OpenAI finds evidence other AI agents escaped containment as it widens hacking probe](https://news.google.com/rss/articles/CBMivAFBVV95cUxQYTc2SUhrNmVER0NvNW9nZHBwbklFMlA0eTNSRFZETXpfSWpVYU1wOUhaMDRLUnRVSEhtRzByeEktX2FEX1ZzaThNMnpZMW9JdVZDZkVRTEQ4UjdlakFPVXZTUjZaM2JOUkhpN1BxeEU4bWJuSjNkUldBNXRVWDkyYWVXVUlKM0VEZU5wZklfX2FJRkQ0bmVybTIyY0xpbHd6aGtIVmZ3YU5ocGdsdFlNeXdOQlBXOUsyZnZtZA?oc=5)
<!--README_FEED:END-->

## 💬 Cytat z szuflady

<!-- markdownlint-disable MD033 -->
<!--STARTS_HERE_QUOTE_README-->
<i>❝The person I like most is the one who points out my defects. — Umar ibn Al-Khattāb (R.A)❞</i>
<!--ENDS_HERE_QUOTE_README-->
<!-- markdownlint-enable MD033 -->
