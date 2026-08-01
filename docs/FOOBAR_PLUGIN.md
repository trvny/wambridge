# foobar2000 component status and design

Current implementation and remaining control-surface work for `foo_out_wam`.

## Implemented

- `output_v6` component for foobar2000 2.x x64.
- Bundled `wambridge-pcm.exe` and `wambridge-control.exe`; no checkout or virtual
  environment is required after installation.
- PCM pipeline: foobar `f32le` -> FFmpeg FLAC -> local HTTP -> Samsung M5.
- `%LOCALAPPDATA%\WAMBridge\foobar.ini` configuration with environment overrides.
- `Playback -> WAM Bridge` submenu containing:
  - Emergency stop,
  - Standby,
  - Volume up,
  - Volume down,
  - Volume to safe level.
- Serialized control-helper queue with output reported in the foobar console.
- Restart and cleanup paths for format change, seek, stop and helper failure.

## Not implemented

- Native preferences page.
- Discovery and connection test UI.
- TuneIn/radio submenu.
- Dockable panel.
- Integrated finite share/DLNA playback for native duration and seek.

## Configuration

The component currently reads:

```ini
[wambridge]
device=M5
volume=3
```

Optional keys and overrides:

- `helper` or `WAMBRIDGE_PCM` for a development PCM helper,
- `WAMBRIDGE_CONTROL` for a development control helper,
- `WAMBRIDGE_DEVICE`,
- `WAMBRIDGE_VOLUME`.

The M5 uses raw volume steps `0..30`; the UI must not pretend these are percentages until a
model-aware conversion exists.

## Transport design

The speaker pulls the encoded stream. TCP backpressure is the speaker-facing pacing
mechanism; do not add FFmpeg `-re` or socket throttling.

Foobar still needs its own bounded accounting. PR #21 keeps queued, writing and submitted
PCM in latency and releases capacity from one cumulative real-time clock anchored at
`AUDIO_STARTED`. The anchor may shift for pause only; it must not follow pipe-write speed.

The URL/PCM path does not hard-gate on `StartPlaybackEvent`. Physical M5 runs produced
audible output without a matching event before the old timeout. The listener stays alive to
log correlated starts and surface real failures.

Only one control connection and one FFmpeg may own a session. A second TCP listener can
compete with playback; a second FFmpeg splits the shared stdin.

## Menu behavior

Emergency stop and standby stop foobar before invoking the control helper. Commands are
serialized so button presses cannot launch overlapping control processes. Physical volume
commands operate in raw M5 steps.

Do not restore the old `cp` warning. `cp` is normal for `SetUrlPlayback`; it is not evidence
that emergency stop should request a speaker power cycle.

## Preferences page, next

Add `preferences_page_v3` under Playback and keep INI compatibility for at least one
release. It should expose:

- speaker alias/IP and discovery,
- startup and maximum startup volume (`0..30` raw until conversion exists),
- output format,
- bundled/development helper selection,
- a test button showing model, firmware and connection result.

## Later UI

Radio/TuneIn controls and a dockable panel should reuse the same dispatcher and settings.
Native TuneIn can continue without the PC; proxied stations require the PC and local HTTP
bridge. Label that difference clearly.

## Merge gate

No output transport candidate is release-ready until the physical M5 completes a 3-5 minute
track at wall-clock speed with stable seekbar, a second track, pause/resume, stop/change and
clean helper/FFmpeg shutdown.
