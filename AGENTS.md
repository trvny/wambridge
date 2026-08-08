# AGENTS.md

Read `docs/WAM_PROTOCOL.md` and `docs/DEVELOPMENT_STATUS.md` before changing
playback code. Measured facts override old comments, issue summaries and
plausible-looking assumptions.

## Testing

- On Windows use `PYTHONPATH=src py -m unittest discover -s tests -q`.
- CI enforces Ruff; it is not assumed to be installed locally.
- Never mock `Popen().stdout` with a bare `MagicMock`. Use a real `BytesIO`.
- `console::printf` is pfc's formatter, not the CRT one. It prints the length modifiers in
  `%lu` and `%llu` literally and drops the value, which left the PCM write probe logging
  `requested=lu written=lu` for its whole life. Use `%u` and `%s` only.
- A timed-out background process must actually be terminated. The physical test machine
  has 8 GB of RAM and has already been taken down by runaway FFmpeg processes.

## Playback paths are different

- **Share/DLNA:** `StartPlaybackEvent` is the trustworthy playback confirmation.
  `MusicInfo` and `PlayStatus` can report stale or impossible state.
- **URL/PCM:** do not hard-gate on `StartPlaybackEvent`. Repeated audibly playing M5 runs
  reached `WAMBRIDGE AUDIO_STARTED` but emitted no matching start event before the old
  45-second timeout. Keep listening and correlate the event when it appears, but do not
  abort a working URL stream because it is absent.
- `WAMBRIDGE AUDIO_STARTED` means encoded bytes entered the HTTP response. It is the
  earliest measured transport-clock anchor that avoids deadlock, not proof that sound is
  audible.
- `cp` is the normal submode for `SetUrlPlayback`. Do not gate URL playback on submode and
  do not tell anyone to power-cycle the speaker. Whether `cp` affects share playback is a
  separate, unmeasured question.

## Control and stream ownership

- During PCM playback use one persistent TCP `55001` connection for commands and events.
  A separate probe or listener competes with `pcm_cli` and can make playback time out.
- One FFmpeg owns the PCM stdin. The M5 opens another HTTP request while the first is still
  live. Serve the first request and refuse later requests. Retiring the first encoder kills
  the working stream; running two encoders splits the PCM between them.
- Command timeouts are not automatically failures. Match responses and events to the
  command or stream attempt. Unmatched `ErrorEvent` values are diagnostics.
- Never hand remote URLs directly to `SetUrlPlayback`. Proxy through FFmpeg and the local
  HTTP server.

## Output clock

- TCP backpressure paces the speaker-facing HTTP stream. Do not add FFmpeg `-re`, socket
  throttling or a second speaker-facing timer.
- Backpressure does not pace foobar's accounting. The output must count queued PCM,
  pipe writes in progress and submitted-but-not-yet-played frames as latency.
- Start one cumulative real-time clock at `AUDIO_STARTED`. Pause may shift its anchor;
  pipe writes must never re-anchor it to `now`. Cap played frames with the amount actually
  submitted instead.
- **`process_samples` must accept every frame it is offered.** It returns void, so a
  partial write cannot be reported and the caller counts the whole chunk as played. Taking
  `min(free, chunk)` and dropping the rest made foobar run a 220 s track out in 22 s while
  the pipe stayed at 1.0x. Block until there is room; stop waiting only when the stream is
  shutting down, flushing or replaced. `process_samples_v2` reports partial writes instead.
- Audio is delayed by **about 13 s** end to end, measured mid-stream on the M5 in 2026-08.
  `get_latency()` reports about 4 s, so it under-reports by some nine seconds, and roughly
  7-8 s of the total sits inside the speaker. Do not treat host buffering as the whole
  latency, and do not expect a smaller host buffer to make controls responsive.
- Anything applied where PCM leaves the queue — the volume gain, for one — reaches the ear
  13 s later. Controls that must feel immediate belong on the `55001` control path, which
  answers in about a second.
- Foobar's `f32le -> FLAC -> M5` path passed the full physical checklist in 2026-08: a
  complete track at a median 1.00x, stable seekbar, second track, pause/resume, seek,
  stop/change, radio HLS across a 44.1 to 48 kHz switch, and clean process shutdown.

## Physical M5 validation

- Start at raw volume step `3` or lower.
- Identify artifacts by commit SHA or workflow run. ZIP timestamps are UTC while installed
  file times on the test machine are local UTC+2.
- Use beefweb for foobar position, the process tree for FFmpeg leaks and
  `Get-NetTCPConnection` for abandoned sockets. `ReadTransferCount` and the tested Windows
  process I/O counters do not work on this machine.
- beefweb reports foobar's transport, not this component's path. A run can show `playing`,
  a median 1.00x and a rising position while the audio leaves through the sound card,
  because the selected output was not `WAM Bridge`. Before trusting any run, confirm the
  helper and FFmpeg are actually up — `wambridge-pcm` appears about six seconds in and
  FFmpeg about nine — and that a connection to the speaker is established. A rate of 1.00
  is not evidence that a single byte reached the M5.
- Do not use `wambridge-control status` as a liveness check. `get_status` calls
  `GetPowerStatus`, which does not exist on this firmware, so the action always fails with
  `Cannot reach Samsung WAM: timed out` and a healthy speaker looks unreachable. Probe with
  `GetSpkName` instead.
- Turn on `diagnostics=1` before claiming anything about pacing. Sampling beefweb once a
  second gives the rate foobar believes in; the `CLOCK` line gives every term behind it.
  One without the other is how a dropped chunk passed for a clock bug for two days.
- To measure how late the sound is, change something audible at a recorded playback
  position and have the listener read the seekbar when they hear it. The difference between
  the two positions is the delay and it does not depend on anyone's reaction time in a chat.
- Do not merge transport changes before a complete track, stable seekbar, second track,
  pause/resume, stop/change and clean process shutdown pass on the physical M5.

## Repository workflow

- Check current `main`, open PRs and recent commits before starting.
- Keep one logical change per PR. Truly small low-risk edits may go directly to `main`.
- Codex may review and advise, but do not ask or rely on it to commit or push. A change exists
  only when its SHA is visible in the repository.
- Wait for CI and automatic review. Resolve actionable threads before merging; use squash
  merge unless the change is intentionally direct-to-main.
- `docs/WAM_PROTOCOL.md` stores measured protocol facts. `docs/DEVELOPMENT_STATUS.md`
  stores implementation and PR status. Correct both when a measurement invalidates an
  assumption.
