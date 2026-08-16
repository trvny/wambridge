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

### Measured audio delay: about six seconds

Re-measured 2026-08-08 on the build of PR #41, `startup_silence=0`, mid-stream with playback
settled. Method: the foobar volume is changed at a recorded playback position and the
listener reads the seekbar when the change is heard, so the number does not depend on
anyone's reaction time. Four changes per run, both formats over the same passages of the
same 19-minute 44.1 kHz source.

| passage | FLAC | WAV |
| --- | --- | --- |
| 192 s | 7.94 s | 5.97 s |
| 217 s | 5.91 s | 4.95 s |
| 242 s | 6.90 s | 5.91 s |
| 267 s | 5.91 s | 5.89 s |
| mean | **6.67 s** | **5.68 s** |
| spread | 2.0 s | 1.0 s |

Two other methods agree in the same session: after a pause the sound stopped about 5 s
later, and after resuming from 51.98 s it returned at 58 s, so 6.0 s.

Attribution, with `startup_silence=0`:

| term | share |
| --- | --- |
| host buffer counted by `get_latency()` | ~3.9 s |
| `adelay` startup silence in the helper | 0 s as configured, 1.5 s by default |
| FFmpeg and the local HTTP socket | under a second |
| the speaker's own prebuffer | ~2 s on FLAC, less on WAV |

Consequences for anything built on this path:

- **The host buffer is the largest single term now.** Its 4.0 s floor was dismissed as
  "2-3 s of thirteen"; against six it is most of the budget and worth revisiting.
- Anything applied host-side to PCM already on its way - a software volume gain, silence
  written for pause - is heard about six seconds later. Controls that must feel immediate
  still belong on the `55001` path, which answers in about a second.

**The older figure of 13.4 s is superseded, not merely refined.** It came from two data
points taken 2026-08-02 on a much older build with `startup_silence=1500`, over unrecorded
material. The silence explains 1.5 s of the gap and nothing recorded explains the rest.
The 7-8 s once attributed to the speaker was a subtraction remainder, never a measurement,
and it did not survive: with the total at 6.7 s and the host holding 3.9 s of it there is no
seven-second speaker left to point at. The owner's report that Samsung's own clients felt
clearly faster than 13 s was the correct instinct - most of that delay was ours.

**The prebuffer is bounded by bytes, at least partly. Confirmed from both directions.**

Thinner is slower: measured 2026-08-02 at MP3 320 kbps against FLAC's 700-900, the delay
grew from about 13.4 s to about 16.9 s. A buffer holding a fixed number of bytes holds
proportionally more seconds of a smaller stream.

Fatter is faster: measured 2026-08-08, WAV at a constant 1411 kbps beat FLAC on every
passage of the same source. The clearest evidence is the variance rather than the mean.
FLAC's bitrate rides the material, so on an uneven mix the delay breathes with it - 2.0 s of
spread, with the worst reading in the quietest passage where the stream went thin. WAV's
constant rate halved that spread.

The size still does not match a pure byte count. MP3 predicted roughly two and a half times
and gave one and a third; WAV predicted several seconds and gave one. Something else is
bounded as well, and it has not been identified.

Practical conclusion: bitrate is a real lever worth about a second and a steadier delay, not
a fix for responsiveness. Controls that must feel immediate belong on the `55001` path
regardless.

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

The `wav` profile is the one place where a length still appears, inside the payload rather
than the HTTP response: FFmpeg cannot seek back on a pipe, so both the RIFF and the `data`
size fields are `0xFFFFFFFF`. **The M5 accepts it and treats the stream as endless**,
measured 2026-08-08 over a 19-minute source through `SetUrlPlayback`: playback ran at a
median 1.00x, crossed into the next track without a restart, survived a seek and stopped
cleanly. The in-payload placeholder is therefore not the same trap as an oversized fake
HTTP `Content-Length`, which this firmware does punish.

## Volume

The tested firmware uses raw steps `0..30`. Values above 30 are silently clamped to maximum
while still returning success. Until model-aware conversion exists:

- `3` is approximately 10 percent,
- `6` is approximately 20 percent,
- `30` is maximum.

Start hardware tests at step `3` or lower.

**A speaker reading `0` is not a level to obey.** Startup mutes it and restores the level
once audio flows, so a helper killed in between leaves the speaker silent. Reading that back
and starting the next stream at `0` produces the worst signal this device can give: audio
flowing, `PLAYING` reported, sockets established and nothing audible. Treat `0` from
`GetVolume` at startup as missing information rather than as a request.

`SetVolume` on the shared control connection changes the speaker within about a second,
measured while a stream was playing. A host-side gain applied to PCM on its way out reaches
the ear about six seconds later. Volume that should feel immediate belongs on this path, and a
matched `result="ng"` for `SetVolume` must be surfaced: startup mutes the speaker on purpose,
so a rejected unmute leaves it silent while every other signal says it is playing.

## The speaker's own radio, and reading it

`GetApInfo` returns `ssid`, `mac`, `rssi`, `ch` and `connectiontype`, answers in 0.13 s and
works with the LED dark. Note the asymmetry: the command is **`GetApInfo`**, the reply carries
the method name `ApInfo`, and sending `ApInfo` costs a full timeout and looks exactly like an
unimplemented command. `GetNetworkInfo` and `GetWifiInfo` do not exist here and time out;
`GetSoftwareVersion` answers.

Measured on the test M5, 2026-08-16: `ssid` on the household **2.4 GHz** network, `ch 1`,
`rssi 4`, `connectiontype wireless`, while the host running foobar sat on the 5 GHz SSID. Every
audio byte therefore crossed the access point from a 5 GHz leg to a 2.4 GHz one, and the second
leg is the one that hurts: ping from the host averaged 2.2 ms to the access point and **176 ms
to the speaker** while idle, 4.2 ms against **91 ms** during playback, with zero packet loss
either way and only two networks visible in the area, both the owner's - so this is not
neighbouring congestion.

This matters because it is the first thing to check when the speaker tears down a stream
mid-playback. The signature of that is in the `CLOCK` line: `submitted` stops advancing while
`offered` keeps climbing and `queued` grows at roughly real time, with `buffered` still near
capacity. A full buffer at the moment of the break means the encoder was feeding fine and the
speaker stopped pulling, which puts the fault on the link or the speaker rather than here.

## Standby and the front LED

Measured 2026-08-02. Nothing on the control port reports the power state **directly**:
`GetPowerStatus`, `GetLedStatus` and `GetStandbyMode` do not exist on this firmware and all
three time out. That made the front LED the only indicator, and it needed a human in the room
until `GetMute` turned out to track it — see the section below, which supersedes this
paragraph on that point.

LED off is network standby, not power off. With the LED dark the M5 still answers `GetFunc`,
`GetVolume` and `GetApInfo` on `55001`. Wi-Fi and the control port stay up; the amplifier and
the display go down. A device coming into range wakes it with an audible "connected".

### The M5 reaches standby on its own

Measured 2026-08-16. Woken by a no-op `pwron` at 00:56:08 (`mute=off`), confirmed dark at
01:13:14 (`mute=on`): **under 17 minutes of idling, with no timer armed** and
`sleepoption=off` throughout. foobar2000 was running the whole time, idle — an open player
does not hold the speaker awake.

This corrects a claim that had spread through this repo in four places — two docstrings, a
user-facing warning string, and the sleep-timer helper on the open teardown branch: that this
firmware never reaches standby by itself and the sleep timer is the only way there.
**It is not.** The sleep timer is the only way to
reach standby *on demand*; left alone, the speaker gets there in about a quarter of an hour.

**With one condition, established 2026-08-16 and easy to miss: the countdown only starts once
every program has let go.** Same speaker, same evening, two sessions: one ended on a build that
sent no release and the LED was still lit 33 minutes later; the next ended with
`WAMBRIDGE STOPPED stop=sent holding=0` and the speaker was dark 17 min 4 s afterwards. A
speaker still holding a `SetUrlPlayback` session whose source vanished does not idle at all,
which is why this looked for a week like a firmware that never sleeps.
What has no configurable knob is the delay — `GetPowerSaving` and `GetAutoPowerDown` do not
exist, so the interval cannot be read or changed.

### `GetMute` is the LED, without a human

`mute=on` when the LED is dark, `mute=off` when it is lit — consistent across every
observation on 2026-08-15 and 2026-08-16, including a timer firing live. Use it instead of
asking someone to look at the speaker.

**It reads the mute flag, not the lamp.** This component mutes deliberately in places -
`standby` sends `set_mute(True)`, and startup mutes before unmuting - so `mute=on` means dark
*only when nothing has just muted it*. As an idle-state detector it is sound; as a check run
immediately after one of this component's own actions it is not.

Two properties that matter when measuring this:

- **Read-only commands do not wake a dark speaker.** About 25 requests over 18 minutes left
  it dark throughout.
- **Whether polling resets the idle countdown on a *lit* speaker is unknown.** Until it is
  settled, measure the idle interval with one late reading rather than a loop — a one-minute
  poll could manufacture the answer "it never sleeps".

A no-op `pwron` wakes it and is **silent**: no chime, confirmed by the person in the room.
That makes it a usable instrument at any hour.

`SetSleepTimer` reaches standby on demand:

- `sleeptime` is in **seconds**, not minutes. `60` counted down to `0` in one minute.
- On firing, the timer clears itself back to `sleepoption=off`, `sleeptime=0` and the speaker
  stays in standby. A fired timer leaves no trace, so `GetSleepTimer` cannot distinguish
  "asleep because a timer fired" from "asleep for any other reason".
- There is no *controllable* idle power-down: `GetPowerSaving` and `GetAutoPowerDown` do not
  exist, so nothing can read or set one. `SetSleepTimer` is the only power lever this
  firmware exposes to a client.

  That is not the same as the speaker never sleeping on its own, and this document said so
  for a while. Corrected 2026-08-15 on the owner's account of normal use: the M5 does go dark
  by itself, but only after every program talking to it has let go, and the Samsung app's
  sleep timer is a separate manual control that has never been seen to arm itself. An
  unreadable idle power-down is still an idle power-down. That reframes the release work
  below as the whole fix rather than half of one, and `sleep_after_stop` as a fallback for
  when it is not.

The component's standby menu item is misnamed. It sends a stop and a mute, which leaves the
speaker lit and fully powered.

### What kept it lit — SOLVED 2026-08-16

The speaker had been found still lit hours after a session ended. Three explanations were
tested; two were dropped and the third turned out to be it.

- **`submode` is not the cause.** The sleep timer put the speaker into standby while it
  stayed in `cp`, and it returns to `cp` on its own while idle. A CPM `SetPlaybackControl
  stop` was accepted and reported `playstatus=stop` without clearing `cp`.
- **Hard-killed sessions do not accumulate damage.** A helper killed with `taskkill /F` was
  respawned by the component 78 times in roughly two minutes, leaving 29 sockets in
  `TIME_WAIT` and briefly making the speaker stop answering altogether. After playback was
  stopped normally, the speaker went dark on its own within ten minutes and the sockets
  drained to 4 unaided. **One clean ending is enough to undo any number of abrupt ones.**
- **No clean ending at all — this was it.** Nothing on the PCM path ever told the speaker the
  stream was over, so every session left it holding a `SetUrlPlayback` whose source had
  vanished, and a speaker in that state never starts its idle countdown. Reproduced on
  2026-08-16: audio stopped at 18:44:48, foobar closed at 18:54:55, and the LED was still lit
  at 19:18 — 33 minutes, against an idle interval of under 17. The same evening, on a build
  that sends the release, a session ending `WAMBRIDGE STOPPED stop=sent sleep=off holding=0`
  at 20:57:15 was dark by 21:14:19: **17 min 4 s**.

Fixed in PR #48: `PlaybackWatcher.release()` sends UIC `SetPlaybackControl pause` over the
connection the listener already holds, at every helper exit, and the teardown line reports
what happened. Seeks were the risk, because a seek replaces a helper and therefore pauses the
speaker before the next `SetUrlPlayback` — measured over four seeks, audio resumed every time
and the cost is about a second per seek.

The standby action reports `holding=<count>` of TCP connections still attached to the speaker,
which turns "something was still attached" from a guess into a reading. It excludes
`TIME_WAIT` and the action's own closing sockets by design, so a respawn storm does **not**
inflate it — that shows up in the socket table, not here.

A second reading on 2026-08-09 narrowed it. The M5 was lit all night after a session that
was **not** hard-killed: foobar's console shows a normal `Shutting down...` with the stream
still up and no error. Reading the PCM path settled why. Nothing on it ever told the speaker
anything about the end of a stream: `stop_playback` was reachable only from the menu and
`cli.py`, while the helper's teardown closed the local HTTP server and the control socket and
exited. Every session, clean or not, left the M5 holding a URL playback session whose source
had simply vanished — and a speaker that believes it is still serving a session is exactly
the speaker that never reaches the idle state its own power-down needs.

The helper now releases the speaker before it goes, over the persistent `55001` connection it
already holds: `SetPlaybackControl pause` on the UIC API, the same command `stop_playback`
uses on this path, without the mute that would hand the speaker back silent, and without
`pwron`. It then reports `WAMBRIDGE STOPPED stop=<sent|rejected|unreachable|skipped>
sleep=<off|Ns|skipped|unreachable|rejected> holding=<count>` once its own sockets are gone. `holding` counts every local
socket attached to the speaker, this helper's included — one it failed to close is a leak
like any other, and hiding it would make the count's zero mean "nobody checked". What it
skips is only its own sockets that are *already closing*: those linger in `FIN_WAIT` for a
measured 0.5 s to 1.5 s while the kernel finishes, and waiting them out cost that on every
helper exit. A killed session's sockets are untouched by that rule — their owner is gone, so
its PID cannot match — and they are the case this reading exists for.

Releasing cleanly **is** enough for the M5 to go dark by itself, measured 2026-08-15. A
session ended `stop=sent sleep=off holding=0` at 15:10:50 and the speaker was dark by roughly
16:00 — under fifty minutes, with nothing armed and nothing held. That matches the owner's
account of normal use (see the standby section: the speaker sleeps once every program lets
go) and makes this release the fix, with `sleep_after_stop` the fallback for when it is not.

Two things the run does not settle. The window is loose: foobar was closed at 15:56, so
whether the speaker went dark before or after that is unknown, and a tighter reading needs a
session left running after the stop. And the time is not a constant — an earlier observation
put it near twenty minutes, while the owner remembers hours, which fits a firmware that steps
down through several states rather than one timeout. The contrast that does hold is with the
failure case: the night the M5 stayed lit until morning, the session had ended
`stop=unreachable holding=1`. What separates the two is whether the stop landed, not whether
the host was still running.

## No AVTransport renderer

The tested M5 exposes no standard UPnP MediaRenderer or AVTransport service. Ports `7676`,
`8080` and `55001` were open; `9197` was closed. Do not restart generic `foo_out_upnp` work
without evidence from different firmware.

## Still unknown

- Exact official multi-queue request bodies and timing.
- Whether `cp` blocks the share/DLNA path.
- A reliable URL/PCM speaker event that always corresponds to audible start.
- What bounds the speaker's prebuffer. It is not a duration, and it is not a plain byte
  count either; a lower bitrate lengthened the delay by less than the bitrate ratio.
- How small the host buffer can get. Its 4.0 s floor is the largest single term of the six
  seconds and was chosen, not measured; nothing has tested where the pipe starts to starve.
- Whether the M5 returns to standby by itself after a clean stop, and if so whether it arms
  a sleep timer to do it. One sample taken hours after a hard-killed session read
  `sleepoption=off` with the LED still on, which argues against self-arming but does not
  settle it: a fired timer reads the same as one that never existed, so only a countdown
  observed while the speaker is idle and still lit would prove the mechanism. Now testable:
  a session that ends with `stop=sent holding=0` is the clean stop this question needs, and
  the answer is whatever the LED does overnight with `sleep_after_stop` left at 0.
- Whether `SetPlaybackControl pause` releases the speaker's HTTP pull, or only stops the
  transport while it keeps the connection. `holding=<count>` reads the local end of that,
  which is the same socket seen from this side, but the speaker's own view is unmeasured.

## Safety and acceptance

- Keep control and HTTP/DMS ports inside the trusted LAN.
- Do not treat an `ok` response as audible playback.
- Do not run competing control probes during PCM playback.
- A transport candidate is accepted only after a complete 3-5 minute track at wall-clock
  speed, stable seekbar, second track, pause/resume, stop/change and clean helper/FFmpeg
  shutdown on the physical M5.
