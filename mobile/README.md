![DLNA](https://img.shields.io/badge/DLNA-48A842?logo=dlna&logoColor=fff&style=for-the-badge) ![Android](https://img.shields.io/badge/Android-3DDC84?logo=android&logoColor=fff&style=for-the-badge)

# WAM Bridge Mobile Adapter

Android-first adapter for exposing Samsung WAM speakers to mobile players without changing the existing WAM Bridge playback/research code.

## Scope

- Uses measured WAM behavior as a protocol specification, not as a code dependency.
- First target: Neutron Music Player -> UPnP/DLNA -> Samsung Shape M5.

## Current state

The Android adapter provides:

- WAM speaker autodiscovery via SSDP with prefix-aware LAN fallback;
- UPnP MediaRenderer services: AVTransport, RenderingControl and ConnectionManager;
- a local WAV/LPCM proxy handed to the M5 through `SetUrlPlayback`;
- safe first-start volume capped at M5 raw step `3`;
- idle/session release so stopped playback does not keep the M5 awake;
- a Quick Settings tile: tap toggles the renderer, long-press opens settings;
- optional launcher-icon hiding;
- an M5-style app/renderer icon exposed through UPnP for players such as Neutron;

Physical phone + M5 playback through Neutron is confirmed.

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
