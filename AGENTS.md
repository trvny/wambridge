# AGENTS.md

For playback or protocol work, read `docs/WAM_PROTOCOL.md` and
`docs/DEVELOPMENT_STATUS.md` first. They are living lab notes from repeated tests
on the physical M5, including failed approaches and measurements that have
superseded earlier assumptions. Prefer the newest measured result over an older
plausible explanation. This file keeps only easy-to-miss traps.

## Expensive traps

- **`process_samples` must accept every frame it is offered.** It returns void,
  so dropping a remainder cannot be reported; that bug made a 220 s track finish
  in 22 s while the pipe itself stayed near 1.0x.
- Active PCM playback has one owner of the persistent TCP `55001` connection and
  one FFmpeg owner of PCM stdin. Extra listeners or encoders have previously
  broken or starved a working stream.
- `console::printf` is pfc's formatter, not the CRT one. `%lu` and `%llu` lose
  the value here; use `%u` and `%s`.
- Model `Popen().stdout` with a real `BytesIO` in tests rather than a bare
  `MagicMock`.
- For M5 liveness, use `GetSpkName` rather than `wambridge-control status`.
  This firmware lacks `GetPowerStatus`, so the status action times out on a
  healthy speaker.
- Terminate timed-out child processes. Runaway FFmpeg processes have already
  exhausted the 8 GB physical test machine.

## Physical M5

Start hardware tests at raw volume step `3` or lower. Transport changes are
merge-ready after a complete track, stable seekbar, second track, pause/resume,
stop/change, and clean process shutdown pass on the physical M5.

## GitHub

When available, use `gptomek[bot]` for GitHub side effects, but open pull
requests as `trvny` so automatic reviews run. Prefer one logical change per PR;
trivial low-risk fixes can go directly to `main`.
