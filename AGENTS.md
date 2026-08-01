# AGENTS.md

Guidance for AI assistants working in this repository. The Samsung WAM protocol has several
traps that look like bugs in this code but are not.

## Testing

- `PYTHONPATH=src py -m unittest discover -s tests -q` — no pytest here; use `py`, not `python`
- ruff is not installed locally; CI enforces it
- Never mock `Popen().stdout` with a bare `MagicMock`. Reads return another truthy
  `MagicMock` that adds no bytes, `unittest.mock` records every call, and the loop grows
  memory until the machine dies. Substitute a real `BytesIO`.
- A background task that outlives its timeout must actually be killed, not just declared
  killed. This machine has 8 GB of RAM and no headroom for a runaway process.

## Trusting the speaker

- On the share/DLNA path, only `StartPlaybackEvent` confirms playback. `MusicInfo` and
  `PlayStatus` report a playing state with nothing playing, and mix in fields from earlier
  sessions.
- On the URL/PCM path, do not block or abort playback waiting for `StartPlaybackEvent`.
  Repeated physical-M5 runs were audibly playing after `audio_started` but emitted no matching
  start event before the 45 s timeout. `audio_started` still means only that encoded bytes
  reached the HTTP response, so use it to start the bounded real-time transport clock, not as
  a claim of audible confirmation. Keep the event listener for diagnostics when the firmware
  does emit a start event.
- A command timeout is not a failure. The firmware answers late, and often with an
  unrelated event, so replies must be matched against the command that was sent.
- Do not gate `SetUrlPlayback` on submode, and never tell anyone to power cycle the speaker.
  `cp` is the normal submode for that path: the speaker switches into it as the command runs
  and plays for as long as you feed it. Measured by sampling submode every 2 s against a
  100 s run that held 1.00x throughput throughout, all of it in `cp`, and confirmed audible
  by the owner. Whether `cp` blocks the share path is a separate, unmeasured question — do
  not merge the two.
- Never send remote URLs to `SetUrlPlayback`: HTTPS errors out, Ogg is silent and HLS
  wedges the control port. Proxy through FFmpeg and the local HTTP server instead.

## Measuring against the physical M5

- **One encoder owns the PCM input.** Every FFmpeg started for a stream request inherits the
  same stdin, so two of them split one stream. The speaker issues a second stream request
  almost immediately while the first is still the live one, so serve the first and refuse the
  rest. Retiring the older one instead kills the stream being served and starves its
  replacement — that mistake was made and measured.
- **Do not open a second connection to 55001 while `pcm_cli` runs.** A separate listener
  (`wamtap sniff`, or a probe of your own) competes with it and the player fails with
  `Cannot reach Samsung WAM: timed out`. Commands and events belong on one connection.
- **Instruments that do not work here**: `(Get-Process x).ReadTransferCount` returns `null`,
  and `Get-Counter "\Process(...)\IO ... Bytes/sec"` fails with `c0000bb8`. A script built on
  either reports flat zeros that look like a stalled process. Use the beefweb API
  (`127.0.0.1:8880/api/player`) for playback position, the process tree for FFmpeg leaks, and
  `Get-NetTCPConnection` for abandoned speaker sockets.
- **Timestamps inside a `.fb2k-component` are UTC; file times on disk are local (UTC+2).**
  Comparing them without the shift makes a current build look two hours stale. To identify
  what is installed, add two hours to the DLL time and match it against `gh run list`.
- **foobar delivers `f32le`**, while the script path was only ever proven on `s16le`. The
  `f32le` → FLAC path has not been verified on hardware; do not assume it behaves the same.

## Changing protocol code

- Prove claims on the physical M5 first. Run a known-good path as a control so a network
  fault cannot be mistaken for a protocol fault.
- `docs/WAM_PROTOCOL.md` holds measured facts; `docs/DEVELOPMENT_STATUS.md` holds pull
  request and approach status. When a measurement invalidates an assumption, correct both
  instead of leaving the old claim in place.

## Branches

- Base on `main`. A pull request based on another pull request's branch cannot be merged
  normally and needs the `pulls/{n}/merge-async` endpoint.
