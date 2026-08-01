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
| `playertype=allshare` | `playertype=myphone` during playback |
| `sourcename=WAMBridge` | `sourcename=phone` |
| numeric object ID `1` | flat `<hash>.mp3` |
| success inferred from DMS HTTP contact | success reported by `DMSAddedEvent` and `StartPlaybackEvent` |
| single-file share command as final path | captured session used a multi-queue |

The browse experiment remains valuable because the physical M5 fetched `description.xml`, service descriptions and executed `ContentDirectory.Browse`. It proved that the network path and basic Python MediaServer were reachable.

## Request-side facts still missing

The passive listener observes speaker responses, not the exact requests sent by the desktop application. The following remain inferred from binary format strings and response names:

- whether `SetDms` is required before queue construction,
- the exact argument list and order for `AddSongsToMultiQueue`,
- which `SetMultiPlaybackControl` variant starts the queue,
- whether the firmware requires `playbackcontol` or `playbackcontrol` for each variant,
- the complete startup timing between registration, queue insertion and play.

Desktop binary strings show separate command families for `mypc`, `myphone` and `allshare`. Library browsing may use `mypc` while active playback reports `myphone`. Do not collapse these names into one global constant.

## Target architecture

The next playback implementation should:

1. create or load one stable client UUID,
2. open a persistent reader on TCP `55001`,
3. send all commands with official mobile headers,
4. serve the finite MP3 and artwork on port `49200`,
5. register the same UUID through `SetIpInfo`,
6. wait for `DMSAddedEvent`,
7. construct a one-item multi-queue using a flat hash object ID,
8. start playback using the matching multi-playback command,
9. wait for `StartPlaybackEvent`,
10. use `MusicPlayTime` for progress and `ErrorEvent` for diagnostics.

The response parser must preserve raw error fields including both `errcode` and `errCode` spellings.

## Safety

- Keep TCP `55001` and local DMS ports inside the trusted LAN.
- Start physical tests at volume step `3` or lower.
- Do not interpret an `ok` command response as proof that audio started.
- Keep PR #7 unmerged until normal-speed playback, pause, seek and cleanup are verified on the physical M5.
