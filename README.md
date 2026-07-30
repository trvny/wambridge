<p align="center">
  <img src="https://www.google.com/s2/favicons?domain=samsung.com&sz=64" alt="samsung">
</p>

# WAM Bridge
<img src="https://images.samsung.com/is/image/samsung/pl_WAM550-EN_014_Front_black?$330_330_JPG$" width="128" alt="wam550">

Windows-first proof of concept for streaming audio over Wi-Fi to Samsung
Wireless Audio Multiroom speakers, including Shape M5 (`WAM550`/`WAM551`).

It runs FFmpeg locally, exposes a tokenized HTTP stream in the LAN and starts
it through Samsung's local `SetUrlPlayback` API. Source formats are decoded by
FFmpeg, so Opus, Ogg Vorbis, AAC, FLAC, MP3 and radio streams can all be sent
as a conservative FLAC or MP3 stream understood by the speaker.

## Status

Working CLI bridge with discovery, saved devices, radio presets and native
TuneIn control. An experimental foobar2000 2.x x64 output component is built
by GitHub Actions and still requires final validation on a physical M5.

## Foobar2000 output

The Windows workflow builds `foo_out_wam.fb2k-component` with a bundled
`wambridge-pcm.exe`. Configuration and test notes are in
[`foobar/README.md`](foobar/README.md).

## Requirements

- Windows 10/11 or another system with Python 3.11+
- FFmpeg available in `PATH`
- computer and speaker reachable in the same LAN
- Windows Firewall access for Python on private networks

## Install

```powershell
cd wambridge
py -m venv .venv
.\.venv\Scripts\Activate.ps1
py -m pip install -e .
```

Confirm FFmpeg is visible:

```powershell
ffmpeg -version
```

## Use

Discover speakers:

```powershell
wambridge --discover
```

Discovery sends repeated SSDP requests through every active IPv4 adapter.
When old firmware stays silent, it falls back to checking Samsung's API on
port `55001` in nearby `/24` networks. The result shows whether the speaker
was found through `ssdp` or `api-scan`.

Useful diagnostics:

```powershell
wambridge --discover --verbose
wambridge --discover --interface 192.168.1.25
wambridge --discover --no-scan
```

### Saved devices

A DHCP address may change. Save the speaker once under an alias instead of
using its IP permanently:

```powershell
wambridge --speaker 10.0.0.118 --remember M5
wambridge --list-devices
wambridge --device M5 --probe
wambridge "D:\Music\track.opus" --device M5
```

The profile stores the speaker's stable `device_id` and only caches its last
working IP. If the address stops matching that ID, WAM Bridge searches the
LAN, finds the same device and updates the profile. On Windows profiles are
stored in `%LOCALAPPDATA%\WAMBridge\devices.json`; this is also the device
source planned for the foobar2000 component.

Remove a saved profile:

```powershell
wambridge --forget M5
```

### Startup volume safety

Old WAM firmware may jump to a high volume while switching to URL playback.
WAM Bridge keeps the speaker at `0`, starts the stream with 1.5 seconds of
silence, then applies the requested level after decoding has begun. Without
an explicit value, the current level is preserved only up to the default
ceiling of `10`.

Choose an explicit level:

```powershell
wambridge "D:\Music\track.opus" --device M5 --volume 6
```

Change only the safety ceiling while preserving quieter current settings:

```powershell
wambridge "D:\Music\track.opus" --device M5 --max-start-volume 20
```

### Remote control

Inspect and control the saved speaker without opening Samsung's application:

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

Native content providers such as TuneIn use their CPM play, pause and stop
commands. DLNA uses UIC pause and resume. Samsung URL playback cannot be
reliably resumed, so `--stop` pauses and mutes it; start the source command
again to play it. `--standby` also mutes native playback so the speaker can
enter its automatic standby state.

### Radio stations

Save any direct HTTP or HTTPS radio stream under your own alias. Optional
fallback URLs are tried in order when a stream fails:

```powershell
wambridge --radio-add paradise "https://primary.example/radio.mp3" `
  "https://backup.example/radio.ogg"
wambridge --radio-list
wambridge --radio-play paradise --device M5 --volume 6
wambridge --radio-remove paradise
```

The station list is stored in
`%LOCALAPPDATA%\WAMBridge\stations.json` on Windows. Existing files containing
only one `url` per station remain compatible. Sources are decoded by FFmpeg,
so the M5 receives the same conservative FLAC or MP3 stream as it does for
local files.

Import the bundled three-station set:

```powershell
wambridge --radio-import top3
wambridge --radio-list
wambridge --radio-play bbc1 --device M5 --volume 4
wambridge --radio-play trojka --device M5 --volume 4
wambridge --radio-play czworka --device M5 --volume 4
```

The `top3` pack contains BBC Radio 1, PR3 Trójka and PR4 Czwórka, each with a
primary and backup stream. TuneIn web-page addresses are not included because
they are pages rather than direct audio inputs.

This pack is stored and played by WAM Bridge. It does not overwrite the three
native presets selected by the physical button on the M5 because no reliable
preset-write API is known.

WAM Bridge can also read and start the native TuneIn presets stored by the
speaker. This includes the `my` presets synchronized after signing in through
Samsung's TuneIn plugin:

```powershell
wambridge --device M5 --tunein-list
wambridge --device M5 --tunein-play 0 --volume 6
wambridge --device M5 --tunein-play "Radio Paradise" --volume 6
```

Custom stations are managed by WAM Bridge. Native TuneIn presets are read and
played from the speaker; changing the speaker's TuneIn account or preset list
still belongs to Samsung's plugin because no reliable write API is known.

Test a known speaker:

```powershell
wambridge --speaker 192.168.1.50 --probe
```

Play an internet radio stream:

```powershell
wambridge "https://example.net/radio-stream" --speaker 192.168.1.50
```

Play a local Opus or Ogg file:

```powershell
wambridge "D:\Music\track.opus" --speaker 192.168.1.50
wambridge "D:\Music\track.ogg" --speaker 192.168.1.50
```

Use MP3 output when FLAC is unstable on a particular firmware:

```powershell
wambridge "D:\Music\track.opus" --speaker 192.168.1.50 --format mp3
```

When exactly one WAM speaker is discovered, `--speaker` may be omitted.

## Notes

- The local stream uses HTTP/1.0 without chunked transfer for compatibility
  with old firmware.
- The URL contains a random session token and exists only while the command
  is running.
- Do not expose port `55001` or the bridge HTTP port to the internet.
- URL playback in Samsung firmware has unreliable pause/resume behaviour.
  This PoC stops the session instead of attempting to preserve it.
- `SetUrlPlayback` may freeze malformed firmware when the served body is not
  audio. The bridge only exposes FFmpeg output and returns `404` for other
  paths.

## Validate

```powershell
py -m unittest discover -s tests -v
```
