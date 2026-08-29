# foobar2000 component status and design

Current implementation and remaining control-surface work for `foo_out_wam`.

## Implemented

- `output_v6` component for foobar2000 2.x x64.
- Bundled `wambridge-pcm.exe` and `wambridge-control.exe`; no checkout or virtual
  environment is required after installation.
- PCM pipeline: foobar `f32le` -> FFmpeg FLAC -> local HTTP -> Samsung M5.
- `%LOCALAPPDATA%\WAMBridge\foobar.ini` configuration with environment overrides.
- Native `Preferences -> Playback -> Output -> WAM Bridge` page editing the same INI,
  with foobar2000 dark-mode theming.
- `Playback -> WAM Bridge` submenu containing:
  - Emergency stop,
  - Stop & mute,
  - Volume up,
  - Volume down,
  - Volume to safe level.
- Serialized control-helper queue with output reported in the foobar console.
- Restart and cleanup paths for format change, seek, stop and helper failure.

## Not implemented

- Discovery and connection test UI.
- TuneIn/radio submenu.
- Dockable panel.
- Integrated finite share/DLNA playback for native duration and seek.

## Configuration

The native preferences page writes the existing INI rather than introducing a second
configuration store. Runtime and UI use the same parser, defaults and ranges. Existing files
remain valid; when a known INI value is malformed or out of range, the page shows the
effective fallback, enables Apply and rewrites or removes the stale value on save.
`WAMBRIDGE_*` environment variables still take precedence and are never rewritten by the
page. Changes are marked as requiring a playback restart and are used by the next output
session. The page follows foobar2000's light/dark appearance through the core dark-mode
hooks.

The component reads:

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
- `format` or `WAMBRIDGE_FORMAT`, one of `flac` (default), `wav`, `wav24` or `mp3`. Anything
  else falls back to `flac`. The prebuffer is partly bounded by bytes, confirmed from both
  directions: `mp3` at 320 kbps against FLAC's 700-900 measured *worse*, 16.9 s against
  13.4 s, because a thinner stream fits more seconds into the same space; `wav`, which is
  uncompressed 16-bit PCM fixed at 44.1 kHz, measured *better* than FLAC on every passage
  of the same source, 5.68 s against 6.67 s, and halved the spread. **`wav` stays opt-in.**
  Its transport passed the physical checklist on 2026-08-08 - complete track at a median
  1.00x, seamless track change, seek, pause/resume, clean shutdown, no leaks - but every one
  of those was read off instruments while nobody was listening, so the absence of audible
  artefacts is still unverified. It also costs depth: FLAC carries 24 bit through this path
  and `wav` is fixed at 16. `wav24` is the same lever pulled harder — 2117 kbps against
  1411, at 24 bit — and **the M5 does accept it**, first heard on 2026-08-15. It is not
  recommended: across that day the speaker closed the stream by itself thirteen minutes in,
  twice, while a `flac` run over the same station reached twenty-seven without trouble. One
  day is not a verdict, but nothing so far argues for spending the bandwidth,
- `startup_silence` or `WAMBRIDGE_STARTUP_SILENCE`, milliseconds of leading silence,
  `0..10000`, default `0`. Values outside that fall back to the default rather than
  reaching the helper, which would reject them and take the stream down with it,
- `buffer_extra` or `WAMBRIDGE_BUFFER_EXTRA`, milliseconds of queue kept on top of foobar's
  own buffer length, `0..10000`, default `0`. A 2026-08-27 hardware sweep at
  1500/1000/500/0 found no starvation. At 0, the 2.0 s capacity stayed 1.83-2.00 s full
  for three minutes and `free` stayed in 0-167 ms; higher values add almost the same
  amount of delay as queue. The remaining 2.0 s floor has not been tested lower,
- `start_volume_max` or `WAMBRIDGE_START_VOLUME_MAX`, the highest raw step the **first**
  helper of a playback session may start at, `0..30`, default `3`, `0` disables. Not a
  volume limit — the slider governs everything after the start. See the section below for
  why the slider needs this and a configured `volume` does not,
- `sleep_after_stop` or `WAMBRIDGE_SLEEP_AFTER_STOP`, seconds of sleep timer armed once a
  stream ends, `0..86400`, default `0` (off). `SetSleepTimer` is the only power lever this
  firmware answers, and it stays opt-in because powering the speaker down is the listener's
  decision — the speaker does go dark on its own once every program has let go, so this is a
  fallback rather than the mechanism. See the prose below for what it does not cover,
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
the four, a `volume`, `volume_max`, `start_volume_max`, `startup_silence` or `buffer_extra`
that is not a number in range, and a `helper` path that does not exist all say so in the
console. `volume_max` was the last one still falling back in silence while this paragraph
already claimed otherwise. So do settings written as
`#key=value`: Windows comments start with `;`, so those are key names rather than disabled
lines and nothing reads them. Boolean settings accept `0/1`, `false/true`, `no/yes` and `off/on`; other non-empty
values fall back to off and are reported.

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
skipped> sleep=<off|Ns|skipped|unreachable|rejected> holding=<count>`. `sleep=off` means
nothing was configured, `skipped` that there was nothing left to arm after, `unreachable`
that the command could not be sent and `rejected` that the speaker refused it. Before this
line there was nothing at all in the console
at the end of a stream, which is why the morning after a speaker that stayed lit there was
nothing to read. All three fields appear on every path, including one that failed before the
watcher existed.

`stop=skipped` also covers an offer the speaker **refused**. A matched rejection means it
never took the URL, so it is still doing whatever it was doing before, and a release would
reach past this helper and pause that instead.

`holding` counts this helper's own sockets too. What it skips is narrower: its own sockets
that are *already closing*. Measured on 2026-08-15, a locally closed socket sits in
`FIN_WAIT` for a further 0.5 s to 1.5 s, and the count is taken right after teardown closes
its own — so waiting those out cost that long on every helper exit, which precedes every
seek, to report this teardown as something holding the speaker. A socket this helper left
`ESTABLISHED` or `CLOSE_WAIT` is a different thing entirely and still counts; skipping those
would make `holding=0` mean "nobody checked", which is the failure this reading exists to
prevent. A killed session's sockets are untouched by any of this — their owner is gone, so
its PID cannot match.

`sleep_after_stop` arms `SetSleepTimer` once the stream ends, in seconds, `0` and off by
default. It is the only lever this firmware answers *on demand*, and it is opt-in because
powering the speaker down is the listener's decision.

In normal use it is not needed at all. Since the stream path tells the speaker the stream is
over, a released speaker reaches its own idle standby unaided — measured at 17 min 4 s. The
timer is worth arming when something might not let go, not as the way the speaker goes dark.

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

The same `CONTROL_PORT` connection also carries `volume <n>`, `pause`/`resume` and, since the
release/discard work, two more one-word commands: `release` (a real stop - the component sends
this before it starts killing the helper, so `PlaybackWatcher.release()` runs immediately over
the connection already open rather than waiting on the helper's own exit, which an encoder that
never exits could otherwise delay) and `discard` (a replacement helper is about to take over,
e.g. a seek or format change - the same release minus arming a sleep timer, since the
replacement will keep the speaker awake on its own). All five are best effort: a failed or
unknown command is logged and the connection stays open, because the stream matters more than
any one of them.

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
The state a user recognises as the speaker sleeping now arrives on its own: since PR #48 the
stream path tells the speaker the stream is over, and a released speaker goes dark after its
own idle interval - measured at 17 min 4 s. `SetSleepTimer`, exposed as `sleep_after_stop`,
reaches that state *on demand* and is a fallback rather than the mechanism. Renaming this menu
item or pointing it at the same timer is still open work. See the standby section of
`docs/WAM_PROTOCOL.md`.

Left alone the speaker reaches standby by itself in under 17 minutes (measured 2026-08-16),
so the menu item is a convenience, not the only route. Earlier notes in this repo claimed the
firmware never sleeps unaided; that was wrong.

What standby does now guarantee is that nothing local is still attached. After the stop and
the mute it waits up to `STANDBY_RELEASE_TIMEOUT` for established TCP connections to the
speaker to drop, and reports `holding=<count>`, or `holding=unknown` when the socket table
could not be read. What the action excludes is narrower than its own process: only sockets it
owns that are *already closing* — `FIN_WAIT1`, `FIN_WAIT2`, `CLOSING`, `LAST_ACK`. Its stop,
mute and verification each opened one, and waiting out that lingering kernel bookkeeping would
report the action's own finished requests as a hold. A socket the action still holds open
counts like anyone else's, deliberately: excluding the whole process would hide this
component's own leaks behind a reassuring `holding=0`. A remaining hold adds a `warning=` line
rather than failing the action: the mute and
the stop did land, and the caller may have asked while something else was streaming.

This is not the explanation for a speaker that stays lit, and it should not be read as one.
The M5 was still on the morning after the whole computer had been shut down, and a
powered-off host holds no sockets. Whatever keeps the speaker awake outlives its peer, which
puts it in the speaker's own state — the `SetUrlPlayback` session nothing ever ended. The
reading is worth having as proof that this end let go, not as the lead.

One thing `holding=` will *not* show you is a respawn storm. Killing a helper while foobar is
still playing makes the component relaunch it immediately - measured at 78 restarts in about
two minutes, with no backoff - and every dead session leaves a socket behind, 29 of them in
`TIME_WAIT` afterwards. Those are excluded from the count by design, so `holding=` stays low
while the socket table fills up. Read the table itself if you suspect a storm.

`holding=unknown` is deliberately not `holding=0`. Reporting a speaker as released when it
was never checked is the failure this exists to prevent.

Do not restore the old `cp` warning. `cp` is normal for `SetUrlPlayback`; it is not evidence
that emergency stop should request a speaker power cycle.

## Preferences page

`Preferences -> Playback -> Output -> WAM Bridge` edits the same INI keys the output already
reads: device, format, startup volume, hardware slider routing, volume ceiling, safe start
cap, startup silence, extra buffer, sleep-after-stop, diagnostics and the optional PCM helper
override. The page uses foobar2000's core dark-mode hooks. Runtime and UI share one settings
parser, so their ranges cannot drift; startup volume is the M5's raw `0..30` scale in both.
Reset restores component defaults, and default-valued settings are removed rather than
written redundantly. Invalid known values in an existing INI mark the page changed so Apply
normalizes them. If any `WAMBRIDGE_*` override is active the page says so, because those
values continue to win over the INI and are not modified by Apply.

Discovery, a connection test and model/firmware reporting remain future UI work.

## Later UI

Radio/TuneIn controls and a dockable panel should reuse the same dispatcher and settings.
Native TuneIn can continue without the PC; proxied stations require the PC and local HTTP
bridge. Label that difference clearly.

## Merge gate

No output transport candidate is release-ready until the physical M5 completes a 3-5 minute
track at wall-clock speed with stable seekbar, a second track, pause/resume, stop/change and
clean helper/FFmpeg shutdown.
