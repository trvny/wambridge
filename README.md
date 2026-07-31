![foobar2000](https://img.shields.io/badge/foobar2000-000?logo=foobar2000&logoColor=fff&style=for-the-badge) ![Samsung](https://img.shields.io/badge/Samsung-1428A0?logo=samsung&logoColor=fff&style=for-the-badge) ![C++](https://img.shields.io/badge/C%2B%2B-00599C?logo=cplusplus&logoColor=fff&style=for-the-badge) ![Python](https://img.shields.io/badge/Python-3776AB?logo=python&logoColor=fff&style=for-the-badge)

[![Build](https://github.com/trvny/wambridge/actions/workflows/build.yml/badge.svg)](https://github.com/trvny/wambridge/actions/workflows/build.yml)

# WAM Bridge

<img src="https://images.samsung.com/is/image/samsung/pl_WAM550-EN_014_Front_black?$330_330_JPG$" width="128" alt="Samsung Shape M5">

Windows-first bridge for streaming audio over Wi-Fi to Samsung Wireless Audio
Multiroom speakers, including Shape M5 (`WAM550`/`WAM551`).

WAM Bridge decodes local files, internet radio and foobar2000 PCM with FFmpeg,
serves a short-lived tokenized stream in the LAN and starts it through Samsung's
local `SetUrlPlayback` API.

## Status

This repository is the source of truth after migration from `trvny/trvny`.

- CLI discovery, saved devices, playback controls, custom radio stations and
  native TuneIn presets are implemented.
- The foobar2000 2.x x64 output component is built by GitHub Actions with a
  bundled standalone helper.
- The current component candidate passed 70 automated tests and a full Windows
  build. Physical validation on a Samsung M5 is still required before merging
  [PR #2](https://github.com/trvny/wambridge/pull/2).

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

Configuration, behaviour and manual test notes are documented in
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

Old WAM firmware may jump to a high level while switching to URL playback.
WAM Bridge mutes the speaker, starts the stream with 1.5 seconds of silence and
then applies a bounded level after decoding begins.

Choose an explicit level:

```powershell
wambridge "D:\Music\track.opus" --device M5 --volume 6
```

Change only the startup ceiling while preserving quieter current settings:

```powershell
wambridge "D:\Music\track.opus" --device M5 --max-start-volume 20
```

## Remote control

```powershell
wambridge --device M5 --status
wambridge --device M5 --set-volume 6
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
wambridge --radio-play paradise --device M5 --volume 6
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
wambridge --radio-play bbc1 --device M5 --volume 4
wambridge --radio-play trojka --device M5 --volume 4
wambridge --radio-play czworka --device M5 --volume 4
```

These are WAM Bridge stations and do not overwrite the three presets selected
by the physical button on the speaker.

## Native TuneIn presets

Read and start TuneIn presets already stored by the speaker:

```powershell
wambridge --device M5 --tunein-list
wambridge --device M5 --tunein-play 0 --volume 6
wambridge --device M5 --tunein-play "Radio Paradise" --volume 6
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

- The local stream uses HTTP/1.0 without chunked transfer for old-firmware
  compatibility.
- The URL contains a random session token and exists only while the command is
  running.
- Do not expose port `55001` or the bridge HTTP port to the internet.
- `SetUrlPlayback` may freeze malformed firmware when the served body is not
  playable audio. The bridge exposes only FFmpeg output and returns `404` for
  other paths.

## Validate

```powershell
py -m unittest discover -s tests -v
```
