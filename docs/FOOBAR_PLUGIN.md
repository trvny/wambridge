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
- `format` or `WAMBRIDGE_FORMAT`, one of `flac` (default), `wav` or `mp3`. Anything else
  falls back to `flac`. The prebuffer is partly bounded by bytes, confirmed from both
  directions: `mp3` at 320 kbps against FLAC's 700-900 measured *worse*, 16.9 s against
  13.4 s, because a thinner stream fits more seconds into the same space; `wav`, which is
  uncompressed 16-bit PCM fixed at 44.1 kHz, measured *better* than FLAC on every passage
  of the same source, 5.68 s against 6.67 s, and halved the spread. `wav` is opt-in until
  the rest of the physical checklist passes on it,
- `startup_silence` or `WAMBRIDGE_STARTUP_SILENCE`, milliseconds of leading silence,
  `0..10000`, default `1500`. Out-of-range values fall back to the default rather than
  reaching the helper, which would reject them and take the stream down with it,
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

The URL/PCM path does not hard-gate on `StartPlaybackEvent`. Physical M5 runs produced
audible output without a matching event before the old timeout. The listener stays alive to
log correlated starts and surface real failures.

Only one control connection and one FFmpeg may own a session. A second TCP listener can
compete with playback; a second FFmpeg splits the shared stdin.

## Startup volume is per session, not per helper

The configured `volume` is handed to the first helper of a playback session only. A seek or
a format change restarts the helper mid-session, and repeating the argument there overwrote
whatever the listener had set from the menu: measured on the M5 on 2026-08-08, the speaker
was walked up to `11`, one seek followed, and the new helper logged `Speaker volume is 11;
starting PCM playback at 3`. Replacement helpers are launched with the clamp raised to the
speaker's own maximum instead, so they leave the level alone. The safe clamp protects the
start of a session; it has no business overriding a level a person chose during one.

## Menu behavior

Emergency stop and standby stop foobar before invoking the control helper. Commands are
serialized so button presses cannot launch overlapping control processes. Physical volume
commands operate in raw M5 steps.

Standby is misnamed. It stops and mutes, which leaves the speaker lit and fully powered.
The state a user recognises as the speaker sleeping comes only from `SetSleepTimer`, whose
`sleeptime` is in seconds and which clears itself after firing. Renaming the item or
switching it to a short sleep timer is open work; see the standby section of
`docs/WAM_PROTOCOL.md`.

What standby does now guarantee is that nothing local is still attached. After the stop and
the mute it waits up to `STANDBY_RELEASE_TIMEOUT` for established TCP connections to the
speaker to drop, and reports `holding=<count>`, or `holding=unknown` when the socket table
could not be read. A remaining hold adds a `warning=` line rather than failing the action:
the mute and the stop did land, and the caller may have asked while something else was
streaming. This targets the documented case of a hard-killed session leaving the M5 lit for
hours — a leaked helper keeps both the control socket and the audio pull open. It is the
best lead available, not a proven cause.

`holding=unknown` is deliberately not `holding=0`. Reporting a speaker as released when it
was never checked is the failure this exists to prevent.

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
