![foobar2000](https://img.shields.io/badge/foobar2000-000?logo=foobar2000&logoColor=fff&style=for-the-badge) ![C++](https://img.shields.io/badge/C%2B%2B-00599C?logo=cplusplus&logoColor=fff&style=for-the-badge)
# Foobar2000 output

`foo_out_wam` exposes `Samsung M5 (Wi-Fi)` as a foobar2000 2.x x64 output.
It sends foobar's decoded PCM to the bundled `wambridge-pcm.exe`, which encodes
FLAC and offers it to the speaker through the local HTTP bridge.

This output remains experimental. A physical M5 starts playback, but longer
tracks have become accelerated or garbled after several seconds. PR #2 isolates
helper handles, while stacked PR #4 experiments with PCM pacing. Neither should
be merged before a fresh physical test.

Finite local-file playback through Samsung's DMS and queue protocol is being
researched separately in PR #7. See
[`../docs/DEVELOPMENT_STATUS.md`](../docs/DEVELOPMENT_STATUS.md) and
[`../docs/WAM_PROTOCOL.md`](../docs/WAM_PROTOCOL.md) before continuing output
work.

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
volume=3
```

The tested Shape M5 firmware uses raw API volume steps `0..30`; `3` is roughly
10 percent. Values above 30 are silently clamped to maximum. Model-aware
percentage conversion is not implemented yet.

The component ships its own `wambridge-pcm.exe`; a source checkout and Python
virtual environment are not needed after installation. `device` defaults to
`M5`, and `volume` may be omitted to preserve the speaker's current level under
the helper's safety ceiling. An explicit `helper` path remains available for
development builds.

The equivalent environment overrides are `WAMBRIDGE_PCM`, `WAMBRIDGE_DEVICE`
and `WAMBRIDGE_VOLUME`.

## Install a development build

Open the latest successful
[`Build`](https://github.com/trvny/wambridge/actions/workflows/build.yml)
workflow run and download the `foo_out_wam-x64` artifact. Extract it, open
`foo_out_wam.fb2k-component` with foobar2000 and then select:

```text
Preferences → Playback → Output → Samsung M5 (Wi-Fi)
```

The current component sends FLAC to the M5. Foobar's volume slider applies
software gain to PCM; the physical speaker level remains managed by WAM Bridge.

## Current behavior

- PCM is queued in memory and consumed by FFmpeg.
- Foobar volume and mute are applied when queued PCM is sent to the helper.
- Pausing keeps the active FLAC session alive with silence and retains queued
  audio for resume.
- Seeking and stopping restart the helper and FFmpeg so pre-flush PCM cannot
  leak into the next position.
- Cancelling startup closes the PCM pipe and lets the helper restore volume.
- Only helper protocol pipes should be inherited by the child process after
  PR #2.
- A PCM format change starts a fresh WAM session.
- Closing foobar stops the temporary local stream.
- A helper crash invalidates the output and is reported in the foobar console.

## Known timing problem

`pcm_stream.py` currently passes FFmpeg `-re` for a `pipe:0` source. Foobar
already supplies PCM according to its playback clock, and PR #4 adds another
pacing layer in C++. Multiple clocks may create drift or underruns.

Before adding another queue or sleep, test a single timing authority and verify
that a track longer than three minutes remains at normal speed.

## Physical M5 checklist

Before merging an output candidate, verify:

1. Start at raw speaker volume step `3` or lower.
2. A track longer than three minutes stays in real time.
3. Stop and seek work during startup and normal playback.
4. Pause and resume do not replay stale buffered audio.
5. Foobar volume and mute behave normally.
6. Cancelling startup restores the previous speaker volume.
7. Closing foobar leaves no orphan helper process.

## Manual helper test

<!-- markdownlint-disable MD013 -->
```powershell
cmd /d /c "ffmpeg -hide_banner -loglevel error -i C:\Music\test.opus -f f32le -acodec pcm_f32le -ar 48000 -ac 2 - | wambridge-pcm --device M5 --sample-rate 48000 --channels 2 --sample-format f32le --format flac --volume 3"
```
<!-- markdownlint-enable MD013 -->

Expected protocol markers:

```text
WAMBRIDGE READY
WAMBRIDGE PLAYING volume=3
```
