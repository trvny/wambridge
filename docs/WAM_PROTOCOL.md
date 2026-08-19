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

### Two playback paths, and the mistake of treating them as one

Every format claim in this file belongs to exactly one of these, and they behave nothing alike:

| path | who decodes | formats | needs a PC |
| --- | --- | --- | --- |
| `--radio-play`, foobar output, the share path | **our FFmpeg**, via the local server | anything FFmpeg reads - Ogg, HLS, HTTPS | yes |
| a remote URL handed straight to `SetUrlPlayback`, TuneIn presets, the physical Radio button | **the speaker** | plain HTTP, MP3/AAC | no |

The station packs in `src/wambridge/station_packs.py` are the first kind. Their Ogg and `.m3u8`
entries are correct and play, because the speaker never sees those bytes. Reading the direct-path
format limits and concluding those packs are broken is a mistake made here on 2026-08-19, one
edit away from "fixing" working code.

Re-measured the same evening, on a speaker known responsive because it was mid-stream on plain
MP3 when the switch was sent: `http://41.dktr.pl:8000/trojka.ogg` handed straight to
`SetUrlPlayback` is accepted, answers `UrlPlayback`, moves to `submode=cp` - and is silent. The
same station as plain MP3, `http://stream3.polskieradio.pl:8954/`, plays. So the Ogg line below
still holds; what it never meant is that Ogg cannot reach this speaker at all.

An earlier attempt at the same test was thrown away rather than recorded: the front panel was
still on Radio from a physical button press, so silence proved nothing. `SetFunc bt` then
`SetFunc wifi` cleared it, and the known-good stream was audible immediately afterwards - the
first time that recovery was needed for real rather than staged.

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

### Three sources, none of them automatically right

Everything known about this speaker came from one of three places, and it is worth naming them
because the newest one is the easiest to over-trust.

1. **Traffic measured against the physical M5**, including traffic produced by Samsung's own
   desktop app while someone watched. This is where `playertype=myphone`, the flat `objectid`,
   the queue-based playback and the 0-30 volume scale came from. Strongest evidence there is.
2. **The mobile app's code**, decompiled 2026-08-19. Broad and precise about *shape* - exact
   arguments, exact endpoints - and silent about whether this firmware answers at all.
3. **This project's own implementation**, which in places went past both.

The third one is not a poor relation. The clearest example is the release at the end of a
session: nothing in either app suggested it, and it exists because a lit speaker was measured
staying lit. A build that sent no release was still lit 33 minutes after its last audio; a
session ending `WAMBRIDGE STOPPED stop=sent sleep=off holding=0` was dark 17 minutes later. That
behaviour is ours, it is measured, and no amount of official-looking code should displace it.

So the rule for adopting anything from the vocabulary below: **replace an improvisation when the
official way is demonstrably better on this hardware, not because it is official.** Sometimes it
will be - a documented argument beats a guessed one. Sometimes the app is doing something for
reasons that do not apply here, and sometimes it is simply worse: it has a whole UI to keep
responsive and no interest in a bit-exact PCM clock. Where two sources disagree, the measurement
wins and the disagreement is worth a line in this file rather than a silent edit.


## What the official app can say, and we cannot

Read this section differently from the rest of the file: **nothing here is measured.** Every
other claim in this document came off a physical `SPK-WAM550`; this one came out of a
decompiled APK and is a list of things worth *trying*, not things known to work. A command
appearing here does not mean this firmware answers it - `GetFeature` is in the list and is
already known to answer with silence on `WAM550WWB-3117.1`.

**Provenance.** `com.samsung.roomspeaker3` version 4164 (January 2026), decompiled 2026-08-19.
The templates live in one obfuscated constants class, `h2.b`, as `String.format` patterns.
Note before trusting the binary further: the copies available from mirrors are signed
`CN=r.savuschuk`, a self-signed personal key rather than a Samsung corporate one, and the same
fingerprint appears on two independent mirrors, so it is the real distribution key rather than
a mirror stamping its own. The Java is Samsung's; who builds and signs it is a separate
question. Nothing from it was installed or run.

**242 commands, of which this project already uses 26.** The gap is not a to-do list - most of
it is grouping, multi-channel test tones, alarms and EQ that a foobar output has no use for.
Four clusters are worth attention:

### Presets and radio browsing - the gap that is actually blocking something

`DEVELOPMENT_STATUS.md` records that a preset can only be recalled by a number the listener
already knows, because nothing lists what the speaker holds. The app does list it, and pages
through it:

```text
CPM?cmd=<name>GetPresetList</name>
       <p type="dec" name="startindex" val="%s"/>
       <p type="dec" name="listcount"  val="%s"/>

CPM?cmd=<name>SetPlayPreset</name>
       <p type="dec" name="presetindex" val="%s"/>
       <p type="dec" name="presettype"  val="%s"/>

CPM?cmd=<name>SetRemovePreset</name>
       <p type="dec" name="presetindex" val="%s"/>

CPM?cmd=<name>GetCurrentRadioList</name>
       <p type="dec" name="startindex" val="%s"/>
       <p type="dec" name="listcount"  val="%s"/>

CPM?cmd=<name>GetUpperRadioList</name>
       <p type="dec" name="startindex" val="0"/>
       <p type="dec" name="listcount"  val="%s"/>
```

**Correction, same day.** An earlier revision of this section claimed the pagination and the
`presettype` argument were missing here. Both were already implemented: `src/wambridge/tunein.py`
selects the service first, pages `GetPresetList`, and sends `presettype` with `SetPlayPreset`,
all reachable as `wambridge --tunein-list` and `--tunein-play`. Listing saved presets is a solved
problem and the open list said otherwise for longer than it was true.

What the app has and this project does not is on either side of that:

- **Writing presets.** `README.md` says changing the preset list "still belongs to Samsung's
  plugin because no reliable write API is known". The app has `SetSavePreset`,
  `SetRemovePreset` (`presetindex`) and `SetMovePreset`. Untested here, but they are not
  unknown any more.
- **Finding stations that are not already saved.** The catalogue really is a tree, and it has
  now been walked - see "Walking the radio tree" below for the measured shapes, and for the
  price of doing it. Nothing in this project browses it yet; only what the speaker already
  holds. `GetGenreStations`, `SearchQuery` and `GlobalSearch` sit beside it, untried.

### A second power lever, on the port we already hold open

`SetSleepTimer` has been treated here as the only power control the firmware answers. The app
also has, on `UIC` rather than `CPM`:

```text
UIC?cmd=<name>SetNetworkStandByMode</name>
       <p type="str" name="networkstandbymode" val="%s"/>
UIC?cmd=<name>GetNetworkStandByMode</name>
```

**Measured 2026-08-19, and it answers.** A plain read against the physical M5 at rest:

```xml
<method>NetworkStandByMode</method>
<response result="ok"><networkstandbymode>on</networkstandbymode></response>
```

The response method drops the `Get`, the same way `GetApInfo` answers as `ApInfo`.
`SetNetworkStandByMode` takes a `str` argument `networkstandbymode` and has not been tried.

**Read that for exactly what it is.** A setting called `NetworkStandByMode` exists and currently
reads `on`. That is all. It does **not** follow that writing it puts the speaker to sleep: in
consumer audio a name like this usually governs what the network does *while* the device is in
standby - whether the radio stays up to be woken - rather than being the thing that enters
standby. An earlier revision of this file called it "a second power lever", which was one
inference too many on a single read.

What would settle it, in order: `SetNetworkStandByMode` with the value already read back
(a no-op that proves the write is accepted and what the argument alphabet looks like), then the
other value with `GetMute` watched as the LED proxy this file already documents, and the idle
interval from the standby section as the yardstick. Until then it is a setting, not a lever.

### Content providers and search

`BrowseMain`, `Browse`, `GetCpList`, `GetCpInfo`, `GetCpSubmenu`, `SetSelectCpSubmenu`,
`SetCpService`, `GlobalSearch`, `SearchQuery`, `SearchUniversalQuery`, `GetStationData`,
`GetGenreStations`, `SetCreateNewStation`, `SetDeleteStation`, `BookmarkStation`. This is the
whole TuneIn-and-friends surface. Most of it needs a service selected first.

Measured the same day, all read-only:

- `GetCpList` answers with `liststartindex` and `listcount` - **not** `startindex`, which is
  what the preset call takes; guessing the wrong one returns `errcode 53`,
  "Input parameter/parameters not found".
- It lists **20 providers** (Pandora, Spotify, Deezer, Qobuz, Tidal HiFi, SiriusXM, Amazon
  Prime and so on), every one `signinstatus=0`, while reporting `listtotalcount=24`. TuneIn is
  **not among them** - it is reached through the radio surface, not as a content provider.
- `GetPresetList` without a service selected returns `errcode 67`, "No service Selected", which
  is why `tunein.py` calls `select_tunein()` first.
- `GetRadioInfo` answers `ok` with `playstatus=stop` and `cpname=Unknown` when nothing is
  selected.
- Anonymous reads come back with `user_identifier=public`, and errors arrive as child elements
  (`<errcode>`, `<errmessage>`), both as this file already records.

### Sound shaping

Bass, treble, balance, DRC, woofer and rear levels, a seven-band EQ with saved custom modes,
and `GetAudioQuality`/`SetAudioQuality`. None of it is needed to play audio.

**Measured 2026-08-19, and only part of it is reachable on this speaker.** Read against the
physical M5 by `tools/wam-probes/probe_rung1_reads.py`:

```text
GetEQBass         eqbass=0            GetWooferLevel    (silent)
GetEQTreble       eqtreble=0          GetRearLevel      (silent)
GetEQBalance      eqbalance=0         GetAudioQuality   (silent)
GetEQDrc          eqdrc=on
GetCurrentEQMode  presetindex=0, presetname=None, eqvalue1..7=0
Get7BandEQList    5 modes: None, Pop, Jazz, Classic, "1 uzytkownika"
```

The silent three are the point. Woofer and rear levels belong to a soundbar with satellites and
`GetAudioQuality` to a different product tier; the M5 has one enclosure and its firmware simply
does not answer. So the 242-command vocabulary lifted from the app is the *app's* vocabulary,
not this speaker's, and the only way to tell the two apart is to send the command. The same
sweep found `SpkInGroup` silent while `GetGroupName` answers with an empty name.

Note also that `Get7BandEQList` came back with a Polish preset name. The speaker localises what
it returns, so a client must not match on these strings.

### Walking the radio tree

**Measured 2026-08-19** with `tools/wam-probes/probe_radio_browse.py` against the physical M5.
The command shapes below are not guessed: they are the format strings lifted out of the
official app's DEX, so the parameter names are the app's own.

```text
CPM?cmd=<name>SetSelectRadio</name>                    (no arguments - enters TuneIn)

CPM?cmd=<name>GetCurrentRadioList</name>               (the level the cursor is on)
       <p type="dec" name="startindex" val="%s"/>
       <p type="dec" name="listcount"  val="%s"/>

CPM?cmd=<name>GetSelectRadioList</name>                (descend into one entry)
       <p type="dec" name="contentid"  val="%s"/>
       <p type="dec" name="startindex" val="0"/>
       <p type="dec" name="listcount"  val="%s"/>

CPM?cmd=<name>GetUpperRadioList</name>                 (back up one level)
       <p type="dec" name="startindex" val="0"/>
       <p type="dec" name="listcount"  val="%s"/>

CPM?cmd=<name>GetStationData</name>                    (detail of one station)
       <p type="dec" name="selectitemid" val="%s"/>
```

An earlier revision of this file said `SetSelectRadio` was what descended into the tree. It is
not; it only selects the provider. Descending is `GetSelectRadioList` with a `contentid`, and
the name being a `Get` hides that it moves the cursor.

**It is a tree, and the cursor lives in the speaker.** The root answers `<category isroot="1">`
with twelve folders, and there is no path in the request - every call is relative to wherever
the speaker's cursor currently is:

```text
[ 0] Favorites   [ 3] Trending      [ 6] Talk           [ 9] By Location
[ 1] Local Radio [ 4] Recommended   [ 7] Sports         [10] By Language
[ 2] Recents     [ 5] Music         [ 8] News & Talk    [11] Podcasts
```

Two shapes of `menuitem`, distinguished by `type`:

```xml
<menuitem type="0">                              <!-- folder -->
    <title>Local Radio</title>
    <contentid>1</contentid>
</menuitem>

<menuitem type="2" cat="stations">               <!-- station -->
    <thumbnail>http://cdn-profiles.tunein.com/s87779/images/logot.jpg</thumbnail>
    <description>Hity Na Czasie</description>
    <mediaid>s87779</mediaid>
    <title>ESKA Krakow 97.7 (Christian Contemporary)</title>
    <contentid>0</contentid>
</menuitem>
```

`contentid` is the index **within the current page**, not a stable identifier - it restarts at 0
on every level. The stable one is `mediaid`, which is the TuneIn station id. `Local Radio`
answered `totallistcount=90` for a `listcount` of 30, so paging is real and the geolocation is
the speaker's, not the client's: the local list came back Polish.

`GetStationData` is the interesting one. For `selectitemid` = the station's `contentid` it
returns, besides the title and artwork, a ready-to-play URL:

```xml
<stationurl>http://opml.radiotime.com/Tune.ashx?id=s87779&amp;partnerId=...&amp;serial=...</stationurl>
```

That is a plain HTTP stream URL carrying the speaker's own TuneIn partner id and serial. It
means browsing does not have to end in a preset write: browse, read the station URL, hand it to
`SetUrlPlayback`, which this project already implements. The whole of rung 3 can stay untouched.
The `partnerId` and `serial` are the speaker's credentials with TuneIn and are redacted here.

**Browsing costs nothing, and an earlier revision of this section said otherwise.** That
revision claimed `SetSelectRadio` strands the speaker in `submode=cp`. It does not. Re-measured
deliberately the same evening, one command at a time:

```text
start                                    submode=dlna
SetSelectRadio                           submode=dlna
GetCurrentRadioList                      submode=dlna
GetSelectRadioList (descend)             submode=dlna
GetStationData                           submode=dlna
GetUpperRadioList                        submode=dlna
+80 s idle                               submode=dlna
```

The whole walk leaves the submode untouched. The `cp` seen earlier was already there before the
first probe ran; nothing established that browsing caused it, and the causal claim was written
down anyway. Browsing is a genuinely free read.

**What does switch the speaker to `cp` is `SetUrlPlayback` - and that is normal, not a fault.**
This file has said so since the "Submode" section near the top, and it is worth repeating here
because the wrong version above was written straight past it. Measured: an ordinary internet
stream handed to `SetUrlPlayback` played audibly while `GetFunc` reported `submode=cp`
throughout, and a second `SetUrlPlayback` switched streams without leaving `cp`.

Two reading traps come with that submode, both measured:

- `GetPlayStatus` returns `function` and `submode` but **no `playstatus` element** while in `cp`.
  Code that decides "is it playing" from that field is blind here.
- `GetMusicInfo` answers `errCode: Wifi Sub Mode is CP`. That is the metadata call not applying
  to content-player playback, not a failure of the playback.

**Leaving `cp` takes two commands, not a power cycle.** Measured from a speaker that was in `cp`
with a stream running:

```text
SetFunc bt      -> function=bt    submode=""
SetFunc wifi    -> function=wifi  submode=dlna
```

`SetFunc` had looked useless earlier only because it was aimed at `wifi` while the speaker was
already on `wifi` - it was told to become what it already was. A round trip through another
source is what moves it. What still refuses to shift `cp` in place: `SetPlaybackControl stop` on
both `CPM` (`"Current track token is empty."`) and `UIC` (`result="ng"`).

So `samsung.py:require_local_playback_mode` saying "no command clears this state - power-cycle
the speaker and retry" is wrong on both halves, and `docs/DEVELOPMENT_STATUS.md` had already
recorded the rule it breaks: *no URL startup gate or power-cycle advice may depend on that
submode*.

**Still untested, and the reason the share path keeps its guard.** Everything above used a
stream from the public internet. Both guarded call sites concern a URL pointing at *our own*
HTTP server (`cli.py`) or the DLNA share path (`share_cli.py`). That `cp` blocks *those* remains
plausible and unmeasured - the DLNA renderer in particular wants `submode=dlna`, which is the
likeliest reason a phone pushing over DLNA finds a speaker in `cp` silent.

**One more failure mode, worth expecting.** Under a fast series of CPM calls the whole CPM
endpoint wedges: first it answers lists with `totallistcount=0`, then it stops answering `CPM`
entirely for 20-30 seconds while `UIC` keeps replying normally, then it recovers on its own. An
empty level is therefore not proof of an empty category, which is why the probe retries before
believing it.

### What can and cannot go under the physical Radio button

**Measured 2026-08-19.** The button plays presets, presets are TuneIn catalogue entries, and
that chain has no opening for a URL of our own. Every command in the app that creates or
bookmarks a station takes an index into the provider's own list:

```text
CPM?cmd=<name>SetCreateNewStation</name><p type="dec" name="itemid" val="%s"/>
CPM?cmd=<name>BookmarkStation</name><p type="dec" name="selectitemid" val="%s"/>
CPM?cmd=<name>AddToFavorite</name><p type="dec" name="selectitemid" val="%s"/>
CPM?cmd=<name>SetSavePreset</name>                     (no arguments - saves what is selected)
```

No command anywhere in the 242 accepts a stream URL. So a station from a personal playlist can
be played with `SetUrlPlayback`, and can never be placed under the physical button. If the
button matters, the station has to exist in TuneIn.

**Which is survivable, because TuneIn is not uniformly unplayable.** Ten stations resolved from
`Local Radio` through `GetStationData` and fetched from a PC:

```text
plain HTTP   5     e.g. PR3 Trójka 99.7 -> http://stream3.polskieradio.pl:8954/
HLS          3     e.g. ESKA Kraków     -> https://liveradio.timesa.pl/2070-1.aac/playlist.m3u8
HTTPS        2
```

Half still hand out a plain HTTP stream, which this firmware plays.

**And the stations actually in the presets are mostly fine, which kills the tidy explanation.**
Resolved by station id straight from TuneIn, using the speaker's own partner credentials:

```text
s15984   PR3 Trójka 98.8   -> http://stream3.polskieradio.pl:8954/
s118200  Czwórka           -> http://stream3.polskieradio.pl:8956/listen.pls
s44392   PR4 Czwórka 92.0  -> http://stream3.polskieradio.pl:8906
s25176   Radio RAM 89.8    -> http://stream2.prw.pl:8000/radioram
```

**These are plain HTTP, which is not the same as verified playing, and an earlier revision of
this table said "plays" against each one.** Nothing here was listened to; the column was a
classification of the URL scheme. Since then, `s15984`'s address has been checked properly and
is worse than it looked: `curl` gets `200 text/html` from it - a Shoutcast status page, not
audio - and FFmpeg refuses it outright with `Stream ends prematurely at 0`. Appending the usual
`;stream` or `;` does not help either.

The audible confirmations tonight were all on `;`-suffixed addresses -
`http://stream3.polskieradio.pl:8906/;stream` and `http://mp3.polskieradio.pl:8956/;` - which is
also the form the station pack already carried. Whether the speaker's own player copes with the
bare `:8954/` form is untested; it was assumed, not heard.

`s15984` is the exact entry sitting in preset 0, and **the station is not ruled out** - an
earlier revision of this paragraph said it was, on the strength of the table above before that
table was corrected. With `:8954/` now known to answer `200 text/html` rather than a stream, the
station is back to being a live suspect for `SetPlayPreset` returning `StopPlaybackEvent`.

The evidence available points that way rather than away from it, though it is two data points
and not a finding:

- Trójka, whose resolved address does not stream, is the preset that failed here every time.
- Czwórka, whose resolved address does stream once its `.pls` is read, is the preset that
  started on the first try from `wambridge-mobile`.

What would settle it is hearing the resolved `:8954/` through the speaker's own player, which
has not been done - it was assumed playable because it was plain HTTP, which is exactly the
inference this section had to retract once already.

BBC Radio 1 remains genuinely unplayable here - HLS from TuneIn and HLS in the personal
playlists in `trvny/stuff/playlists` alike - so a preset holding it cannot work whatever else is
fixed.

**And the owner's history closes the question.** Preset playback started roughly **one attempt
in five** even years ago, with the official app and TuneIn both in good health. So
`SetPlayPreset` answering `StopPlaybackEvent` is not a regression, not something this project
broke, and not worth chasing: the speaker's own preset path has always been that unreliable.

Confirmed from both ends the same evening. Every attempt from this machine failed, and then
`czworka` started on the first try from `wambridge-mobile`, whose `SamsungTuneIn.kt` sends the
identical call - `SetPlayPreset` with `presettype` before `presetindex`. Same command, same
speaker, minutes apart, different outcome. The mechanism works; it is the reliability that does
not, and no amount of getting the call right changes that.

Which settles the design. Browsing to a station, reading `stationurl` and handing it to
`SetUrlPlayback` is not merely an alternative to the preset path - it is **more reliable than
the speaker's own**, and it starts on the first try. Preset writing buys nothing except the
physical button, and the button inherits the one-in-five behaviour anyway.

**Overwriting a preset is therefore not a fix.** Swapping a playable station for another
playable station changes nothing while the failure sits in the call, and `SetRemovePreset` has
no undo.

**What the speaker cannot reach directly, it can reach through us.** BBC Radio 1 plays instantly
in a browser and never on this speaker, because the modern stream is HTTPS and HLS and the 2015
firmware takes neither. The local HTTP server this project already runs for foobar is the way
across: fetch upstream, re-serve as plain HTTP. The cost is that the PC or phone has to be
running, which the plain-HTTP stations do not require.

This is the one thing that keeps preset writing on the table. For this project's own playback
it is unnecessary - browse, take `stationurl`, `SetUrlPlayback`. For the physical button it is
the only route there is.

**A trap for anything that browses.** The browse cursor lives in the speaker and **survives
across client processes**. A fresh run that calls `GetSelectRadioList` assuming it starts at the
root will descend from wherever the last run stopped - which produced an empty level and looked
exactly like the CPM wedge described above. Normalise with `GetUpperRadioList` until
`<category isroot="1">` before descending.

### The three presets behind the physical Radio button

`README.md` says the WAM Bridge station list "does not overwrite the three presets selected by
the physical button on the speaker", and that is true of that feature. It is worth writing down
what those three actually are, because they are already visible and the commands to change them
are now known.

Measured 2026-08-19, `wambridge --device M5 --tunein-list` against the physical M5:

```text
0   speaker   98.8 | PR3 Trójka (Variety)
1   speaker   Czwórka - Polskie Radio (Top 40 & Pop Music)
2   speaker   98.8 | BBC Radio 1 (Pop Country)
3   my        BBC Radio 6 Music (Music)
4   my        96.0 | RMF FM (Adult Hits)
...             (12 more of kind `my`)
```

So the speaker's own three are simply the entries of kind `speaker`, and this project already
distinguishes them: `WamPreset.preset_type` maps `speaker` to `1` and `my` to `0`, which is the
`presettype` that `SetPlayPreset` carries. Reading and playing them is solved. Changing which
stations they are is not.

The three write commands, exactly as the app sends them:

```text
CPM?cmd=<name>SetSavePreset</name>                     (no arguments at all)

CPM?cmd=<name>SetRemovePreset</name>
       <p type="dec" name="presetindex" val="%s"/>

CPM?cmd=<name>SetMovePreset</name>
       <p type="dec" name="presetfromindex" val="%s"/>
       <p type="dec" name="presettoindex"   val="%s"/>
       <p type="dec" name="movedirection"   val="%s"/>
```

`SetSavePreset` taking **no arguments** is the whole shape of the feature: it saves whatever is
currently selected, so a station has to be playing or selected before it can be stored. That is
also why browsing matters - `SetSelectRadio`, `GetCurrentRadioList`, `GetSelectRadioList` and
`GetUpperRadioList` are how the app reaches a station that is not already a preset.

**Untested hypothesis, and the reason to be careful.** The list above is one sequence, with the
three `speaker` entries at indices 0-2 and `my` from 3 onwards. If that ordering is the speaker's
own and not a presentation choice by this client, then `SetMovePreset` from an index in the `my`
range to 0, 1 or 2 would promote an existing station into the physical-button set without having
to browse or re-save anything. That would make the whole feature two commands. It is a guess:
nothing has been sent, and `movedirection` has an unknown meaning and an unknown valid range.

Order to settle it, cheapest first, and **dump `--tunein-list` to a file before the first write**
- `SetRemovePreset` takes only an index and there is no undo:

1. Re-read `--tunein-list` after any change; it is the only way to see the result.
2. `SetMovePreset` between two adjacent `my` entries, far from the speaker slots. Harmless if it
   works, informative if it errors, and it settles what `movedirection` wants.
3. Only then a move that crosses into 0-2, which is the interesting one.
4. `SetSavePreset` last, since it needs a selected station and therefore needs browsing first.


### Adopting any of this, safest first

The vocabulary is large and the speaker is the only one there is. This is the order to take
things in, and the reason each rung is where it is. Nothing below has been implemented.

**Rung 1 - reads, no state touched.** A read costs a socket and answers or times out; a command
this firmware does not know simply stays silent. Everything here can be run today, with one
constraint that already applies to every probe in this file: **not while PCM playback is
running**, because a second connection on `55001` competes with the helper and has knocked the
player over before.

- `GetCurrentEQMode`, `Get7BandEQList`, `GetEQBass`, `GetEQTreble`, `GetEQBalance`, `GetEQDrc`
  - the sound-shaping surface, read. `GetWooferLevel`, `GetRearLevel` and `GetAudioQuality` are
  in the app but silent on this speaker.
- `GetSpeakerStatus`, `GetPlayStatus`, `GetAvSourceAll`, `GetLed`, `GetBatteryStatus`,
  `GetGroupName`, `GetAlarmInfo`. `SpkInGroup` is silent.

**Done 2026-08-19** by `tools/wam-probes/probe_rung1_reads.py`; results are in the sections
above. `GetBatteryStatus` answers `batteryrate=50, batterymode=battery` on a mains-powered M5,
which reads like a stub rather than a measurement - do not build on it.

**Rung 1, continued - the radio browse calls.** `SetSelectRadio`, `GetCurrentRadioList`,
`GetSelectRadioList`, `GetUpperRadioList` and
`GetStationData` touch no preset and no setting, but entering the radio surface leaves the
`GetStationData` touch no preset, no setting and - measured - not even the submode. Browsing
belongs in rung 1 after all; the "1b" this list carried for one revision was based on a causal
claim that did not survive being tested.

**Rung 2 - writes that restore themselves.** Read the current value, write, write it back. Safe
because the old value is known before anything changes, and none of it survives being set back.

- `SetNetworkStandByMode` - read `on` first, so there is something to return to.
- `SetEQBass`, `SetEQTreble`, `SetEQBalance`, `SetEQDrc`, `SetWooferLevel`, `SetRearLevel`.

**Rung 3 - writes that change something the listener owns.** The preset list is the obvious one,
and it is the reason `README.md` still hands preset editing to Samsung's app. Before the first
`SetSavePreset`, dump the existing list with `wambridge --tunein-list` and keep it: there is no
undo, and `SetRemovePreset` takes only an index.

- `SetSavePreset`, `SetRemovePreset`, `SetMovePreset`, `SetCreateNewStation`,
  `SetDeleteStation`, `BookmarkStation`.

**Rung 4 - needs hardware we do not have.** Grouping, stereo pairing, multi-channel positioning
and their test tones all assume a second speaker. `SetGroup`, `SetUngroup`, `SetMultispkGroup`,
`SetStereo`, `SetMultichGroup`, `PositionedSpkInGroupMultiCh`, the four `*Testtone*` calls.

**Never, on the only speaker in the house.** These are in the app because the app ships to
service technicians and factories as well as listeners. None of them has a use here and several
have no visible undo:

```text
FactoryReset            SetBuyer                SetSwuServerType      SetUartOnOff
SetShopMode             SetLocale               SetSwuTestServer      SetBtDut
SetDebugMode            SetSpeakerTime          SetManualSpeakerUpgrade
SetAp / SetApManual     SetIpInfo               RegisterDevice / UnregisterDevice
```

`SetAp`, `SetApManual` and `SetIpInfo` reconfigure the speaker's network from underneath the
connection carrying the command, which is how a speaker stops being reachable. `SetBuyer`,
`SetLocale` and the `Swu*` pair point it at different regional and firmware-update servers.
`SetManualSpeakerUpgrade` starts a firmware write. There is no reason to send any of them and
no way to test them safely.


### The full vocabulary

`*` marks the 26 already used or documented by this project.

```text
  AddCustomEQMode               GetEQMode                     RegisterDevice                SetMultiHopSetting
  AddSongBookmark               GetEQTreble                   RemoveFromFavorite            SetMultiPlaybackControl
  AddSongsToMultiQueue *        GetFeature *                  RemoveFromFavoriteCurrentPlayingSetMultispkGroup
  AddSongToQueue                GetFunc *                     RemoveFromLibraryCurrentPlayingSetMute *
  AddToFavorite                 GetGenreStations              RemoveFromListenLaterCurrentPlayingSetNetworkStandByMode
  AddToFavoriteCurrentPlaying   GetGroupName                  RemoveFromPlaylist            SetNewFolderPlaybackControl *
  AddToLibrary                  GetHtsMainInfo                Reset7bandEQValue             SetNewPlaylistPlaybackControl
  AddToListenLaterCurrentPlayingGetHtsMute                    Save7bandEQMode               SetPartyMode
  AddToPlaylist                 GetHtsVolume                  ScrollPlay                    SetPlaybackControl *
  AddToPlaylistCurrentPlaying   GetIcon                       SearchQuery                   SetPlayCpPlaylistTrack
  AddTracksToCpPlaylist         GetKPI                        SearchUniversalQuery          SetPlayFolder
  BanCurrentTrack               GetLed *                      SelectedSpkInGroupMultiCh     SetPlaylistPlaybackControl
  BookmarkStation               GetLinkMateOutput             SelectSpk                     SetPlayPreset *
  Browse                        GetMainInfo                   SendVoiceText                 SetPlaySelect
  BrowseMain                    GetMultiHopCount              Set7bandEQMode                SetPreviousTrack
  Cancel7bandEQMode             GetMultiHopInfo               Set7bandEQValue               SetQueuelist
  CancelDeviceRegistration      GetMultiHopSetting            SetAcmMode                    SetRadioAutoPlay
  CheckRegistrationComplete     GetMusicInfo *                SetAlarmInfo                  SetRearLevel
  ConnectBluetoothSpeaker       GetMusicListByCategory        SetAlarmOnOff                 SetRemovePreset
  CreatePlaylist                GetMusicListByID              SetAp                         SetRepeatMode
  DelAlarm                      GetMusicListByMultiID         SetApManual                   SetRMServerType
  DelCustomEQMode               GetMusicListBySongs           SetAudioQuality               SetSavePreset
  DeletePlaylist                GetMute *                     SetAudioUI                    SetSearchTime
  DelSongsFromMultiQueue        GetMyPlaylists                SetAutoUpdate                 SetSelectAmazonCp
  DelSongsFromQueue             GetNetworkStandByMode         SetBtDut                      SetSelectCpSubmenu
  DisconnectBluetooth           GetPlayStatus *               SetBuyer                      SetSelectRadio *
  EditSpkName                   GetPresetList *               SetChVolMultich               SetSettings
  FactoryReset                  GetRadioInfo *                SetContinueListen             SetSharePlaybackControl *
  Get7BandEQList                GetRearLevel                  SetCpService                  SetShopMode
  GetAcmMode                    GetRepeatMode                 SetCreateNewStation           SetShuffleMode
  GetAlarmInfo                  GetRMServerType               SetDebugMode                  SetSignIn
  GetAlarmSoundList             GetSelectRadioList            SetDeleteStation              SetSignOut
  GetApInfo *                   GetSettings                   SetDeviceInfoForKPI           SetSkipCurrentTrack
  GetApList                     GetShopMode                   SetDms                        SetSleepTimer *
  GetApPasswordInfo             GetShuffleMode                SetEQBalance                  SetSpeakerTime
  GetAudioQuality               GetSleepTimer *               SetEQBass                     SetSpkName
  GetAudioUI                    GetSoftwareVersion *          SetEQDrc                      SetStartApp
  GetAutoUpdate                 GetSpeakerBuyer               SetEQTreble                   SetStereo
  GetAvSourceAll                GetSpeakerStatus              SetEqualizeVolMultich         SetSwuServerType
  GetAvSourceInGroup            GetSpeakerWifiRegion          SetFolderPlaybackByArtistControlSetSwuTestServer
  GetBatteryStatus              GetSpkName *                  SetFolderPlaybackControl      SetTesttoneChVolMultich
  GetBtDut                      GetStationData                SetFunc *                     SetToggleShuffle
  GetBuyer                      GetStereo                     SetGroup                      SetTrickMode
  GetCaptcha                    GetSubSoftwareVersion         SetGroupName                  SetUartOnOff
  GetChVolMultich               GetSwuServerType              SetHtsMultispkGroup           SetUngroup
  GetCpInfo                     GetSwuTestServer              SetHtsMute                    SetUsbPlaybackControl
  GetCpList                     GetUartOnOff                  SetHtsUngroup                 SetUsbRepeatMode
  GetCpPlayerPlaylist           GetUpperRadioList             SetHtsVolume                  SetUsbTrickMode
  GetCpSubmenu                  GetUsbRepeatMode              SetIcon                       SetVoiceText
  GetCurrentEQMode              GetValidAppVersion            SetIpInfo *                   SetVolume *
  GetCurrentMultiQueuelist      GetVolume *                   SetKPI                        SetVVIPMasterSpk
  GetCurrentPlaylist            GetVVIPMasterSpk              SetLed                        SetWooferLevel
  GetCurrentPlayTime            GetWheel                      SetLikeMix                    SpkInGroup
  GetCurrentQueuelist           GetWooferLevel                SetLikeStatus                 StartTesttoneGroupInMultich
  GetCurrentRadioList           GlobalSearch                  SetLikeStatusSelected         StartTesttoneSpkInMultich
  GetDebugMode                  GoLive                        SetLinkMateOutput             StopTesttoneGroupInMultich
  GetDeviceId *                 MoveSongFromMultiQueue        SetLocale                     StopTesttoneSpkInMultich
  GetDmsList *                  PlayById                      SetManualSpeakerUpgrade       UnregisterDevice
  GetEQBalance                  PopupAction                   SetMovePreset                 WhyThisTrack
  GetEQBass                     PositionedSpkInGroupMultiCh   SetMultichGroup
  GetEQDrc                      Promotions                    SetMultiHopPairingMode
```

## Safety and acceptance

- Keep control and HTTP/DMS ports inside the trusted LAN.
- Do not treat an `ok` response as audible playback.
- Do not run competing control probes during PCM playback.
- A transport candidate is accepted only after a complete 3-5 minute track at wall-clock
  speed, stable seekbar, second track, pause/resume, stop/change and clean helper/FFmpeg
  shutdown on the physical M5.
