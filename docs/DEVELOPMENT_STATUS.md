# Development status

Last reviewed: 2026-08-01.

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

## Active pull request

### PR #21: synchronize the foobar output clock

Branch: `fix/foobar-output-clock`

Latest documented candidate: `0db3742`, green in Build #227.

The branch:

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

Do not merge yet. The current build still needs one complete 3-5 minute track against wall
clock, stable seekbar, second track, pause/resume, stop/change and process cleanup on the
physical M5.

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

Status: foundation, with foobar clock work still experimental.

- Works for files, radio and endless sources.
- Uses local HTTP without fake `Content-Length`.
- Uses one control connection and one encoder.
- Relies on speaker TCP backpressure for HTTP pacing.
- Requires separate bounded host accounting for accepted-but-not-heard PCM.

### Finite share/DLNA transport

Status: protocol proven, product integration deferred.

Use it later for local files that benefit from native duration, pause and seek. It cannot be
the universal foundation because it does not cover endless sources.

### Generic UPnP AVTransport

Status: rejected for the tested `SPK-WAM550`. The service is not exposed.

## Next order

1. Finish the physical acceptance run for PR #21.
2. Fix raw M5 volume handling to `0..30` or add model-aware percentage conversion.
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
