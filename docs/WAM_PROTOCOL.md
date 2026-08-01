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
| URL/PCM through `SetUrlPlayback` | `AUDIO_STARTED` anchors bounded host flow; correlated events remain diagnostics | Audible short runs work; full-track timing validation is pending |
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
URL handoff and helper startup. It is only an upper bound, not a measured speaker buffer and
not a latency target.

### Foobar-facing accounting

Writing PCM to the helper pipe does not mean it has been heard. The output adapter must
retain all of these in latency and capacity accounting:

- PCM still in the C++ queue,
- the current pipe write,
- PCM submitted to the helper but not released by the host playback clock.

Two physical failures identified the required algorithm:

1. Waiting for `StartPlaybackEvent` before releasing capacity filled the minimum four-second
   output capacity and froze foobar after exactly four seconds, while the M5 path could
   otherwise play.
2. Starting at `AUDIO_STARTED` but resetting the clock anchor to `now` whenever wall time
   caught submitted PCM made the anchor follow pipe-write speed. Foobar advanced at about
   94x and immediately opened later tracks.

The correction is one cumulative monotonic anchor at `AUDIO_STARTED`, shifted only by pause.
Compute a real-time target from that fixed anchor and set played frames to
`min(target, submitted)`. Never move the anchor because submitted PCM temporarily ran out.

No complete 3-5 minute foobar track has yet passed this corrected algorithm on hardware.
That physical run remains the release gate.

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

## No AVTransport renderer

The tested M5 exposes no standard UPnP MediaRenderer or AVTransport service. Ports `7676`,
`8080` and `55001` were open; `9197` was closed. Do not restart generic `foo_out_upnp` work
without evidence from different firmware.

## Still unknown

- Exact official multi-queue request bodies and timing.
- Whether `cp` blocks the share/DLNA path.
- A reliable URL/PCM speaker event that always corresponds to audible start.
- Full-track drift, pause/resume and transition behavior of the fixed-anchor foobar build.

## Safety and acceptance

- Keep control and HTTP/DMS ports inside the trusted LAN.
- Do not treat an `ok` response as audible playback.
- Do not run competing control probes during PCM playback.
- A transport candidate is accepted only after a complete 3-5 minute track at wall-clock
  speed, stable seekbar, second track, pause/resume, stop/change and clean helper/FFmpeg
  shutdown on the physical M5.
