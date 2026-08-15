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
  of the same source, 5.68 s against 6.67 s, and halved the spread. **`wav` stays opt-in.**
  Its transport passed the physical checklist on 2026-08-08 - complete track at a median
  1.00x, seamless track change, seek, pause/resume, clean shutdown, no leaks - but every one
  of those was read off instruments while nobody was listening, so the absence of audible
  artefacts is still unverified. It also costs depth: FLAC carries 24 bit through this path
  and `wav` is fixed at 16,
- `startup_silence` or `WAMBRIDGE_STARTUP_SILENCE`, milliseconds of leading silence,
  `0..10000`, default `1500`. Values outside that fall back to the default rather than
  reaching the helper, which would reject them and take the stream down with it,
- `buffer_extra` or `WAMBRIDGE_BUFFER_EXTRA`, milliseconds of queue kept on top of foobar's
  own buffer length, `0..10000`, default `2000`. Capacity is delay on this path almost one
  for one - the queue measured 3.79-3.99 s full of a 4.0 s capacity - so this is the largest
  single share of the roughly six seconds that reach the ear, and it was chosen rather than
  measured. Lower it to find where the pipe starves; starving shows up as `free` climbing in
  the `CLOCK` line and as audible dropouts,
- `start_volume_max` or `WAMBRIDGE_START_VOLUME_MAX`, the highest raw step the **first**
  helper of a playback session may start at, `0..30`, default `3`, `0` disables. Not a
  volume limit — the slider governs everything after the start. See the section below for
  why the slider needs this and a configured `volume` does not,
- `diagnostics=1` or `WAMBRIDGE_DIAGNOSTICS=1` for the per-second `CLOCK` line in the
  console (`target`, `offered`, `submitted`, `played`, `queued`, `write`, `buffered`,
  `free`, `capacity`, flags). Off by default. One line a second for the first 240 of each
  stream, then one every thirty seconds for as long as that stream lasts — it used to stop
  dead at 240, which made it useless for anything that is not a startup problem. A format
  change starts a new stream for this purpose, so a station changing sample rate restarts
  the burst. The line comes from the callback foobar uses to ask how much room the output
  has, so a gap in it means foobar stopped asking — a long pause or a full buffer does that
  as readily as a dead stream, and silence is not evidence of a dropout on its own. Turn it
  on before reporting anything about pacing — it is what attributed the runaway start to a
  dropped chunk rather than to the output clock.

**Nothing here is ignored quietly any more.** Unknown keys, a `format` that is not one of
the three, a `volume`, `volume_max`, `start_volume_max`, `startup_silence` or `buffer_extra`
that is not a number in range, and a `helper` path that does not exist all say so in the
console. `volume_max` was the last one still falling back in silence while this paragraph
already claimed otherwise. So do settings written as
`#key=value`: Windows comments start with `;`, so those are key names rather than disabled
lines and nothing reads them. The owner ran for days with `hardware_volume=1`, a key that
exists only on an unmerged branch, with nothing anywhere saying it was dead.

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

Routing the slider used to disable that protection entirely. With `hardware_volume=1` the
component passes the slider's level as an explicit `--volume`, and `choose_start_volume`
returns an explicit level before it ever looks at `max_start_volume` — so the safe start
existed in the code, was passed on the paths that did not need it, and was skipped on the
one that did. A slider left high after an evening of listening became the level the next
session opened at, on material whose loudness nobody knew yet.

`start_volume_max` (raw step, `0..30`, default `3`, `0` disables) caps the level handed to
the **first** helper of a session. It is not a volume limit: the slider governs everything
after the start, and moving it reaches the speaker in about a second over the held
connection. The cap lifts as soon as a helper reports `PLAYING`, so a seek cannot turn down
a level chosen mid-session.

**It applies only with `hardware_volume=1`.** That promise above — the slider governs
everything after the start — is the whole reason a capped start is tolerable, and with
routing off the slider is a host-side gain that never reaches the speaker. Capping there
would leave a quiet speaker raisable only one raw step per menu press. A configured `volume`
in the INI is left uncapped for the same reason from the other direction: it is a level
somebody chose, where a slider position is leftover state from last time.

Turning the cap off restores exactly what the helper did before it existed — the argument is
omitted rather than set to the speaker's maximum, so `wambridge-pcm` keeps its own default
clamp of `10`. Passing `30` there would have made disabling a safety limit *raise* the
ceiling.

The component also moves the slider to whatever level the helper reports in
`WAMBRIDGE PLAYING volume=<step>`. Without that the capped start leaves the slider pointing
at a level the speaker is not playing, and the first pixel of movement jumps straight to it
— the same surprise the cap removes, only deferred.

That sync is applied on the `CONTROL_PORT` line rather than on `PLAYING`, and only when
three things hold. The control socket must be up, because moving the slider sends the level
back out and without the socket that means launching a control process — a second connection
to `55001` while audio is streaming, which this project has already watched starve a stream.
The generation must still be current, or a `PLAYING` left in a retired helper's pipe moves
the slider on behalf of a helper being killed. And the reported step must not exceed
`volume_max`, because the inverse mapping clamps to that ceiling, so syncing a higher level
would write the ceiling back and quietly turn the speaker *down*.

**The mapping was never the problem.** Measured by ear on the M5 on 2026-08-15, slider onto
`0..10`: `1` inaudible, `2` barely there, `3` a little more, `4` clearly louder, `5`
distinct, `6` enough to cut through conversation, `7` comfortable listening. No cliff
anywhere in it. Reaching `7` by dragging sounds fine; reaching it cold is what made the
owner jump, which is why this is a cap on the starting point rather than a different curve.
Whether the M5's own raw steps are even in dB is still unmeasured — this reading is a
listener's judgement, not an instrument's.

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
