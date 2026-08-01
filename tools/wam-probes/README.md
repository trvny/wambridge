# WAM probes

Throwaway scripts that produced the measured facts in `docs/WAM_PROTOCOL.md`. They are kept
because the measurements are expensive to repeat: each one isolates a single variable against a
physical speaker, and several of them disproved an assumption the code was built on.

They are diagnostic artifacts, not a supported tool. Comments are in Polish, the style is
one-shot, and nothing here is covered by the test suite.

## Configuration

Every script reads its addresses from the environment, so nothing is hardcoded to one network:

| Variable | Meaning | Default |
| --- | --- | --- |
| `WAM_SPEAKER` | speaker address | `192.168.1.50` |
| `WAM_HOST` | this machine, as the speaker sees it | `192.168.1.10` |
| `WAM_MEDIA` | audio file to serve | `sample.mp3` |
| `WAM_SCRATCH` | where ffmpeg writes test files | `_scratch/` next to the script |

`probe_share.py` holds the shared HTTP server and event plumbing; the other probes import it.

## What each probe settled

| Script | Question | Answer |
| --- | --- | --- |
| `probe_share.py` | Does `SetSharePlaybackControl` work, and does the `device_udn` form matter? | Raw UUID is answered, `uuid:`-prefixed is silently ignored |
| `probe_playertype.py` | `allshare` or `myphone`? | The official app sends `myphone` |
| `probe_dlna_headers.py` | Does the renderer need DLNA response headers? | Yes — without them it fetches and stays silent |
| `probe_confirm_audio.py` | Does the assembled configuration actually make sound? | Yes, confirmed by ear |
| `probe_formats.py` | What will the speaker play through the share path? | FLAC up to 96/24 |
| `probe_livestream.py` | Will it play an HTTP stream with no known length? | Yes — which is what makes a foobar2000 output viable |
| `probe_backpressure.py` | Does TCP backpressure pace the stream on its own? | Yes — this closed PR #4 as solving a non-problem |
| `capture.py` | What does the official Samsung app actually send? | Produced `capture.log`, with a hard volume limiter so the tap cannot blast the room |
| `wamtap.py` | Passive event tap and port diagnostics | Generally useful; no dependencies |

Verdicts rest on `StartPlaybackEvent` from TCP 55001. `MusicInfo` and `PlayStatus` were measured
lying, so no probe trusts them.

## capture.log

A session of the official Samsung Multiroom app talking to an M5, captured passively. This is the
source of roughly half of `docs/WAM_PROTOCOL.md` and **cannot be reproduced** — the desktop app has
no radio function, and the phone that recorded it is gone.

Identifiers were replaced before committing, each original mapped to one fixed stand-in so the
analysis still holds — in particular that `device_udn`, `user_identifier` and the `SetIpInfo` UUID
carry the same value, and that `user_identifier` falls into three buckets. Addresses, MAC
addresses, the Wi-Fi SSID and client UUIDs are stand-ins; timings, protocol fields and track titles
are untouched.
