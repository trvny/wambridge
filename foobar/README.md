![foobar2000](https://img.shields.io/badge/foobar2000-000?logo=foobar2000&logoColor=fff&style=for-the-badge) ![C++](https://img.shields.io/badge/C%2B%2B-00599C?logo=cplusplus&logoColor=fff&style=for-the-badge)
# Foobar2000 output

`foo_out_wam` exposes `Samsung M5 (Wi-Fi)` as a foobar2000 2.x x64 output. It sends
foobar's decoded `f32le` PCM to the bundled helper, encodes FLAC and offers the stream to
the M5 through local HTTP and `SetUrlPlayback`.

The component is merged on `main` and validated on the physical M5. PR #21 passed the full
normal-speed track and transition checklist on 2026-08-02; later measurements kept FLAC as
the default, validated WAV transport, and reduced the default extra host buffer to zero.
Support outside the measured physical M5 (`SPK-WAM550`) remains experimental. WAM551 is
an intended M5-family target, but is not yet hardware-validated.

## Requirements

- foobar2000 2.x x64
- a saved WAM device profile, normally `M5`
- speaker and PC in the same LAN

FFmpeg is bundled inside the component helper artifact; a source checkout and Python virtual
environment are not required after installation.

## Install a development build

Download `foo_out_wam-x64` from a successful Build workflow, extract it and open
`foo_out_wam.fb2k-component` with foobar2000. Then select:

```text
Preferences -> Playback -> Output -> Samsung M5 (Wi-Fi)
```

Prefer an artifact whose name or accompanying workflow identifies the tested commit SHA.
ZIP member timestamps are UTC; installed Windows timestamps on the test machine are UTC+2.

## Configure

Open:

```text
Preferences -> Playback -> Output -> WAM Bridge
```

The page edits the existing `%LOCALAPPDATA%\WAMBridge\foobar.ini`, so existing manual
configuration stays compatible. It exposes the device profile, stream format, startup
volume, hardware volume routing and limits, startup silence, extra buffer, sleep-after-stop,
diagnostics and the optional PCM helper override, and follows foobar2000's dark mode.
Runtime and UI share the same validation rules. If an existing known INI value is invalid,
the page shows its effective fallback and enables Apply; saving normalizes that stale entry.
Reset returns the page to component defaults and removes redundant default-valued keys.

The file can still be edited directly:

```ini
[wambridge]
device=M5
volume=3
```

`WAMBRIDGE_*` environment overrides still take precedence over values saved by the page;
the page calls this out when one is active. Changes take effect on the next playback
session.

The M5 uses raw volume steps `0..30`; `3` is roughly 10 percent. Model-aware percentage
conversion is not implemented.

## WAM controls

The installed component adds:

```text
Playback -> WAM Bridge -> Emergency stop
Playback -> WAM Bridge -> Stop & mute
Playback -> WAM Bridge -> Start sleep timer
Playback -> WAM Bridge -> Cancel sleep timer
Playback -> WAM Bridge -> Volume up
Playback -> WAM Bridge -> Volume down
Playback -> WAM Bridge -> Volume to safe level
```

Commands run through a serialized bundled control helper and report completion or errors in
the foobar console. `Start sleep timer` uses the existing `sleep_after_stop` preference as its
delay; when playback is active it is routed through the running PCM helper instead of opening a
second control connection. `Cancel sleep timer` clears the pending timer.

## Current output behavior

- One helper session owns one PCM stream.
- The first speaker HTTP request owns FFmpeg; duplicate requests are refused.
- PCM queued locally, in the pipe write and submitted to the helper is included in output
  capacity and latency.
- Pause keeps the stream alive with paced silence and shifts the host clock anchor.
- Seek, stop and format change restart the helper so stale PCM cannot enter the next stream.
- Helper logs and protocol errors are mirrored to the foobar console.
- A helper crash invalidates the output instead of silently respawning forever.

## Timing findings

The speaker-facing HTTP stream needs no FFmpeg `-re`: TCP backpressure converges toward
real time.

Foobar's position is separate. Two measured failures were:

- a hard `StartPlaybackEvent` gate filled the four-second capacity and froze the seekbar,
- resetting the real-time anchor to each pipe-write catch-up let foobar advance at about
  94x and open later tracks immediately.

The merged output starts one cumulative clock at `WAMBRIDGE AUDIO_STARTED`, shifts it only
for pause and caps played frames by submitted frames. URL/PCM does not abort because a matching
`StartPlaybackEvent` is absent; the event listener remains active for diagnostics.

`AUDIO_STARTED` is a transport anchor, not proof of audible sound. New timing or transport
changes still require physical M5 validation.

## Expected helper markers

```text
WAMBRIDGE STREAM_REQUESTED
WAMBRIDGE ENCODER_STARTED
WAMBRIDGE READY
WAMBRIDGE AUDIO_STARTED
WAMBRIDGE PLAYING volume=3
```

## Physical M5 checklist

Before merging an output candidate:

1. Start at raw volume step `3` or lower.
2. Play one complete 3-5 minute track at wall-clock speed.
3. Keep the seekbar aligned with audible playback.
4. Play a second same-format track without instant skip or stale audio.
5. Test early and normal pause/resume.
6. Test stop, seek and track change.
7. Confirm no extra FFmpeg/helper processes or abandoned speaker sockets remain.
8. Confirm errors are visible in the foobar console.

## Manual helper test

```powershell
cmd /d /c "ffmpeg -hide_banner -loglevel error -i C:\Music\test.opus -f f32le -acodec pcm_f32le -ar 48000 -ac 2 - | wambridge-pcm --device M5 --sample-rate 48000 --channels 2 --sample-format f32le --format flac --volume 3"
```

Do not run a separate `wambridge-events` or `wamtap` listener during this test.
