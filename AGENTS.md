# AGENTS.md

For playback or protocol work, read `docs/WAM_PROTOCOL.md` and
`docs/DEVELOPMENT_STATUS.md` first. They are living lab notes from repeated tests
on the physical M5, including failed approaches and measurements that have
superseded earlier assumptions. Prefer the newest measured result over an older
plausible explanation. This file keeps only easy-to-miss traps.

## Wambridge

### Expensive traps

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
- This firmware answers unimplemented commands with silence, not a refusal, so
  every such call costs a full timeout. Measured on the M5 against a speaker
  answering everything else in 0.02-0.2 s: `GetPowerStatus`, `GetLedStatus`,
  `GetStandbyMode`, `GetFeature`, `GetPowerSaving`, `GetAutoPowerDown` and
  `GetSpkStatus` (that last one added 2026-08-15, same run) all time out every
  time. Never let one of them decide whether a reading succeeded: `get_status`
  used to, and reported a healthy speaker as unreachable.
- For M5 liveness, still use `GetSpkName` rather than `wambridge-control
  status`. The status action no longer fails on a healthy speaker, but it makes
  four round trips - five in `cp`, the normal idle submode, which adds
  `GetRadioInfo` - and its `timeout` is per command rather than a total, so an
  unreachable speaker takes several times that to say so. One command that
  answers in 0.14 s is the better test.
- Terminate timed-out child processes. Runaway FFmpeg processes have already
  exhausted the 8 GB physical test machine.
- **Reporting a failure throws `exception_output_invalidated`, and foobar
  answers that by building a brand new output object.** Anything meant to bound
  retries therefore cannot live in that object: it is constructed fresh for
  every attempt. Measured 2026-08-16, before the budget moved to file scope: 77
  helper restarts in 90 seconds, continuing on its own afterwards at one death
  every 25 seconds, each leaving a socket in `TIME_WAIT` to port 55001 until at
  29 of them the speaker stopped answering commands at all. A helper that
  reached `PLAYING` and then exited is a different case - that is the speaker
  ending a stream, and restarting at once is the recovery that works.

### Physical M5

Start hardware tests at raw volume step `3` or lower. Transport changes are
merge-ready after a complete track, stable seekbar, second track, pause/resume,
stop/change, and clean process shutdown pass on the physical M5.

## Code review rules

- Do not comment on README-only, documentation-only, changelog, formatting, or cosmetic changes unless they introduce a factual error or break generated/validated content.
