# Development status

Last reviewed: 2026-08-02.

Continuity note for playback work. Read this with `WAM_PROTOCOL.md` before reviving an old
branch or implementing another timing layer.

## Stable on `main`

- SSDP discovery with subnet fallback and saved devices resolved by stable device ID.
- Status, raw volume, mute, pause, play, stop and standby control.
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

## Next order

1. Make the speaker-facing format configurable, then measure whether the speaker prebuffers
   bytes or seconds. It decides whether the 13 s delay has a knob at all.
2. Route the foobar volume slider to the speaker's own volume instead of the host gain,
   with send throttling for slider drags. This subsumes fixing raw M5 volume to `0..30`.
3. Measure pause the same way the volume delay was measured.
3. Reduce and reimplement the finite share path from its measured working form.
4. Add a proper foobar preferences page while retaining legacy INI compatibility.
5. Add TuneIn/radio UI and a dockable panel only after output transport is stable.

## Rules for continuing

- Check `main`, open PRs and recent commits first.
- One logical stage per PR; no unrelated refactors or long changelogs.
- Do not merge transport work without the physical M5 checklist.
- Do not reopen AVTransport without new device evidence.
- Do not reintroduce `-re`, socket throttling, fake `Content-Length`, competing 55001
  listeners or multiple FFmpeg readers for one PCM stdin.
- Keep raw test volume at step `3` or lower.
