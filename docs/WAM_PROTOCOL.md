# Samsung WAM protocol notes

This document records facts measured on a physical Samsung Shape M5 and keeps them separate from assumptions still awaiting request-side capture.

## Test device

- model: `SPK-WAM550`
- firmware: `WAM550WWB-3117.1`
- protocol version: `2.3`
- LAN API: TCP `55001`

Primary evidence is preserved in the [physical M5 review](https://github.com/trvny/wambridge/pull/7#issuecomment-5147009977), [desktop application string analysis](https://github.com/trvny/wambridge/pull/7#issuecomment-5147063914) and [captured playback session](https://github.com/trvny/wambridge/pull/7#issuecomment-5147120908).

## Confirmed behavior

### Control connection and events

The speaker accepts HTTP-like commands over TCP `55001`. A connection left open receives responses and unsolicited state changes caused by other clients.

A passive connection observed `VolumeLevel` responses for commands sent through another socket. This means the control layer should use:

1. one persistent reader connection,
2. short-lived writer connections for commands,
3. a stream parser that handles several HTTP responses without relying on EOF.

A single `urlopen()` per command cannot reliably observe delayed events or methods that return more than one response.

### Client identity headers

Official clients send:

```text
mobileUUID: <stable client UUID>
mobileName: Wireless Audio
mobileVersion: 1.0
```

Responses contain `user_identifier`. The same client UUID is also used by local playback registration and metadata.

The UUID must remain stable for one client profile. Generating unrelated identities for commands, DMS registration and playback breaks the relationship expected by firmware.

### Volume scale

The tested M5 firmware uses raw API steps `0..30`.

Measured behavior:

```text
SetVolume 31  -> volume 30
SetVolume 45  -> volume 30
SetVolume 100 -> volume 30
```

The firmware silently clamps values above 30 and still returns success. `GetFeature` timed out on this firmware, so the number of steps cannot be discovered through that method.

Until model-aware translation is implemented, treat values as raw M5 steps:

- `3` is approximately 10 percent,
- `6` is approximately 20 percent,
- `30` is maximum.

### No AVTransport renderer

The tested M5 does not expose a standard UPnP MediaRenderer or AVTransport service.

Observed open TCP ports were `7676`, `8080` and `55001`. Port `9197` was closed. Enumeration of the UPnP endpoints on `7676` found no `MediaRenderer`, `AVTransport`, `ContentDirectory` or `MediaServer` service.

Do not restart the `foo_out_upnp` or generic AVTransport path for this model unless different firmware provides new evidence.

### Official local playback identity

A captured session from the Samsung Multiroom desktop application reported:

```xml
<device_udn>b00524c5-87b8-4439-9bb6-010545a40948</device_udn>
<playertype>myphone</playertype>
<playbacktype>playlist</playbacktype>
<sourcename>phone</sourcename>
<playindex>0</playindex>
<objectid>21329DC1305BF41B8AD9FCD0A6736302.mp3</objectid>
```

The same UUID appeared as:

- `mobileUUID`,
- `user_identifier`,
- `SetIpInfo` UUID,
- `MusicInfo.device_udn`.

Therefore `device_udn` is the raw client UUID in this playback flow. It is not the UPnP UDN of the MediaServer and has no `uuid:` prefix.

### DMS address and object IDs

The speaker reported the registered DMS address as:

```text
10.0.0.103:49200
```

The official desktop DMS uses port `49200`.

The played object ID was a flat hash with extension:

```text
21329DC1305BF41B8AD9FCD0A6736302.mp3
```

The artwork used the same pattern with `.jpg`. The captured playback metadata had empty `parentid` values. This differs from the experimental numeric `0` and `1` hierarchy used by PR #7.

### Queue playback and state transitions

The session emitted:

```text
DMSAddedEvent
IpInfo
MediaBufferStartEvent
MediaBufferEndEvent
MusicInfo
AddSongsToMultiQueueResult
MultiQueueList
StartPlaybackEvent
MusicPlayTime
```

Important meanings:

- `DMSAddedEvent` confirms that the speaker registered the DMS.
- `StartPlaybackEvent` confirms that playback actually started.
- `MusicPlayTime` exposes the current position and total length.
- captured `MusicInfo` reported `seek=enable` and `pause=enable`.

HTTP contact with the DMS is useful diagnostics, but it is not the final success condition.

### DMS implementation clues

The Samsung desktop package contains native `DMS.dll`. Its observed server identifies with old `pupnp/libupnp` behavior. The current Python DMS already matches several useful DLNA details, including finite content length, range support and MP3 protocol information.

The official DMS appeared to restrict requests to the speaker address. That is not required for initial compatibility, but it explains why manual browser requests may receive different status codes than the speaker.

## Confirmed wrong assumptions in PR #7

The following values were useful experiments but are not faithful to the captured official session:

| Experimental assumption | Captured behavior |
| --- | --- |
| DMS port `3921` | DMS port `49200` |
| `device_udn` is the MediaServer UDN | `device_udn` is the client UUID |
| `uuid:<server UUID>` | raw client UUID |
| numeric object ID `1` | flat `<hash>.mp3` |
| success inferred from DMS HTTP contact | success reported by `DMSAddedEvent` and `StartPlaybackEvent` |
| object served from the server root | speaker requests `/DLNA/<objectid>` |

Two earlier entries in this table were themselves wrong and have been removed after direct
measurement (see below):

- `playertype` and `sourcename` were listed as needing `myphone` / `phone`. Measured: they
  make **no difference** to this path. `myphone` and `allshare` behave identically.
- the single-file share command was listed as not being the final path because the captured
  session used a multi-queue. Measured: `SetSharePlaybackControl` **works** on this firmware.
  The official application simply chooses the queue path instead.

The browse experiment remains valuable because the physical M5 fetched `description.xml`, service descriptions and executed `ContentDirectory.Browse`. It proved that the network path and basic Python MediaServer were reachable.

## Request-side facts still missing

The passive listener observes speaker responses, not the exact requests sent by the desktop application. The following remain inferred from binary format strings and response names:

- whether `SetDms` is required before queue construction,
- the exact argument list and order for `AddSongsToMultiQueue`,
- which `SetMultiPlaybackControl` variant starts the queue,
- whether the firmware requires `playbackcontol` or `playbackcontrol` for each variant,
- the complete startup timing between registration, queue insertion and play.

Desktop binary strings show separate command families for `mypc`, `myphone` and `allshare`. Library browsing may use `mypc` while active playback reports `myphone`. Do not collapse these names into one global constant.

## Share playback: measured working configuration

`SetSharePlaybackControl` reaches audible playback on the physical M5. Verified with a
control experiment that separates network faults from protocol faults, and confirmed by
ear. Two independent bugs were stacked; fixing only one produces no observable change.

| Variant | Speaker replied | HTTP fetches | Result |
| --- | --- | --- | --- |
| control: `SetUrlPlayback` | yes | 1 | proves the network path is open |
| `device_udn` = raw UUID | yes | 5 retries | `ErrorEvent errCode=URL_OPEN_FAIL` |
| `device_udn` = `uuid:` + UUID | **no reply at all** | 0 | silently ignored |
| raw UUID **and** `/DLNA/` path | yes | 1 | `StartPlaybackEvent`, audio |

1. **`device_udn` must be the raw registered UUID.** The `uuid:` prefix makes the firmware
   ignore the command entirely — no reply, no error, no fetch. This is the "no contact"
   symptom. The field resolves against the `SetIpInfo` registration, so it must match the
   `uuid` sent there, which `set_ip_info_apk` already requires to be raw.
2. **The object must be served at `/DLNA/<objectid>`.** Serving from the root returns 404,
   the speaker retries five times and reports `URL_OPEN_FAIL`.

`GetDmsList` entries do carry a `uuid:` prefix in `dmsid`. That is a different field and is
the likely origin of the wrong assumption.

Successful playback emits `MediaBufferStartEvent` → `MediaBufferEndEvent` →
`StartPlaybackEvent`. `MusicInfo` and `PlayStatus` are **not** trustworthy: after a probe
they returned `playstatus=play` with nothing playing, and mixed fields from the current
command with `device_udn` and `objectid` left over from an earlier session.

## Formats

Measured through the share path, verdict taken from `StartPlaybackEvent`:

| Format | Result | HTTP requests |
| --- | --- | --- |
| MP3 44.1/16 | plays | 1 |
| WAV 44.1/16 PCM | plays | 1 |
| FLAC 44.1/16 | plays | 1 |
| FLAC 96/24 | plays | 1 |
| AAC in MP4 (`.m4a`) | plays | 3 |
| Opus 48k | `ErrorEvent` after 5 retries | 5 |

- FLAC needs no transcoding, including high resolution.
- `Range` support is mandatory for MP4: the speaker issues three requests (`0-`,
  `<end>-`, `44-`) while locating the `moov` atom.
- The player identifies as `Lavf52.104.0`, so format support follows libavformat.

## Transport and pacing

The speaker pulls from the local HTTP server. This removes the need to throttle FFmpeg or
the HTTP socket, but it does **not** let a host output forget PCM after writing it to a pipe.
The host still has to count accepted-but-not-yet-heard frames as latency and keep that
backlog bounded.

Pushing WAV over HTTP as fast as the socket accepts it:

```text
+4s   4.43x realtime   <- initial buffer fill
+8s   2.70x
+12s  2.13x
+16s  1.84x
+average 1.18x
```

The TCP window closes and HTTP throughput converges toward real time. Backpressure is the
right clock for the speaker-facing transport and is more accurate than FFmpeg `-re`.
A 100-second unanchored control run through `ffmpeg | pcm_cli` showed an initial burst of
about 6.4x and stable throughput around 1.00x after roughly 25 seconds. Its apparent
`+23 s` reserve included discovery, URL handoff and helper startup, so it is only an upper
bound, not a measured speaker cushion and not a target for `get_latency()`.

That measurement does not prove the complete foobar output clock: a host can still decode
far ahead if it reports pipe writes as already played. A physical foobar test on 2026-08-01
exposed exactly that split. The M5 continued playing normally while foobar's seekbar ran
ahead. No complete track had yet been timed end to end.

`WAMBRIDGE PLAYING` must be emitted only after the persistent TCP 55001 listener receives
a `StartPlaybackEvent` whose `user_identifier` matches the stable client UUID used by the
playback command. Encoder output, an HTTP request or an event from another controller cannot
start the host clock.

Endless streams of unknown length are accepted by `SetUrlPlayback`:

| Response shape | Result |
| --- | --- |
| `Transfer-Encoding: chunked` | streams cleanly, one connection |
| no `Content-Length`, `Connection: close` | streams cleanly, one connection |
| MP3 with no length, radio style | streams cleanly, one connection |
| oversized fake `Content-Length` | **worst** — four connections, three aborted |

Do not fake `Content-Length`. Chunked or close-delimited is cleaner.

## Target architecture

The product goal is a foobar2000 output plugin that carries whatever foobar plays, including
internet radio, and stays reusable for other host applications later. That rules out any
design that assumes a finite local file.

**Foundation — works for every source:**

1. create or load one stable client UUID,
2. open a persistent reader on TCP `55001`,
3. send all commands with official mobile headers,
4. serve the audio over local HTTP, chunked or close-delimited, never a faked
   `Content-Length`,
5. point the speaker at it with `SetUrlPlayback`,
6. let the speaker pull without FFmpeg `-re` or HTTP throttling; host outputs must retain
   accepted PCM in their latency accounting until a real-time playback clock releases it,
7. wait for the matching `StartPlaybackEvent`, and treat `MusicInfo` and `PlayStatus` as
   unreliable,
8. use matching `ErrorEvent` values for diagnostics.

**Optional layer — finite local files only**, when seek, pause and duration are wanted:

1. serve the object at `/DLNA/<objectid>` on port `49200`,
2. register the client UUID through `SetIpInfo`,
3. send `SetSharePlaybackControl` with `device_udn` set to that same raw UUID,
4. wait for `MediaBufferStartEvent` → `StartPlaybackEvent`.

A single attempt is enough once both values are correct. The three-attempt strategy in
PR #7 exists only because the working form was unknown, and should be removed rather than
kept as a fallback.

The response parser must preserve raw error fields including both `errcode` and `errCode` spellings.

## Safety

- Keep TCP `55001` and local DMS ports inside the trusted LAN.
- Start physical tests at volume step `3` or lower.
- Do not interpret an `ok` command response as proof that audio started.
- Keep PR #7 unmerged until normal-speed playback, pause, seek and cleanup are verified on the physical M5.
