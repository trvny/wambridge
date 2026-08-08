# Development status

Last reviewed: 2026-08-02.

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

## Measured: about thirteen seconds of audio delay

Measured 2026-08-02 on the physical M5, mid-stream, with playback already settled. The
foobar volume slider was moved through beefweb while the playback position at the moment
of the change was recorded; the listener read the position off the seekbar when the change
was heard. Two events in one run: 12.14 s heard at 26 s, and 37.16 s heard at 50 s.

**End-to-end delay is about 13.4 s**, spread one second. Reaction time inflates it by
roughly half a second.

`get_latency()` reports about 4 s. It under-reports by some nine seconds, and the missing
part is downstream of anything the host counts:

| term | share | ours to change |
|---|---|---|
| host `buffered` | ~3.9 s | floored at 4.0 s by `clamp(bufferLength, 2.0, 30.0)` plus 2.0 |
| `adelay=1500` startup silence | 1.5 s | yes |
| FFmpeg and the HTTP socket | under a second | barely |
| the speaker itself | ~7-8 s | no |

Consequences, none of them optional to know:

- Lowering the host buffer floor buys 2-3 s of thirteen. It is not the fix for anything.
- The volume slider applies a gain where PCM leaves the queue, and `queued` is 0-61 ms.
  Everything else is already past that point, so the slider cannot be responsive by
  construction. Route it to the speaker's own volume, which answers in about 1.3 s.
- Pause writes silence into the same pipe, so it very likely has the same delay. Not
  measured yet.
- Whether the speaker prebuffers bytes or seconds is unknown. If bytes, a lower bitrate
  shortens everything proportionally, and the `mp3` profile at 320 kbps against FLAC's
  700-900 kbps is a cheap way to find out.

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
| Does a lower bitrate shorten the delay | **No, it lengthens it.** MP3 320k = 16.9 s against FLAC's 13.4 s | `WAM_PROTOCOL.md` |
| Is `cp` submode a fault | No. It is the normal submode for `SetUrlPlayback` and also the idle one | `WAM_PROTOCOL.md` |
| Does the SDK offer a hardware volume interface | `output_entry_v2::get_volume_control` exists but no public component implements it; not a foundation to build on | PR #30 |
| Does `flag_needs_shims` affect volume | No. It means regular `update()` calls and end-of-stream padding | SDK `output.h` |
| Can a command clear `cp` | Not observed. `SetPlaybackControl stop` is accepted and does not clear it | `WAM_PROTOCOL.md` |
| Does the M5 auto-power-down | No configurable one exists. `SetSleepTimer` in **seconds** is the only lever | `WAM_PROTOCOL.md` |

## Open, in the order that makes sense

1. **Physical checklist for PR #30** (routed volume slider). It changes `volume_set` and the
   helper's startup volume, so the full gate applies before merging.
2. **Measure the delay again with `startup_silence=0`.** Expected saving is about 1 s, not
   1.5: with the silence gone `AUDIO_STARTED` arrives about six pipe writes later, because
   the prepended silence used to supply the first FLAC audio frame immediately. That is
   close to the method's own spread, so it needs four or five volume changes in one run,
   not two. **`hardware_volume` must be off for this** — with the slider routed to `55001`
   it no longer travels the delayed path, and the measuring instrument is gone.
3. **Try a fatter stream, not a thinner one.** The speaker's prebuffer is partly bounded by
   bytes, so raw PCM at roughly twice FLAC's bitrate should hold fewer seconds. This is the
   one remaining idea with a plausible several-second payoff. Needs a `wav` output profile.
4. **Route pause onto `55001`** (`SetPlaybackControl pause`/`resume`), then stop, seek and
   skip. Same shape as the volume fix. Measure pause first, or there is no baseline. Risk to
   watch: whether a paused speaker stops pulling and the HTTP connection times out.
5. Log unknown INI keys to the console. Keys from an unbuilt branch are silently ignored
   today, so the file does not tell anyone what is actually active.
6. Rename or rewire the misnamed standby menu item; see `FOOBAR_PLUGIN.md`. Standby now
   reports `holding=<count>` for connections still attached to the speaker, but it still
   sends no power command, so the name remains wrong until it arms a sleep timer.
7. Reduce and reimplement the finite share path from its measured working form.
8. Add a proper foobar preferences page while retaining legacy INI compatibility.
9. Add TuneIn/radio UI and a dockable panel only after output transport is stable.

## What the 7-8 s speaker figure actually is

A subtraction remainder, not a measurement: total delay minus the terms that can be counted.
It therefore absorbs every error in the others. The owner reports Samsung's own PC and
Android clients felt slower than instant but clearly faster than 13 s on this same speaker,
which is circumstantial evidence that part of that remainder belongs to how this project
feeds the speaker. Treat it as an upper bound on the untouchable part.

## Rules for continuing

- Check `main`, open PRs and recent commits first.
- One logical stage per PR; no unrelated refactors or long changelogs.
- Do not merge transport work without the physical M5 checklist.
- Do not reopen AVTransport without new device evidence.
- Do not reintroduce `-re`, socket throttling, fake `Content-Length`, competing 55001
  listeners or multiple FFmpeg readers for one PCM stdin.
- Keep raw test volume at step `3` or lower.
