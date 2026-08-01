# WAM event listener

`wambridge-events` is a standalone diagnostic tool for TCP `55001`. It keeps a reader open
and sends a harmless `GetFunc` request so the firmware begins returning responses and
unsolicited events.

## Important topology rule

Do not run this tool, `wamtap sniff` or another 55001 probe beside active `pcm_cli` playback.
The tested M5 effectively allows one useful control session during that path; a second
listener can compete with the player and produce timeouts.

Integrated PCM playback uses one `WamEventConnection` for both commands and events. The
standalone listener's separate writer is acceptable only as an isolated diagnostic, not as
the playback architecture.

## Usage

```powershell
wambridge-events --device M5
wambridge-events --device M5 --raw
wambridge-events --device M5 --duration 120
wambridge-events --device M5 --client-uuid <stable-uuid>
```

Without `--client-uuid`, the tool prints a temporary UUID at startup.

## Event interpretation

- `DMSAddedEvent`: DMS registration accepted.
- `MediaBufferStartEvent` / `MediaBufferEndEvent`: share-path buffering.
- `StartPlaybackEvent`: trustworthy confirmation for share/DLNA playback.
- `MusicPlayTime`: position and total length when firmware provides it.
- `MusicInfo` and `PlayStatus`: diagnostics only; they can be stale or false.
- `ErrorEvent`: preserve `errcode` and `errCode`; correlate before treating it as the active
  command's failure.

For URL/PCM, a matching `StartPlaybackEvent` is useful when it appears but is not a hard
startup gate. Audibly playing runs have completed the helper startup without that event
before the former timeout.

The listener sees responses and events. It does not capture the exact outgoing request made
by another client.
