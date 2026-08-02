# Samsung WAM protocol notes

Measured facts for a physical Samsung Shape M5. Keep path-specific observations separate:
what is true for share/DLNA is not automatically true for `SetUrlPlayback` PCM streams.

## Test device

- model: `SPK-WAM550`
- firmware: `WAM550WWB-3117.1`
- protocol version: `2.3`
- LAN control API: TCP `55001`
- raw volume range: `0..30`

Primary evidence is preserved in PR #7, PR #21 and `tools/wam-probes/`.

## Current path summary

| Path | What confirms or anchors it | Current state |
| --- | --- | --- |
| URL/PCM through `SetUrlPlayback` | `AUDIO_STARTED` anchors bounded host flow | Validated on hardware 2026-08-02: full checklist at a median 1.00x |
| Share/DLNA through `SetSharePlaybackControl` | `StartPlaybackEvent` | Audible finite-file playback is proven; integration is not active |
| Generic UPnP AVTransport | none exists on this M5 | Rejected |

`MusicInfo` and `PlayStatus` are not reliable success signals. They have reported `play`
with no audio and mixed fields from older sessions.

## TCP 55001 and client identity

Official clients send a stable identity:

```text
mobileUUID: <stable client UUID>
mobileName: Wireless Audio
mobileVersion: 1.0
```

Responses contain `user_identifier`. Captures show three useful buckets: the client UUID,
a foreign UUID and the literal `public` for unattributed broadcasts.

For active PCM playback, one persistent connection must both send commands and read events.
Opening a second listener or probe while `pcm_cli` owns the control channel can make the M5
stop answering the player. The standalone `wambridge-events` tool is diagnostic and should
not be run beside an active PCM session.

A command timeout is not proof of failure. The firmware may answer late or emit an unrelated
event. Match what can be matched; keep unmatched events as diagnostics. A concrete
`NETWORK_TIMEOUT_ERROR` after the speaker requested the active HTTP stream is meaningful
because it indicates starvation of that stream.

## URL/PCM playback

The universal path is:

```text
foobar PCM -> helper stdin -> FFmpeg -> local HTTP -> SetUrlPlayback -> M5
```

The speaker pulls from the local HTTP server. Do not send a remote URL directly:

- HTTPS has failed,
- Ogg has been fetched silently,
- HLS has wedged the control port.

Proxy all sources through the local server.

### Submode

`SetUrlPlayback` normally switches the tested M5 from `dlna` to `cp`. A measured run stayed
in `cp` for about 100 seconds while throughput held around 1.00x and audio was confirmed by
ear. `cp` is not a URL-path fault and must not trigger a power-cycle instruction.

### One encoder per PCM input

Every FFmpeg launched for a stream request inherits the same stdin. The M5 may open a second
HTTP request while the first is still live.

- Running both encoders splits one PCM stream and produces holes, jumps and leaked processes.
- Retiring the first encoder kills the stream actually being consumed.
- The correct behavior is to keep the first active encoder and refuse later requests.

This removed the repeated `NETWORK_TIMEOUT_ERROR` pattern caused by starving the speaker.

### Protocol markers

The helper exposes this progression to the component:

```text
WAMBRIDGE STREAM_REQUESTED
WAMBRIDGE ENCODER_STARTED
WAMBRIDGE READY
WAMBRIDGE AUDIO_STARTED
WAMBRIDGE PLAYING volume=<raw-step>
```

`AUDIO_STARTED` means FFmpeg produced bytes and the HTTP response started carrying encoded
audio. It does not prove that the speaker is audible. `PLAYING` on the URL/PCM path means the
helper has a live stream and its bounded host clock may progress; it is not a renamed
`StartPlaybackEvent`.

Repeated physical runs were audibly playing after `AUDIO_STARTED` but did not emit a
matching `StartPlaybackEvent` before the former 45-second timeout. Therefore URL/PCM must
not block or abort on that event. Keep the listener active, log a correlated event when it
appears and still surface real asynchronous failures.

## Speaker transport versus host output clock

These are separate clocks.

### Speaker-facing transport

The M5 closes its TCP receive window as its buffer fills. HTTP throughput converges toward
real time without FFmpeg `-re` or manual socket throttling. A control run showed an initial
burst, then stable throughput near 1.00x after roughly 25 seconds.

The often-quoted `+21..23 s` value was measured from process start and includes discovery,
URL handoff and helper startup. It is an upper bound on startup, not the steady-state delay.

### Measured audio delay: about 13 seconds

Measured 2026-08-02, mid-stream, with playback already settled. The foobar volume slider was
moved at a recorded playback position and the listener read the seekbar when the change was
heard. Two events in one run: sent at 12.14 s and heard at 26 s, sent at 37.16 s and heard at
50 s. **About 13.4 s**, spread one second, of which reaction time is roughly half a second.

Attribution:

| term | share |
| --- | --- |
| host buffer counted by `get_latency()` | ~3.9 s |
| `adelay=1500` startup silence in the helper | 1.5 s |
| FFmpeg and the local HTTP socket | under a second |
| **the speaker's own prebuffer** | **~7-8 s** |

Two consequences for anything built on this path:

- The M5 holds several seconds of audio that no host accounting can see. `get_latency()`
  reports about 4 s and is therefore some nine seconds optimistic. Do not treat it as the
  distance between a sample and the ear.
- Anything applied host-side to PCM already on its way — a software volume gain, silence
  written for pause — is heard 13 s later. Controls that must feel immediate belong on the
  `55001` path, which answers in about a second.

Whether that prebuffer is a byte count or a duration is still unmeasured. A run at 320 kbps
against FLAC's 700-900 answers it: bytes shrink with the bitrate, seconds do not.

### Foobar-facing accounting

Writing PCM to the helper pipe does not mean it has been heard. The output adapter must
retain all of these in latency and capacity accounting:

- PCM still in the C++ queue,
- the current pipe write,
- PCM submitted to the helper but not released by the host playback clock.

The adapter must also accept **every** frame the host offers it. `process_samples` returns
void, so a partial write cannot be reported and the caller counts the whole chunk as played.
Taking `min(free, chunk)` and dropping the rest is invisible from the protocol side and
looks exactly like a runaway clock. Block until there is room instead.

Three physical failures identified the required algorithm:

1. Waiting for `StartPlaybackEvent` before releasing capacity filled the minimum four-second
   output capacity and froze foobar after exactly four seconds, while the M5 path could
   otherwise play.
2. Starting at `AUDIO_STARTED` but resetting the clock anchor to `now` whenever wall time
   caught submitted PCM made the anchor follow pipe-write speed. Foobar advanced at about
   94x and immediately opened later tracks.

3. Dropping the part of a chunk that did not fit made foobar run a 220 s track out in 22 s
   at a median 11x, while `submitted` advanced at 1.04x, `buffered` sat at 3.8-4.0 s of a
   4.0 s capacity and `free` hovered near 100 ms. Every clock term was correct. About nine
   tenths of each chunk was being discarded and counted as played.

The correction is one cumulative monotonic anchor at `AUDIO_STARTED`, shifted only by pause.
Compute a real-time target from that fixed anchor and set played frames to
`min(target, submitted)`. Never move the anchor because submitted PCM temporarily ran out.
And never decline part of a chunk.

Failure 3 is worth remembering as a method, not just a bug: the first two were found by
reading the clock code, and the third was invisible that way. It took printing every term
once a second to see that the terms were fine and the audio was not.

A complete track passed this algorithm on hardware on 2026-08-02, together with seek,
pause/resume, a natural track transition, radio HLS across a 44.1 to 48 kHz switch, stop and
clean process shutdown.

## Share/DLNA finite-file playback

`SetSharePlaybackControl` reaches audible playback when both of these are correct:

1. `device_udn` is the raw registered client UUID, without a `uuid:` prefix.
2. The object is served at `/DLNA/<objectid>`.

Measured values and behavior:

- official DMS port: `49200`,
- object IDs: flat `<hash>.<extension>`,
- successful sequence: `MediaBufferStartEvent` -> `MediaBufferEndEvent` ->
  `StartPlaybackEvent`,
- `MusicPlayTime` exposes position and total length,
- `playertype` and `sourcename` did not change the result,
- a single-file share command works; the official app's multi-queue is not mandatory.

The old assumptions `3921`, MediaServer UDN as `device_udn`, numeric object `1`, root-path
serving and HTTP contact as success are all invalid.

### Formats measured through share playback

| Format | Result | Requests |
| --- | --- | --- |
| MP3 44.1/16 | plays | 1 |
| WAV 44.1/16 PCM | plays | 1 |
| FLAC 44.1/16 | plays | 1 |
| FLAC 96/24 | plays | 1 |
| AAC in MP4 (`.m4a`) | plays | 3 with Range |
| Opus 48k | fails after retries | 5 |

MP4 requires Range support. FLAC does not require transcoding, including the measured
96/24 file.

## Streams and lengths

`SetUrlPlayback` accepts endless streams with no known duration:

| HTTP response | Result |
| --- | --- |
| chunked | clean, one connection |
| no `Content-Length`, connection close | clean, one connection |
| radio-style MP3 | clean, one connection |
| oversized fake `Content-Length` | worst case: retries and aborted connections |

Never fake a length. Use chunked or close-delimited output.

## Volume

The tested firmware uses raw steps `0..30`. Values above 30 are silently clamped to maximum
while still returning success. Until model-aware conversion exists:

- `3` is approximately 10 percent,
- `6` is approximately 20 percent,
- `30` is maximum.

Start hardware tests at step `3` or lower.

`SetVolume` on the shared control connection changes the speaker within about a second,
measured while a stream was playing. A host-side gain applied to PCM on its way out reaches
the ear about 13 s later. Volume that should feel immediate belongs on this path, and a
matched `result="ng"` for `SetVolume` must be surfaced: startup mutes the speaker on purpose,
so a rejected unmute leaves it silent while every other signal says it is playing.

## No AVTransport renderer

The tested M5 exposes no standard UPnP MediaRenderer or AVTransport service. Ports `7676`,
`8080` and `55001` were open; `9197` was closed. Do not restart generic `foo_out_upnp` work
without evidence from different firmware.

## Still unknown

- Exact official multi-queue request bodies and timing.
- Whether `cp` blocks the share/DLNA path.
- A reliable URL/PCM speaker event that always corresponds to audible start.
- Whether the speaker's ~7-8 s prebuffer counts bytes or seconds.
- Whether pause carries the same ~13 s delay. It writes silence into the same pipe, so it
  probably does, but that has not been measured.

## Safety and acceptance

- Keep control and HTTP/DMS ports inside the trusted LAN.
- Do not treat an `ok` response as audible playback.
- Do not run competing control probes during PCM playback.
- A transport candidate is accepted only after a complete 3-5 minute track at wall-clock
  speed, stable seekbar, second track, pause/resume, stop/change and clean helper/FFmpeg
  shutdown on the physical M5.
