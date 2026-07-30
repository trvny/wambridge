# Foobar2000 output

`foo_out_wam` exposes `Samsung M5 (Wi-Fi)` as a foobar2000 2.x x64 output.
It sends foobar's decoded PCM to `wambridge-pcm`, which encodes a FLAC stream
and offers it to the speaker through the existing local HTTP bridge.

## Requirements

- foobar2000 2.x x64
- FFmpeg available in `PATH`
- a saved speaker profile, for example `M5`

The component uses the foobar2000 SDK dated `2025-03-07` and implements the
stable `output_v6` API. Network and process work runs outside foobar's playback
thread.

## Configure

Create `%LOCALAPPDATA%\WAMBridge\foobar.ini`:

```ini
[wambridge]
device=M5
volume=4
```

The component ships its own `wambridge-pcm.exe`; the source checkout and Python
virtual environment are not needed after installation. `device` defaults to
`M5`, and `volume` may be omitted to preserve the speaker's current level under
the helper's normal safety ceiling. An explicit `helper` path remains available
for development builds.

The equivalent environment overrides are `WAMBRIDGE_PCM`, `WAMBRIDGE_DEVICE`
and `WAMBRIDGE_VOLUME`.

## Install

Download `foo_out_wam.fb2k-component` from the `WAM Bridge foobar` workflow
artifact, open it with foobar2000, then select:

```text
Preferences → Playback → Output → Samsung M5 (Wi-Fi)
```

The component always sends FLAC to the M5. Foobar's volume slider applies a
software gain to PCM; the physical speaker level remains managed by WAM Bridge.

## Behaviour

- PCM is queued in memory and consumed by FFmpeg at the configured sample rate.
- Foobar volume and mute are applied when queued PCM is sent to the helper.
- Pausing keeps the active FLAC session alive with FFmpeg-paced silence, while
  retaining queued audio for resume.
- Seeking and stopping reset the helper and FFmpeg so buffered pre-flush PCM
  cannot leak into the next position; new PCM starts a fresh WAM session.
- Cancelling startup closes the PCM pipe and lets the helper restore volume.
- A PCM format change still starts a fresh WAM session.
- Closing foobar stops the temporary local stream; the speaker cannot continue
  an original internet source independently.
- A helper crash invalidates the output and is reported in the foobar console.
- The speaker is muted before URL handoff, then raised to the bounded start level.
- The expected startup delay includes the 1.5-second volume-safety silence.

## Manual helper test

<!-- markdownlint-disable MD013 -->
```powershell
cmd /d /c "ffmpeg -hide_banner -loglevel error -i C:\Music\test.opus -f f32le -acodec pcm_f32le -ar 48000 -ac 2 - | wambridge-pcm --device M5 --sample-rate 48000 --channels 2 --sample-format f32le --format flac --volume 4"
```
<!-- markdownlint-enable MD013 -->

Expected protocol markers:

```text
WAMBRIDGE READY
WAMBRIDGE PLAYING volume=4
```
