# AGENTS.md

Read `docs/WAM_PROTOCOL.md` and `docs/DEVELOPMENT_STATUS.md` before changing
playback code. Measured facts take priority over old comments, issue summaries,
and plausible-looking assumptions.

## Testing

- On Windows use `PYTHONPATH=src py -m unittest discover -s tests -q`.
- CI enforces Ruff; it is not assumed to be installed locally.
- Model `Popen().stdout` with a real `BytesIO` in tests; a bare `MagicMock` does
  not behave like the byte stream this code consumes.
- `console::printf` is pfc's formatter, not the CRT one. It prints the length
  modifiers in `%lu` and `%llu` literally and drops the value, which previously
  left the PCM write probe logging `requested=lu written=lu`. Use `%u` and `%s`.
- Terminate timed-out background processes. The physical test machine has 8 GB
  of RAM and has already been taken down by runaway FFmpeg processes.

## Playback paths are different

- **Share/DLNA:** `StartPlaybackEvent` is the trustworthy playback confirmation.
  `MusicInfo` and `PlayStatus` can report stale or impossible state.
- **URL/PCM:** treat `StartPlaybackEvent` as corroboration rather than a gate.
  Repeated audibly playing M5 runs reached `WAMBRIDGE AUDIO_STARTED` without a
  matching start event before the old 45-second timeout. Keep listening and
  correlate the event if it arrives without aborting an otherwise working URL
  stream.
- `WAMBRIDGE AUDIO_STARTED` means encoded bytes entered the HTTP response. It is
  the earliest measured transport-clock anchor that avoids deadlock, not proof
  that sound is audible.
- `cp` is the normal submode for `SetUrlPlayback`; it is not a useful URL
  playback gate. Leave power cycling out of normal troubleshooting unless new
  evidence points there. Whether `cp` affects share playback is a separate,
  unmeasured question.

## Control and stream ownership

- During PCM playback keep one persistent TCP `55001` connection for commands
  and events. A separate probe or listener competes with `pcm_cli` and can make
  playback time out.
- One FFmpeg owns the PCM stdin. The M5 opens another HTTP request while the
  first is still live; serving the first request and refusing later ones keeps
  the working stream intact. Retiring the first encoder kills it, while two
  encoders split the PCM between them.
- Command timeouts are not automatically failures. Match responses and events
  to the command or stream attempt; unmatched `ErrorEvent` values are
  diagnostics.
- Route remote URLs through FFmpeg and the local HTTP server before
  `SetUrlPlayback`.

## Output clock

- Let TCP backpressure be the speaker-facing pacing mechanism. Extra FFmpeg
  `-re`, socket throttling, or a second speaker-facing timer would pace the same
  path again.
- Foobar's latency accounting includes queued PCM, pipe writes in progress, and
  submitted-but-not-yet-played frames; TCP backpressure alone does not pace that
  accounting.
- Keep one cumulative real-time clock anchored at `AUDIO_STARTED`. Pause may
  shift the anchor; pipe writes should not re-anchor it to `now`. Cap played
  frames with the amount actually submitted instead.
- **`process_samples` must accept every frame it is offered.** It returns void,
  so a partial write cannot be reported and the caller counts the whole chunk
  as played. Taking `min(free, chunk)` and dropping the rest made foobar run a
  220 s track out in 22 s while the pipe stayed at 1.0x. Block until there is
  room; stop waiting only when the stream is shutting down, flushing, or
  replaced. `process_samples_v2` reports partial writes instead.
- Audio is delayed by **about 13 s** end to end, measured mid-stream on the M5 in
  2026-08. `get_latency()` reports about 4 s, so it under-reports by some nine
  seconds, and roughly 7-8 s of the total sits inside the speaker. Host
  buffering is therefore only part of the latency.
- Anything applied where PCM leaves the queue, including the volume gain,
  reaches the ear about 13 s later. Controls that need to feel immediate belong
  on the `55001` control path, which answers in about a second.
- Foobar's `f32le -> FLAC -> M5` path passed the full physical checklist in
  2026-08: a complete track at a median 1.00x, stable seekbar, second track,
  pause/resume, seek, stop/change, radio HLS across a 44.1 to 48 kHz switch, and
  clean process shutdown.

## Physical M5 validation

- Start at raw volume step `3` or lower.
- Identify artifacts by commit SHA or workflow run. ZIP timestamps are UTC while
  installed file times on the test machine are local UTC+2.
- Use beefweb for foobar position, the process tree for FFmpeg leaks, and
  `Get-NetTCPConnection` for abandoned sockets. `ReadTransferCount` and the
  tested Windows process I/O counters do not work on this machine.
- beefweb reports foobar's transport, not this component's path. A run can show
  `playing`, a median 1.00x, and a rising position while audio leaves through the
  sound card because the selected output was not `WAM Bridge`. Before trusting
  a run, confirm the helper and FFmpeg are actually up — `wambridge-pcm` appears
  about six seconds in and FFmpeg about nine — and that a speaker connection is
  established. A rate of 1.00 is not evidence that bytes reached the M5.
- Use `GetSpkName` rather than `wambridge-control status` for liveness. On this
  firmware `get_status` calls nonexistent `GetPowerStatus`, so the status action
  times out even when the speaker is healthy.
- Turn on `diagnostics=1` before drawing conclusions about pacing. Sampling
  beefweb once a second gives the rate foobar believes in; the `CLOCK` line gives
  every term behind it. Using only one of those signals previously made a
  dropped chunk look like a clock bug.
- To measure audible delay, change something audible at a recorded playback
  position and have the listener read the seekbar when they hear it. The
  position difference is the delay and does not depend on chat reaction time.
- Transport changes are merge-ready only after a complete track, stable seekbar,
  second track, pause/resume, stop/change, and clean process shutdown pass on the
  physical M5.

## Repository workflow

- When work could overlap ongoing changes, check `main`, open pull requests, and
  recent commits first.
- When available, use `gptomek[bot]` for commits, comments, review replies, and
  reactions. Open pull requests as `trvny` so external automatic reviews are
  triggered.
- Prefer one logical change per pull request. Truly small low-risk edits can go
  directly to `main`.
- Let automatic Codex review handle review when available; treat its findings as
  advisory and apply useful ones directly.
- Merge after relevant CI and automatic review are clean and actionable threads
  are resolved. Prefer squash merge for pull requests.
- `docs/WAM_PROTOCOL.md` stores measured protocol facts;
  `docs/DEVELOPMENT_STATUS.md` stores implementation and PR status. When a new
  measurement invalidates an assumption, update the relevant records with it.
