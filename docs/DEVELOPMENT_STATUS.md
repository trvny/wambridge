# Development status

Last reviewed: 2026-08-08.

Continuity note for playback work. Read this with `WAM_PROTOCOL.md` before reviving an old
branch or implementing another timing layer.

## Stable on `main`

- SSDP discovery with subnet fallback and saved devices resolved by stable device ID.
- Raw volume, mute, pause, play, stop and standby control. The `status` action is broken on
  this firmware: `get_status` calls `GetPowerStatus`, which does not exist here, so it
  always times out and reports a healthy speaker as unreachable.
- Direct `SetUrlPlayback`, custom radio stations and native TuneIn preset playback.
- Windows builds for bundled helpers and foobar2000 2.x x64.
- Restricted helper handle inheritance from merged PR #2.
- Persistent event listener from merged PR #9.
- `Playback -> WAM Bridge` submenu with emergency stop, standby and physical volume actions
  from merged PR #23.
- PCM HTTP server keeps the first FFmpeg and refuses duplicate stream requests, so one
  encoder owns stdin.
- `cp` is documented as normal for the URL path; no URL startup gate or power-cycle advice
  may depend on that submode.
- Speaker-facing output profiles `flac` (default), `wav` and `mp3`, selected by `format` in
  the INI or `--format` on the helper. Only `flac` has played a full track on hardware.

The stable universal transport is local HTTP started through `SetUrlPlayback`. The speaker
paces the HTTP side through TCP backpressure. Finite share/DLNA playback is proven as a
separate optional path but is not integrated into the foobar output.

## Merged: the foobar output clock (PR #21, `9b12d44`)

Merged 2026-08-02 after the full physical M5 checklist passed.

### What actually caused the runaway start

Not the clock. `process_samples` returns void, so a partial write cannot be reported and
the caller counts the whole chunk as delivered. The output took `min(free, chunk)` and
dropped the rest, so foobar advanced over audio that was never sent.

The per-second `CLOCK` line settled it in one run: `target` and `played` advanced at
exactly 1000 ms per second, `submitted` at about 1035 ms, `buffered` sat between 3.8 and
4.0 s of a 4.0 s capacity and `free` hovered near 100 ms — while foobar ran a 220 s track
out in 22 s. Every clock term was behaving. About nine tenths of each chunk was going in
the bin.

**Rule that follows: the void `process_samples` must accept every frame it is offered,
blocking until there is room.** Only give up when the stream is shutting down, flushing or
has been replaced. `process_samples_v2` keeps reporting partial writes; that is what its
return value is for.

After the fix, measured over a complete track: median 1.00x, 100% of samples between 0.9x
and 1.1x, natural transition into the next track, no leaked encoder.

Every hypothesis that preceded this — clock anchoring, refresh ordering, capacity —
described terms that measurement showed to be correct. None of them was the fault.

### What else the branch carries

- matched shared-socket responses: a rejected `SetUrlPlayback` fails the attempt and a
  rejected unmute fails startup, so `WAMBRIDGE PLAYING` cannot be printed over a speaker
  that was muted for startup and never unmuted,
- a `CLOCK` counter line behind `diagnostics=1`,
- a write probe that prints its numbers again,

- counts queued, in-progress and submitted PCM in latency and capacity,
- starts one cumulative host clock at `WAMBRIDGE AUDIO_STARTED`,
- shifts that anchor only for pause and never re-anchors it to pipe-write completion,
- caps played frames by submitted frames,
- keeps `force_play()` as a transient drain request,
- keeps one TCP `55001` connection for commands and events,
- leaves `StartPlaybackEvent` diagnostic on URL/PCM instead of a 45-second hard gate,
- mirrors helper logs and errors into the foobar console,
- keeps FFmpeg free of `-re` and refuses duplicate stream encoders.

Physical measurements behind the design:

- gating capacity on `StartPlaybackEvent` filled the minimum four-second buffer and froze
  foobar after four seconds,
- releasing capacity from a clock repeatedly reset to `now` let foobar advance at about 94x,
- short `f32le -> FLAC -> M5` runs produced audible sound,
- the audibly playing URL path did not emit a matching start event before the old timeout,
- `NETWORK_TIMEOUT_ERROR` disappeared after stream starvation was fixed.

## Measured: about six seconds of audio delay

Re-measured 2026-08-08 on the physical M5 from the build of PR #41 (`78da8b2`, installed
and verified 69/69 by hash), with `startup_silence=0`. Method unchanged: the foobar volume
is changed through beefweb, the playback position at the moment of the change is recorded,
and the listener reads the position off the seekbar when the change is heard. Four changes
per run instead of two, over the same passages of the same 19-minute 44.1 kHz source, so
the two formats are compared on identical material.

| passage | FLAC | WAV |
|---|---|---|
| 192 s | 7.94 s | 5.97 s |
| 217 s | 5.91 s | 4.95 s |
| 242 s | 6.90 s | 5.91 s |
| 267 s | 5.91 s | 5.89 s |
| mean | **6.67 s** | **5.68 s** |
| spread | 2.0 s | 1.0 s |

**End-to-end delay is about 6.7 s on FLAC and 5.7 s on WAV.** The listener reads whole
seconds off the seekbar, so a single pair carries about a second of quantisation; the mean
difference of 1.0 s is at that limit and rests on the pattern - four passages out of four
in the same direction, and half the spread - rather than on the average alone.

Pause and resume agree from two other directions, same session: after a pause the sound
stopped about 5 s later, and after resuming from 51.98 s it came back at 58 s, so 6.0 s.
Three methods, one answer.

**The previously recorded 13.4 s is superseded.** It was measured 2026-08-02 on a much
older build with `startup_silence=1500`, from two data points, on unrecorded material.
The silence accounts for 1.5 s of the difference and nothing here accounts for the rest,
so treat the older figure as history rather than as a term to subtract from.

### The `wav` profile passed the physical checklist

Same session, same 19-minute source, sampled at 1 Hz with the process tree and socket table
beside beefweb. Every criterion in `AGENTS.md` measured rather than judged:

| criterion | result |
|---|---|
| track at wall-clock speed | 300 samples, `rate` median **0.999**, min 0.966, max 1.037, **100% within 0.9-1.1x** |
| stable seekbar | no excursion outside that band at any point |
| second track | index 3 to 4 seamless: 1140.69 s to 0.99 s, tempo back at once, **no restart** |
| seek | encoder retired and replaced in **under a second** while the helper's count never dropped; tempo back within ~2 s |
| pause/resume | 18 s paused, both sockets stayed `Established`, helper and FFmpeg alive, resumed from the same position |
| stop | 0 FFmpeg, 0 helper, no `Established` socket left |
| leaks | FFmpeg and helper never above 1; free RAM 2.2-2.4 GB throughout |

One honest gap: nobody was listening during these runs. The transport is proven; the absence
of audible artefacts is not. `flac` therefore stays the default and `wav` stays opt-in until
somebody has listened to a full track on it.

`get_latency()` reports about 4 s. With `startup_silence=0` the host's own share is roughly
that, so most of the remaining two to three seconds sits past anything the host counts:

| term | share | ours to change |
|---|---|---|
| host `buffered` | ~3.9 s | floored at 4.0 s by `clamp(bufferLength, 2.0, 30.0)` plus 2.0 |
| `adelay` startup silence | 0 s as configured, 1.5 s by default | yes |
| FFmpeg and the HTTP socket | under a second | barely |
| the speaker itself | ~2 s on FLAC, less on WAV | partly, through bitrate |

Consequences, none of them optional to know:

- The host buffer is now the **largest single term**, not a rounding error next to the
  speaker. Its 4.0 s floor is worth revisiting, which was not true when the total was 13 s.
- The volume slider applies a gain where PCM leaves the queue, and `queued` is 0-61 ms.
  Everything else is already past that point, so the slider cannot be responsive by
  construction. Route it to the speaker's own volume, which answers in about 1.3 s.
- Pause takes about 5 s to fall silent and resume about 6 s to come back, measured
  2026-08-08. Both are far above the 1.3 s a `55001` command costs, so routing them is still
  worth it - the prize is just five seconds rather than thirteen.
- **The prebuffer is partly bounded by bytes, confirmed twice.** A thinner stream is slower:
  `mp3` at 320 kbps measured 16.9 s against FLAC's 13.4 s on the old build. A fatter one is
  faster: `wav` at a constant 1411 kbps beat FLAC on every passage of the same source and
  halved the spread. The variance is the tell - FLAC's bitrate rides the material, so on an
  uneven mix the delay breathes with it, while WAV's constant rate does not. The gain is
  about a second, not the several the bitrate ratio alone would predict, which agrees with
  the mp3 ratio of 1.26x where 2.4x was expected: something else is bounded too.

## Closed investigations retained as evidence

### PR #4: manual PCM pacing

Closed without merge. It correctly proved that the M5 paces the speaker-facing HTTP stream
through TCP backpressure, but the original conclusion was too broad. Backpressure does not
pace foobar's own output accounting. Do not restore FFmpeg `-re` or HTTP throttling; fix
host latency and capacity instead.

### PR #7: finite share/DLNA playback

Closed without merge after the experiment became too large and its early assumptions were
invalidated. The useful result remains proven:

- `SetSharePlaybackControl` works,
- `device_udn` is the raw registered client UUID,
- media is served at `/DLNA/<objectid>` on port `49200`,
- `StartPlaybackEvent` confirms this path,
- finite playback can expose duration, pause and seek state.

A future implementation should be rebuilt as a small optional layer with one known-good
attempt, not resurrect the old fallback ladder.

## Current conclusions

### Universal URL/PCM transport

Status: validated on the physical M5. Pacing is correct; responsiveness is not.

- Works for files, radio and endless sources.
- Uses local HTTP without fake `Content-Length`.
- Uses one control connection and one encoder.
- Relies on speaker TCP backpressure for HTTP pacing.
- Requires separate bounded host accounting for accepted-but-not-heard PCM.
- Accepts every offered frame in `process_samples`, blocking until it fits. That entry point
  returns void, so a partial write is invisible and the caller counts the dropped remainder as
  played. Measured on the M5 (2026-08-02): foobar advanced 220 s of track in 22 s at a median
  11x while `submitted` grew at 1.04x, `buffered` sat at 3.8-4.0 s of a 4.0 s capacity and
  `free` hovered near 100 ms. The transport was never fast; the surplus was discarded.
- Matches shared-socket responses to the command that was sent. A matched `ng` fails startup
  for `SetUrlPlayback` and for the `SetVolume` that undoes the startup mute; an unanswered
  command still counts as success, and unmatched bodies stay diagnostics.

### Finite share/DLNA transport

Status: protocol proven, product integration deferred.

Use it later for local files that benefit from native duration, pause and seek. It cannot be
the universal foundation because it does not cover endless sources.

### Generic UPnP AVTransport

Status: rejected for the tested `SPK-WAM550`. The service is not exposed.

## Settled, do not re-litigate

Each of these cost real time and each is closed by measurement, not by argument.

| Question | Answer | Where |
|---|---|---|
| What caused the runaway start | `process_samples` returns void; taking `min(free, chunk)` binned ~9/10 of every chunk | this file, above |
| Does a lower bitrate shorten the delay | **No, it lengthens it.** MP3 320k = 16.9 s against FLAC's 13.4 s; and a fatter WAV beat FLAC on every passage | `WAM_PROTOCOL.md` |
| Is `cp` submode a fault | No. It is the normal submode for `SetUrlPlayback` and also the idle one | `WAM_PROTOCOL.md` |
| Does the SDK offer a hardware volume interface | `output_entry_v2::get_volume_control` exists but no public component implements it; not a foundation to build on | PR #30 |
| Does `flag_needs_shims` affect volume | No. It means regular `update()` calls and end-of-stream padding | SDK `output.h` |
| Can a command clear `cp` | Not observed. `SetPlaybackControl stop` is accepted and does not clear it | `WAM_PROTOCOL.md` |
| Does the M5 auto-power-down | No configurable one exists. `SetSleepTimer` in **seconds** is the only lever | `WAM_PROTOCOL.md` |

## Open, in the order that makes sense

1. **Physical checklist for PR #30** (routed volume slider). It changes `volume_set` and the
   helper's startup volume, so the full gate applies before merging.
2. **Find how small the host buffer can get.** It was dismissed as "2-3 s of thirteen" and is
   now the largest single term of six. `clamp(bufferLength, 2.0, 30.0)` plus a pad is a
   choice, not a measurement, and nothing has tested where the pipe starts to starve.
   The pad is now `buffer_extra` in the INI, milliseconds, default `2000` so nothing moves
   until it is measured. Capacity is delay here almost one for one: the queue was measured
   running 3.79-3.99 s full of its 4.0 s capacity. Walk it down - 1500, 1000, 500, 0 - and
   watch for the pipe starving, which shows up as `free` climbing in the `CLOCK` line and
   audible dropouts, not as a lower number. Below `buffer_extra=0` the remaining 2.0 s is
   the clamp floor and needs its own change.
3. **Decide whether `startup_silence` should default to 0.** It has now run at 0 for a whole
   session on hardware, repeatedly reaching `WAMBRIDGE PLAYING`, and it is 1.5 s of pure
   delay on a path of six. The default is still 1500, so every stock installation pays it and
   the measured figures above do not describe one. Nobody ever recorded what the silence was
   for, which is the only reason it is still there.
4. **Route pause onto `55001`** (`SetPlaybackControl pause`/`resume`), then stop, seek and
   skip. Same shape as the volume fix. Baseline measured 2026-08-08: about 5 s to fall
   silent, about 6 s to come back. Risk to watch: whether a paused speaker stops pulling and
   the HTTP connection times out - a 30 s pause did not disturb either socket or restart any
   process.
6. Rename or rewire the misnamed standby menu item; see `FOOBAR_PLUGIN.md`. Standby now
   reports `holding=<count>` for connections still attached to the speaker, but it still
   sends no power command, so the name remains wrong until it arms a sleep timer.
7. Reduce and reimplement the finite share path from its measured working form.
8. Add a proper foobar preferences page while retaining legacy INI compatibility.
9. Add TuneIn/radio UI and a dockable panel only after output transport is stable.

## What the 7-8 s speaker figure was

A subtraction remainder, not a measurement: total delay minus the terms that could be
counted, so it absorbed every error in the others. The 2026-08-08 re-measurement retired it.
With the whole path down to 6.7 s and the host's own buffer accounting for about 3.9 s of
that, there is no seven-second speaker to point at.

The owner's report that Samsung's own PC and Android clients felt clearly faster than 13 s
on this same speaker turned out to be the right instinct: most of the difference was ours,
not the speaker's. Do not reintroduce a large fixed speaker term into any budget without
measuring it directly.

## Rules for continuing

- Check `main`, open PRs and recent commits first.
- One logical stage per PR; no unrelated refactors or long changelogs.
- Do not merge transport work without the physical M5 checklist.
- Do not reopen AVTransport without new device evidence.
- Do not reintroduce `-re`, socket throttling, fake `Content-Length`, competing 55001
  listeners or multiple FFmpeg readers for one PCM stdin.
- Keep raw test volume at step `3` or lower.
