![foobar2000](https://img.shields.io/badge/foobar2000-000?logo=foobar2000&logoColor=fff&style=for-the-badge) ![C++](https://img.shields.io/badge/C%2B%2B-00599C?logo=cplusplus&logoColor=fff&style=for-the-badge)
# Foobar2000 output

`foo_out_wam` exposes `Samsung M5 (Wi-Fi)` as a foobar2000 2.x x64 output.
It sends foobar's decoded PCM to the bundled `wambridge-pcm.exe`, which encodes
FLAC and offers it to the speaker through the local HTTP bridge.

The current candidate passed the automated test suite and Windows build. It
still requires final validation on a physical Samsung M5 before
[PR #2](https://github.com/trvny/wambridge/pull/2) is merged.

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

The component ships its own `wambridge-pcm.exe`; a source checkout and Python
virtual environment are not needed after installation. `device` defaults to
`M5`, and `volume` may be omitted to preserve the speaker's current level under
the helper's normal safety ceiling. An explicit `helper` path remains available
for development builds.

The equivalent environment overrides are `WAMBRIDGE_PCM`, `WAMBRIDGE_DEVICE`
and `WAMBRIDGE_VOLUME`.

## Install

Open the latest successful
[`Build`](https://github.com/trvny/wambridge/actions/workflows/build.yml)
workflow run and download the `foo_out_wam-x64` artifact. Extract it, open
`foo_out_wam.fb2k-component` with foobar2000 and then select:

```text
Preferences → Playback → Output → Samsung M5 (Wi-Fi)
```

The component always sends FLAC to the M5. Foobar's volume slider applies
software gain to PCM; the physical speaker level remains managed by WAM Bridge.

## Behaviour

- PCM is queued in memory and consumed by FFmpeg at the configured sample rate.
- Foobar volume and mute are applied when queued PCM is sent to the helper.
- Pausing keeps the active FLAC session alive with FFmpeg-paced silence while
  retaining queued audio for resume.
- Seeking and stopping reset the helper and FFmpeg so buffered pre-flush PCM
  cannot leak into the next position; new PCM starts a fresh WAM session.
- Cancelling startup closes the PCM pipe and lets the helper restore volume.
- Startup uses a longer graceful shutdown window than active playback because
  old WAM firmware may answer slowly.
- Only the helper protocol pipes are inherited by the child process; unrelated
  foobar component handles stay private.
- A PCM format change starts a fresh WAM session.
- Closing foobar stops the temporary local stream; the speaker cannot continue
  the original source independently.
- A helper crash invalidates the output and is reported in the foobar console.
- The speaker is muted before URL handoff, then raised to the bounded start
  level.
- The expected startup delay includes the 1.5-second volume-safety silence and
  the speaker's own response time.

## Physical M5 checklist

Before merging the candidate, verify:

1. A track longer than three minutes stays in real time.
2. Stop and seek work during startup and normal playback.
3. Pause and resume do not replay stale buffered audio.
4. Foobar volume and mute behave normally.
5. Cancelling startup restores the previous speaker volume.
6. Closing foobar does not crash and leaves no orphan helper process.

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
