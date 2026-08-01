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
- Restricted helper handle inheritance from merged PR #2.
- Persistent TCP `55001` event listener and `wambridge-events` command from merged PR #9,
  verified against the physical M5.

`main` is centered on HTTP streams started with `SetUrlPlayback`. Measurement on 2026-08-01
promoted that from a stopgap to the intended foundation: the speaker accepts streams of
unknown length and paces itself through TCP backpressure. What it still does not provide is
native resume, seek or finite-track state — that is what the share path adds for local files.

## Active pull requests

### PR #2: restrict helper handle inheritance

Branch: `fix/restrict-helper-handles`

Purpose:

- launch the helper through `STARTUPINFOEX`,
- inherit only the helper stdin and stdout pipes,
- prevent unrelated foobar component handles from leaking into the child process.

Status: **merged** on 2026-08-01 (`1e27383`).

Reviewed against the three usual traps of `PROC_THREAD_ATTRIBUTE_HANDLE_LIST`:
`EXTENDED_STARTUPINFO_PRESENT` is set, `DeleteProcThreadAttributeList` runs on both the
success and failure paths, and `bInheritHandles` stays `TRUE`. All correct.

Note for future stacked work: PR #4 was based on this branch rather than `main`. GitHub then
refused both a normal merge and a base change, and the merge had to go through
`PUT /repos/{owner}/{repo}/pulls/{n}/merge-async`. Prefer basing on `main`.

### PR #4: pace foobar PCM writes — closed, not merged

Branch: `fix/pace-foobar-pcm` (deleted; recoverable from the closed pull request).

Closed on 2026-08-01 after direct measurement showed the approach solves a problem that
does not exist in a pull-based design.

Pushing WAV over HTTP as fast as the socket accepts it converges to real time on its own —
4.43x, 2.70x, 2.13x, 1.84x, averaging 1.18x — because the speaker's TCP window closes.
Backpressure is a more accurate clock than FFmpeg `-re` or a timer in C++, and it costs
nothing. Adding a third pacing layer made the original three-clock conflict worse.

The correct fix is architectural: stop pushing. Serve the audio over HTTP and let the
speaker pull. See "Transport and pacing" in the protocol notes.

Not re-measured: whether the original garbled or accelerated audio also had a separate
cause in encoder timestamps. If that symptom returns under a pull design, look there
first rather than reintroducing pacing.

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
- numeric object ID `1`,
- HTTP contact as the final success signal,
- serving the object from the server root instead of `/DLNA/<objectid>`.

What direct measurement on 2026-08-01 then established:

- `SetSharePlaybackControl` **does work** on this firmware and reaches audible playback.
  It is not a dead path; the official application merely prefers the queue.
- Exactly two changes are required, and both are needed at once: a raw `device_udn`, and
  serving at `/DLNA/<objectid>`. Attempt 3 already sends the raw form, which is why fixing
  only the identity appeared to change nothing.
- `playertype` and `sourcename` are irrelevant here. `myphone` and `allshare` behave
  identically, so the earlier note to switch them can be dropped.

See [WAM_PROTOCOL.md](WAM_PROTOCOL.md) for the control experiment and the format matrix.

PR #7 should be reworked and substantially reduced. With the working form known, the
three-attempt strategy has no reason to exist, and this path becomes an optional layer for
finite local files rather than the foundation.

## Approaches and conclusions

### Standard UPnP AVTransport

Result: rejected for the tested M5.

The physical `SPK-WAM550` exposes no MediaRenderer or AVTransport service and port `9197` is closed. Generic foobar UPnP outputs have no renderer endpoint to control.

### Endless PCM to FLAC or MP3 URL

Result: **confirmed as the foundation.** Measured on 2026-08-01.

The speaker accepts streams of unknown length through `SetUrlPlayback` — chunked, or no
`Content-Length` with `Connection: close`, or radio-style MP3 — each on a single clean
connection. An oversized fake `Content-Length` is the worst option: four connections, three
aborted. Do not fake it.

Advantages:

- simple HTTP source,
- works with `SetUrlPlayback`,
- useful for radio and truly live streams,
- the speaker paces itself through TCP backpressure, so no pacing layer is needed,
- one transport serves every source, which keeps the design reusable outside foobar.

Limitations:

- no reliable resume,
- no native finite duration or seek — use the share path when those are wanted.

The earlier note that "encoding and transport pacing can diverge" was a consequence of a
push design and does not apply once the speaker pulls.

### Finite MP3 through a local DMS

Result: **working**, verified audibly on 2026-08-01 with a raw `device_udn` and the object
served at `/DLNA/<objectid>`.

Keep it as the optional layer for finite local files, where it adds what the plain stream
cannot: finite track length, pause, seek and playback progress events. It is not the
foundation, because it cannot carry radio or any other endless source.

### Predictable WAV with `Content-Length`

Result: **no longer needed.**

The idea was to compute a finite WAV size so `SetUrlPlayback` could report duration. Direct
measurement removed the premise: the speaker plays streams with no length at all, and an
oversized fake `Content-Length` was the worst-behaving variant tested — four connections,
three aborted. Where finite-track state is genuinely wanted, use the share path instead of
fabricating a length.

WAV, FLAC and even FLAC 96/24 all play, so there is also no need to transcode for
compatibility.

## Next implementation order

1. Fix raw M5 volume handling to `0..30` or add model-aware translation.
2. Add stable `mobileUUID`, `mobileName` and `mobileVersion` request headers.
3. Preserve raw error codes, including `errCode` spelling.
4. Remove the pacing layers from the PCM path: drop FFmpeg `-re` on `pipe:0` and let the
   speaker's backpressure set the rate.
5. Serve the output over local HTTP as chunked or close-delimited, never a faked
   `Content-Length`, and drive it with `SetUrlPlayback`. This is the foundation and it
   covers radio, files and future host applications alike.
6. Rework and shrink PR #7 as the optional finite-file layer: raw `device_udn`,
   `/DLNA/<objectid>`, one attempt, no fallback ladder.
7. Treat `StartPlaybackEvent` as the only success signal; never trust `MusicInfo` or
   `PlayStatus`.
8. Verify normal-speed playback, pause, resume, seek, stop and state restoration on the physical M5.
9. Only then integrate the finite-file layer into the foobar component.

## Rules for continuing work

- Check current `main`, open PRs and recent commits before changing code.
- Keep one logical stage per PR.
- Do not merge PR #7 without the relevant physical M5 test.
- Do not reopen AVTransport work without new device evidence.
- Do not reuse port `3921` or a MediaServer UDN as captured official values.
- Do not reintroduce a manual pacing layer. Backpressure already does it; see PR #4.
- Do not fake `Content-Length`. It behaves worse than sending no length at all.
- Base new branches on `main`, not on another pull request branch.
- Keep test volume at raw step `3` or lower until volume translation is fixed.
- Treat passive TCP logs as responses and events, not proof of the exact outgoing request body.
- Preserve useful failed experiments in documentation instead of leaving dead production paths.
