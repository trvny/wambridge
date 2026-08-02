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
- `WAMBRIDGE_VOLUME`,
- `format` or `WAMBRIDGE_FORMAT`, one of `flac` (default) or `mp3`. Anything else falls
  back to `flac`. Not a latency lever: `mp3` at 320 kbps measured worse than FLAC's
  700-900, 16.9 s against 13.4 s,
- `startup_silence` or `WAMBRIDGE_STARTUP_SILENCE`, milliseconds of leading silence,
  `0..10000`, default `1500`. Out-of-range values fall back to the default rather than
  reaching the helper, which would reject them and take the stream down with it,
- `hardware_volume=1` or `WAMBRIDGE_HARDWARE_VOLUME=1` to route the foobar volume slider
  to the speaker's own volume over `55001` instead of the host gain. Off by default,
- `volume_max` or `WAMBRIDGE_VOLUME_MAX` for the raw step the top of the slider maps to,
  `1..30`, default `10`,
- `diagnostics=1` or `WAMBRIDGE_DIAGNOSTICS=1` for the per-second `CLOCK` line in the
  console (`target`, `offered`, `submitted`, `played`, `queued`, `write`, `buffered`,
  `free`, `capacity`, flags). Off by default; it caps itself at 240 lines. Turn it on
  before reporting anything about pacing — it is what attributed the runaway start to a
  dropped chunk rather than to the output clock.

The M5 uses raw volume steps `0..30`; the UI must not pretend these are percentages until a
model-aware conversion exists.

## Transport design

The speaker pulls the encoded stream. TCP backpressure is the speaker-facing pacing
mechanism; do not add FFmpeg `-re` or socket throttling.

Foobar still needs its own bounded accounting. PR #21 keeps queued, writing and submitted
PCM in latency and releases capacity from one cumulative real-time clock anchored at
`AUDIO_STARTED`. The anchor may shift for pause only; it must not follow pipe-write speed.

## Volume

Two volumes exist and only one of them is responsive.

The host gain in `volume_set()` is applied where PCM leaves the queue, and about 13 s of
audio sits past that point, so moving the slider is heard 13 s later. The speaker's own
volume on `55001` answers in about a second. `hardware_volume=1` switches the slider to the
second one and stops applying the first, because applying both would attenuate twice.

Three things the routing has to get right:

- **The slider must not reach the speaker's maximum.** foobar starts at 0 dB and the M5's
  step 30 is very loud. The slider maps onto `0..volume_max`, default `10`.
- **A drag is not a click.** It emits a level per pixel, and each one that survived would
  spawn its own control-helper process against the shared control port. The dispatcher
  keeps only the newest pending level, spaces sends by 250 ms and skips a level equal to
  the one already sent. Menu actions are taken ahead of pending levels so an emergency stop
  never waits behind a drag.
- **dB are mapped linearly in amplitude**, which is what the host gain being replaced did,
  so the slider keeps its old meaning. Whether the M5's own steps are linear in amplitude
  is **not measured**. If they turn out to be perceptual this needs a curve; the mapping
  lives in one function, `volume_step_for`.

The host gain remains the default because it is the only volume that still works when the
speaker is unreachable.

The URL/PCM path does not hard-gate on `StartPlaybackEvent`. Physical M5 runs produced
audible output without a matching event before the old timeout. The listener stays alive to
log correlated starts and surface real failures.

Only one control connection and one FFmpeg may own a session. A second TCP listener can
compete with playback; a second FFmpeg splits the shared stdin.

## Menu behavior

Emergency stop and standby stop foobar before invoking the control helper. Commands are
serialized so button presses cannot launch overlapping control processes. Physical volume
commands operate in raw M5 steps.

Standby is misnamed. It stops and mutes, which leaves the speaker lit and fully powered.
The state a user recognises as the speaker sleeping comes only from `SetSleepTimer`, whose
`sleeptime` is in seconds and which clears itself after firing. Renaming the item or
switching it to a short sleep timer is open work; see the standby section of
`docs/WAM_PROTOCOL.md`.

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
