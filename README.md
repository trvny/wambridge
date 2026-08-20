![Samsung](https://img.shields.io/badge/Samsung-1428A0?logo=samsung&logoColor=fff&style=for-the-badge) ![Python](https://img.shields.io/badge/Python-3776AB?logo=python&logoColor=fff&style=for-the-badge)

[![Release](https://github.com/trvny/wambridge/actions/workflows/release.yml/badge.svg)](https://github.com/trvny/wambridge/actions/workflows/release.yml) <a href="https://deepwiki.com/trvny/wambridge"><img src="https://deepwiki.com/badge.svg" alt="DeepWiki"></a>

# WAM Bridge

<img src="https://images.samsung.com/is/image/samsung/pl_WAM550-EN_014_Front_black?$330_330_JPG$" width="128" alt="Samsung Shape M5"><img src="https://images.samsung.com/is/image/samsung/pl_WAM551-EN_014_Front_white?$330_330_JPG$" width="128" alt="Samsung Shape M5">

Windows-first bridge for streaming audio over Wi-Fi to Samsung Wireless Audio
Multiroom speakers, including Shape M5 (`WAM550`/`WAM551`).

The CLI serves a tokenized local stream and starts it through Samsung's
`SetUrlPlayback` API. The foobar2000 output component sends whatever foobar is
playing, including internet radio. Finite share/DLNA playback is protocol-proven
but not integrated.

Everything here was measured against one physical Shape M5 (`SPK-WAM550`,
firmware `WAM550WWB-3117.1`). Other models in the family are untested.

## Status: working [`alpha`](https://github.com/trvny/wambridge/releases/tag/alpha)

Both paths play audio on real hardware. The foobar component passed its full
physical checklist on 2026-08-02: a complete 213-second track start to finish at
a median 1.00x with every sample between 0.9x and 1.1x, seek, pause and resume,
an unattended transition into the next track, internet radio across a 44.1 to
48 kHz switch, and a clean shutdown leaving no FFmpeg or helper behind.

What works:

- SSDP discovery, saved devices resolved by stable device ID, playback control,
  custom radio stations and native TuneIn presets from the CLI.
- foobar2000 2.x x64 output: `f32le → FFmpeg FLAC → local HTTP → speaker`.
- `Playback → WAM Bridge` with emergency stop, standby and raw volume steps.
- Configuration through `%LOCALAPPDATA%\WAMBridge\foobar.ini`.

### The one limitation worth knowing before you install

**Audio reaches the speaker about 6 seconds after foobar plays it.** This is
measured, not estimated: 6.7 s on the default FLAC profile and 5.7 s on the
optional `wav` one, both **with `startup_silence=0` in the INI**. The shipped
default prepends 1.5 s of silence on top of that, so a stock installation is
nearer 8 s until you turn it off. The largest single share is this project's own
4 s output buffer, not the speaker. An earlier figure of 13 s, most of it blamed
on the speaker's prebuffer, did not survive being measured again.

Playback itself is unaffected — the stream runs at wall-clock speed and the
seekbar is honest. What suffers is **control latency**: pause, stop, skip and
the volume slider all act on audio the speaker will not play for another few
seconds. Lowering the bitrate makes it worse rather than better, and raising it
helps by about a second, both measured. The fix is to route each control onto
the speaker's own `55001` command path, which answers in about a second, rather
than to keep shortening the audio path; that work is in progress.

Everything else is documented honestly, including the approaches that failed:

- helper isolation (PR #2) and the output clock (PR #21) are merged,
- manual pacing (PR #4) and the large share experiment (PR #7) are closed, with
  their measured conclusions kept.

Current architecture, failed approaches and continuation notes are in
[`docs/DEVELOPMENT_STATUS.md`](docs/DEVELOPMENT_STATUS.md). Measured protocol
facts from a physical `SPK-WAM550` are in
[`docs/WAM_PROTOCOL.md`](docs/WAM_PROTOCOL.md).

## Foobar2000 output

Rolling prerelease,
[`alpha`](https://github.com/trvny/wambridge/releases/tag/alpha), rebuilt whenever `main`
moves. The link does not change and both halves always come from the same commit. The
version comes from `version` in `pyproject.toml`, the only place it is written down:

- `foo_out_wam.fb2k-component` - the foobar2000 output
- `wambridge-<version>-alpha.<date>.<commit>.apk` - the Android adapter
- a source archive

Open the `.fb2k-component` file with foobar2000 2.x x64, then select:

```text
Preferences → Playback → Output → Samsung M5 (Wi-Fi)
```

This is alpha software built by one person against one speaker. It works, and it
is not polished: the preferences page is new and the INI is still the fuller
file, and the control latency above is real. If you own a Shape speaker and were
looking for exactly this, it should serve you — just read the limitation first.

Configuration, known limitations and the physical checklist are documented in
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

A station can also carry its TuneIn id. The id is resolved when the station is played, so it
follows the broadcaster if the stream address moves - the URLs saved beside it stay as
fallbacks for when TuneIn cannot be reached or offers nothing usable:

```powershell
wambridge --radio-add trojka "http://41.dktr.pl:8000/trojka.ogg" --tunein-id s15984
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
- [US debt crosses $40 trillion threshold after doubling under Trump and Biden](https://www.reuters.com/world/us-debt-crosses-40-trillion-threshold-after-doubling-under-trump-biden-2026-08-19/)
- [Dwa miasta, dwie trasy i setki rowerów. „Zakręceni sąsiedzi” wracają! - Przelom.pl - portal ziemi chrzanowskiej](https://news.google.com/atom/articles/CBMivAFBVV95cUxNdV9rWVhja3MybWdTV2pLeEFhcVUxUHRtbTFreFJIUXZLcGxfMEpqNkdBQ1gwTjRqWVpKb1BMT3dWYmVBX1N0Y1RFako2ZlJRbGJLQjQyTGxnOGJ4ZHJjTVlBNktWSXNLMFFiTkl4SHRKSWpXZzV6UFVKVVJRcXAyMDBTdDllbVI1bWtaR3RndVBwejYzVmRvbjJCZUt1MlJnemxmTXloNlpZdDlpY0dLdkhrUjVFS2JvMy1TcA?oc=5)
- [Liberia's former vice president charged in probe of transnational drug ring, government says](https://www.reuters.com/world/africa/liberias-former-vice-president-charged-transnational-narcotics-investigation-2026-08-19/)
- [Garmin Fenix 8 na dużej przecenie. Czyszczenie magazynów](https://antyweb.pl/garmin-fenix-8-na-duzej-przecenie-czyszczenie-magazynow)
- [Google dodaje quizy, symulacje i nowe funkcje Lens do wyszukiwarki na nowy rok szkolny](https://promptowy.com/google-quizy-symulacje-lens-wyszukiwarka-rok-szkolny/)
- [UN's Guterres seriously concerned by US sanctions on ICC](https://www.reuters.com/world/uns-guterres-seriously-concerned-by-us-sanctions-icc-2026-08-19/)
<!--README_FEED:END-->

## 💬 Cytat z szuflady

<!-- markdownlint-disable MD033 -->
<!--STARTS_HERE_QUOTE_README-->
<i>❝I alone cannot change the world, but I can cast a stone across the waters to create many ripples. — Mother Teresa❞</i>
<!--ENDS_HERE_QUOTE_README-->
<!-- markdownlint-enable MD033 -->
