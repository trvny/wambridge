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
- `diagnostics=1` or `WAMBRIDGE_DIAGNOSTICS=1` for the per-second `CLOCK` line in the
  console (`target`, `offered`, `submitted`, `played`, `queued`, `write`, `buffered`,
  `free`, `capacity`, flags). Off by default; it caps itself at 240 lines. Turn it on
  before reporting anything about pacing — it is what attributed the runaway start to a
  dropped chunk rather than to the output clock.

**Nothing here is ignored quietly any more.** Unknown keys, a `format` that is not one of
the three, a `volume`, `startup_silence` or `buffer_extra` that is not a number in range,
and a `helper` path that does not exist all say so in the console. So do settings written as
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

## Ending a stream is a command, not just a close

Closing the local HTTP server does not end anything as far as the speaker is concerned. It
was told to play a URL and it keeps that session, and a speaker that believes it is still
serving one never reaches the idle state its own power-down needs: a normal `Shutting
down...` with a stream still up left the M5 lit all night on 2026-08-08.

The helper therefore releases the speaker on its way out, over the persistent `55001`
connection it already owns rather than a second one. Three rules hold it together:

- It runs from `PlaybackWatcher.__exit__`, so failed sessions release too. Those are the ones
  that used to walk away from a speaker still holding a session.
- It is best effort. Teardown usually runs because something already went wrong, and a second
  exception there would bury the first. What happened is reported instead.
- It sends no mute and no `pwron`. A mute would hand the speaker back silent to whoever picks
  it up next, and `pwron` would wake what is being released.

Every session then ends with one line: `WAMBRIDGE STOPPED stop=<sent|rejected|unreachable|
skipped> sleep=<off|Ns> holding=<count>`. Before it there was nothing at all in the console
at the end of a stream, which is why the morning after a speaker that stayed lit there was
nothing to read. All three fields appear on every path, including one that failed before the
watcher existed.

`stop=skipped` also covers an offer the speaker **refused**. A matched rejection means it
never took the URL, so it is still doing whatever it was doing before, and a release would
reach past this helper and pause that instead.

`holding` excludes sockets owned by this helper. Measured on 2026-08-15, a locally closed
socket sits in `FIN_WAIT` for a further 0.5 s to 1.5 s, and the count is taken right after
teardown closes its own — so counting them reported this helper's own exit as something
holding the speaker, and made every helper exit wait it out. That wait lands in the seek
path, where the component stops one helper before starting its replacement. A killed
session's sockets still count: their owner is gone, so its PID cannot match ours.

`sleep_after_stop` arms `SetSleepTimer` once the stream ends, in seconds, `0` and off by
default. It is the only lever this firmware answers, and it is opt-in because powering the
speaker down is the listener's decision.

It is **not finished**, and the gap is in the seek path. A seek restarts the helper
mid-session, so the departing helper arms a timer that the stream replacing it never asked
for. The replacement clears any pending timer before offering its stream, which closes the
common case but is a race, not a guarantee: it only gets there after discovery, probing and
its own server coming up, so a short enough timer fires first and the speaker goes into
standby mid-track. Closing that properly needs the component to tell the helper whether it is
being replaced or the session is ending — it knows, and the helper does not. Two consequences
worth stating while it stands: the clear removes **any** pending timer, including one set from
the Samsung app, because the speaker does not say who armed it; and a configured install that
goes back to `0` leaves the last timer armed with nothing left to clear it. A default install
never sends either command.

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
`sleeptime` is in seconds and which clears itself after firing. The stream path now offers
that through `sleep_after_stop`; renaming this menu item or pointing it at the same timer is
still open work. See the standby section of `docs/WAM_PROTOCOL.md`.

What standby does now guarantee is that nothing local is still attached. After the stop and
the mute it waits up to `STANDBY_RELEASE_TIMEOUT` for established TCP connections to the
speaker to drop, and reports `holding=<count>`, or `holding=unknown` when the socket table
could not be read. Sockets owned by the action's own process are excluded; its stop, mute and
verification each opened one, and waiting those out would report the action's own requests as
a hold. A remaining hold adds a `warning=` line rather than failing the action: the mute and
the stop did land, and the caller may have asked while something else was streaming.

This is not the explanation for a speaker that stays lit, and it should not be read as one.
The M5 was still on the morning after the whole computer had been shut down, and a
powered-off host holds no sockets. Whatever keeps the speaker awake outlives its peer, which
puts it in the speaker's own state — the `SetUrlPlayback` session nothing ever ended. The
reading is worth having as proof that this end let go, not as the lead.

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
