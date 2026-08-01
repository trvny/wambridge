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
- On the URL/PCM path, `audio_started` means only that the first encoded bytes reached the
  HTTP response; it is not proof of audible playback. The current experiment arms the event
  listener immediately before `SetUrlPlayback` and accepts `StartPlaybackEvent` from either
  the stable client UUID or `user_identifier=public`. An earlier claim that this event never
  arrives was based only on failed runs with event logging and was retracted. Keep the gate
  until the public-filtered build is tested on the physical M5; do not silently fall back to
  `audio_started`.
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

## Changing protocol code

- Prove claims on the physical M5 first. Run a known-good path as a control so a network
  fault cannot be mistaken for a protocol fault.
- `docs/WAM_PROTOCOL.md` holds measured facts; `docs/DEVELOPMENT_STATUS.md` holds pull
  request and approach status. When a measurement invalidates an assumption, correct both
  instead of leaving the old claim in place.

## Branches

- Base on `main`. A pull request based on another pull request's branch cannot be merged
  normally and needs the `pulls/{n}/merge-async` endpoint.
