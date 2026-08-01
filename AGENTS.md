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
- `SetUrlPlayback` did not emit `StartPlaybackEvent` during measured working PCM streams.
  Do not gate that path on the event until a different reliable signal is proved on the
  physical M5.
- A command timeout is not a failure. The firmware answers late, and often with an
  unrelated event, so replies must be matched against the command that was sent.
- Check submode before local playback. In `cp` the speaker fetches the object and stays
  silent; nothing but a power cycle clears it. Do not assume why it entered `cp` without a
  measured command trace.
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
