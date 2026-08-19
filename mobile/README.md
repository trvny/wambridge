# WAM Bridge Mobile Adapter

Android-first adapter for exposing Samsung WAM speakers to mobile players without changing the existing WAM Bridge playback/research code.

## Scope

- Lives under `mobile/` apart from its isolated mobile-only CI workflow.
- Does not modify or depend on `src/` or the foobar2000 output component.
- Uses the measured WAM behavior as a protocol specification, not as a code dependency.
- First target: Neutron Music Player -> UPnP/DLNA -> Samsung Shape M5.

## Current PoC

The Android app now contains:

- SSDP discovery as `WAM Bridge · M5`;
- UPnP MediaRenderer services: AVTransport, RenderingControl and ConnectionManager;
- a local HTTP proxy handed to the M5 through `SetUrlPlayback`;
- one persistent WAM `55001` control connection for the active adapter;
- WAV pass-through and LPCM `audio/L16` -> endless WAV wrapping;
- a safe first-start volume cap at M5 raw step `3`;
- foreground-service lifetime and a debug APK artifact from mobile CI;
- optional launcher-icon hiding with a Quick Settings recovery tile.

The desktop Python/foobar transport is untouched.

## First physical test

1. Install the debug APK from the `wambridge-mobile-debug` CI artifact.
2. Enter the M5 IPv4 address and use `Save + test M5` while the renderer is stopped.
3. Start the UPnP renderer.
4. In Neutron, open **Settings -> Output To** and select **WAM Bridge · M5**.
5. Prefer **WAV** for the first run if Neutron exposes it under the Format gear. The screenshot's **LPCM / 44100 / Stereo / ∞ Close** setup is also a sensible first attempt: the PoC accepts `audio/L16` and wraps it into the endless-WAV form already proven on the M5.
6. Leave **Change device volume** off for the first audio test. The adapter already caps the first M5 start at raw volume step `3`; remote volume mapping can be tested after transport works.
7. `Send tags` may stay enabled; metadata is accepted but does not gate playback.
8. Play a normal 44.1 kHz stereo track, then test stop/pause and a second track.

Neutron's separate **Media Renderer (UPnP/DLNA)** switch under its Network settings makes Neutron itself a renderer. It is not required when Neutron is the source sending to WAM Bridge, so leave it off for this test.

## Architecture

```text
Neutron / other UPnP-DLNA player
            |
            v
 Android MediaRenderer facade
            |
            v
      local WAV proxy
            |
            v
  Samsung WAM control client
            |
            v
        Shape M5
```

## Launcher visibility

The launcher entry is a dedicated `activity-alias`, so it can be disabled without disabling the app or foreground renderer service. A WAM Bridge Quick Settings tile opens the real activity directly and can restore the launcher entry later.

## PoC gate

Keep the PR draft until a physical phone + M5 proves:

1. Neutron discovers `WAM Bridge · M5`.
2. The M5 requests `/stream` and produces audible audio.
3. Playback survives a complete track and a second track.
4. Stop/pause do not leave the speaker or phone holding stale sessions.
5. Hiding the launcher entry leaves a usable recovery path.

Only then promote the adapter beyond PoC or start extracting iOS-shareable protocol code.
