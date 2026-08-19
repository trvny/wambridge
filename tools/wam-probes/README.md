# WAM probes

One-shot diagnostic scripts that produced the measured facts in
`docs/WAM_PROTOCOL.md`. They are kept because physical M5 measurements are expensive and
several probes disproved assumptions that had already reached code or documentation.

These are not supported user tools. Comments may be in Polish and the test suite does not
cover them.

## Configuration

| Variable | Meaning | Default |
| --- | --- | --- |
| `WAM_SPEAKER` | speaker address | `192.168.1.50` (the M5 has since moved to another subnet - always pass this) |
| `WAM_HOST` | this machine as seen by the speaker | `192.168.1.10` |
| `WAM_MEDIA` | audio file | `sample.mp3` |
| `WAM_SCRATCH` | generated files | `_scratch/` |

## What the probes settled

| Script | Result |
| --- | --- |
| `probe_share.py` | Share playback requires raw client UUID and `/DLNA/<objectid>` |
| `probe_playertype.py` | `playertype`/`sourcename` do not decide success |
| `probe_dlna_headers.py` | Share playback requires the measured DLNA response shape |
| `probe_confirm_audio.py` | Working share configuration was confirmed by ear |
| `probe_formats.py` | Share path plays FLAC through 96/24 and MP4 with Range |
| `probe_livestream.py` | `SetUrlPlayback` accepts streams with no known length |
| `probe_backpressure.py` | Speaker HTTP throughput converges toward real time without `-re` |
| `probe_clock_drift.py` | A run settles near 1.00x; the old `+21..23 s` number includes startup and is not a speaker-buffer target |
| `probe_rung1_reads.py` | Rung 1 read sweep; three EQ commands and `SpkInGroup` are silent on this firmware |
| `probe_radio_browse.py` | The radio catalogue is a tree walked with `GetSelectRadioList`; browsing leaves the submode untouched |
| `probe_cp_escape.py` | `cp` is the submode URL playback runs in, and `SetFunc bt` then `SetFunc wifi` returns the speaker to `dlna` |
| `capture.py` | Produced the preserved official-client session in `capture.log` |
| `wamtap.py` | Standalone event and port diagnostics |

## Interpretation rules

- Share/DLNA verdicts use `StartPlaybackEvent`.
- URL/PCM does not hard-gate on that event. Physical foobar runs were audible after
  `AUDIO_STARTED` without a matching event before the former timeout.
- Do not run `probe_clock_drift.py`, `wamtap sniff` or another 55001 listener beside active
  `pcm_cli`; the extra connection can disrupt playback.
- `cp` is not damage. It is the submode `SetUrlPlayback` runs in, audible playback and all, and
  `SetFunc bt` then `SetFunc wifi` returns the speaker to `dlna`. An earlier revision of this
  file blamed `probe_radio_browse.py` for stranding the speaker there; re-measured, the whole
  browse leaves the submode alone.
- While in `cp`, `GetPlayStatus` omits `playstatus` entirely and `GetMusicInfo` answers
  `errCode: Wifi Sub Mode is CP`. Neither means playback failed.
- The CPM endpoint wedges under a fast series of calls: empty lists first, then 20-30 s of
  silence on `CPM` while `UIC` still answers. An empty list is not evidence of an empty level.
- A process-start-relative drift value includes discovery, URL handoff and helper startup.
  Do not publish it as a measured M5 cushion.
- `ReadTransferCount` and the tested process I/O counters are unusable on the current test
  machine. Use beefweb position, process trees and TCP connection state instead.

## capture.log

The preserved Samsung Multiroom session is not reproducible with the equipment still
available. Identifiers and addresses were replaced consistently; timings, protocol fields
and track titles remain intact.
