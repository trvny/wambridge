# AGENTS.md

Guidance for assistants working in this repository. Read this file together with
`docs/WAM_PROTOCOL.md` and `docs/DEVELOPMENT_STATUS.md` before changing playback code.
Measured facts override old comments, issue summaries and plausible-looking assumptions.

## Testing

- On Windows use `PYTHONPATH=src py -m unittest discover -s tests -q`.
- CI enforces Ruff; it is not assumed to be installed locally.
- Never mock `Popen().stdout` with a bare `MagicMock`. Use a real `BytesIO`.
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
- A measured `+21..23 s` figure included discovery and startup. It is an upper bound, not a
  speaker cushion and not a target for `get_latency()`.
- Foobar's `f32le -> FLAC -> M5` path is confirmed to produce audible output. A complete
  normal-speed 3-5 minute track is still the acceptance gate.

## Physical M5 validation

- Start at raw volume step `3` or lower.
- Identify artifacts by commit SHA or workflow run. ZIP timestamps are UTC while installed
  file times on the test machine are local UTC+2.
- Use beefweb for foobar position, the process tree for FFmpeg leaks and
  `Get-NetTCPConnection` for abandoned sockets. `ReadTransferCount` and the tested Windows
  process I/O counters do not work on this machine.
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
