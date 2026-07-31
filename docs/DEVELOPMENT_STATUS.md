# Development status

Last reviewed: 2026-07-31.

This file is the continuity note for playback work. Read it before opening another branch or reviving an older experiment.

## Stable on `main`

- Samsung WAM discovery through SSDP with local subnet fallback.
- Saved speaker profiles resolved by stable device ID.
- Status, volume, mute, pause, play, stop and standby commands.
- Direct URL playback through `SetUrlPlayback`.
- Custom radio stations and native TuneIn preset playback.
- Windows build for the Python helper and foobar2000 2.x x64 component.
- Minimal Python lint and test checks from merged PR #6.

`main` remains centered on short-lived HTTP streams started with `SetUrlPlayback`. That path is useful for radio and direct URLs but does not provide reliable native resume, seek or finite-track state.

## Active pull requests

### PR #2: restrict helper handle inheritance

Branch: `fix/restrict-helper-handles`

Purpose:

- launch the helper through `STARTUPINFOEX`,
- inherit only the helper stdin and stdout pipes,
- prevent unrelated foobar component handles from leaking into the child process.

Status:

- automated tests and Windows build passed,
- physical M5 validation is still required,
- do not merge solely because later playback experiments build on it.

### PR #4: pace foobar PCM writes

Branch: `fix/pace-foobar-pcm`

Base: PR #2 branch, not `main`.

Purpose:

- rate-limit PCM writes according to sample time,
- account for the block currently in flight,
- improve diagnostics around READY, PLAYING, EOF and flush.

Observed problem:

- normal playback starts on the physical M5,
- a roughly three-minute track becomes accelerated or garbled after several seconds,
- FFmpeg can encode faster than real time while the speaker consumes the URL stream differently.

Open concern:

- `pcm_stream.py` adds FFmpeg `-re` to a `pipe:0` input already paced by foobar,
- PR #4 adds another clock in C++,
- multiple independent pacing layers may cause drift, underruns or queue distortion.

Do not merge before a fresh physical test. Reconsider `-re` before adding more pacing.

### PR #7: finite local MP3 through Samsung DMS

Branch: `feat/dlna-file-playback`

Purpose:

- make the M5 fetch a finite local MP3 from a temporary MediaServer,
- obtain real duration, pause and seek support,
- replace endless `SetUrlPlayback` streaming for local tracks.

What the experiment proved on the physical M5:

- SSDP and `SetIpInfo` can lead the speaker to the local server,
- the M5 fetched `description.xml`, ContentDirectory SCPD and ConnectionManager SCPD,
- the M5 invoked `ContentDirectory.Browse`,
- the speaker briefly switched state, shown by its red indicator.

What later protocol capture invalidated:

- port `3921`,
- MediaServer UDN as `device_udn`,
- `playertype=allshare`,
- numeric object ID `1`,
- HTTP contact as the final success signal,
- `SetSharePlaybackControl` as the intended final playback path.

The official desktop session instead used client identity, DMS port `49200`, a flat hash object ID and a multi-queue. See [WAM_PROTOCOL.md](WAM_PROTOCOL.md).

PR #7 must be reworked around the captured event flow before merge. Existing commits are research history, not a production-ready implementation.

## Approaches and conclusions

### Standard UPnP AVTransport

Result: rejected for the tested M5.

The physical `SPK-WAM550` exposes no MediaRenderer or AVTransport service and port `9197` is closed. Generic foobar UPnP outputs have no renderer endpoint to control.

### Endless PCM to FLAC or MP3 URL

Result: audio starts, but long-track timing is unstable.

Advantages:

- simple HTTP source,
- works with `SetUrlPlayback`,
- useful for radio and truly live streams.

Limitations:

- no reliable resume,
- no native finite duration or seek,
- encoding and transport pacing can diverge,
- the speaker may consume buffered data in an unexpected time base.

### Finite MP3 through a local DMS

Result: protocol direction confirmed, playback command flow not yet reproduced.

This is the current preferred path for local files because the official session reports:

- finite track length,
- pause enabled,
- seek enabled,
- playback progress events.

### Predictable WAV with `Content-Length`

Result: untested alternative.

A finite WAV size can be calculated from known PCM duration and format, unlike streamed FLAC. It may provide a simpler `SetUrlPlayback` experiment with `Content-Length` and ranges. Keep it separate from the DMS work until the official DMS queue path is either reproduced or conclusively blocked.

## Next implementation order

1. Fix raw M5 volume handling to `0..30` or add model-aware translation.
2. Add stable `mobileUUID`, `mobileName` and `mobileVersion` request headers.
3. Add a persistent TCP `55001` event reader and multi-response parser.
4. Preserve raw error codes, including `errCode` spelling.
5. Rework PR #7 to DMS port `49200` and flat hash object paths.
6. Use the client UUID for `SetIpInfo` and `device_udn`.
7. Reproduce the one-item multi-queue and wait for `DMSAddedEvent` and `StartPlaybackEvent`.
8. Verify normal-speed playback, pause, resume, seek, stop and state restoration on the physical M5.
9. Only then integrate finite DMS playback into the foobar component.

## Rules for continuing work

- Check current `main`, open PRs and recent commits before changing code.
- Keep one logical stage per PR.
- Do not merge PR #2, #4 or #7 without the relevant physical M5 test.
- Do not reopen AVTransport work without new device evidence.
- Do not reuse port `3921`, `allshare` or MediaServer UDN as captured official values.
- Keep test volume at raw step `3` or lower until volume translation is fixed.
- Treat passive TCP logs as responses and events, not proof of the exact outgoing request body.
- Preserve useful failed experiments in documentation instead of leaving dead production paths.
