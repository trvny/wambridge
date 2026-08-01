# foobar2000 component design

Design sketch for the control surface of `foo_out_wam`. Written before implementation so the
work can be picked up by anyone, including a different assistant. Nothing here is built yet;
the transport layers it depends on are all merged and verified on a physical M5.

## What already exists

- `foo_out_wam.cpp` implements `output_v6` and feeds decoded PCM to `wambridge-pcm.exe`.
- Configuration is read from `%LOCALAPPDATA%\WAMBridge\foobar.ini`. There is no UI at all.
- `wambridge-share`, `wambridge-events`, `wambridge-pcm` and the radio bridge are working
  command-line entry points.

## What this adds

A control surface inside foobar, in two stages. Stage one is menu commands plus a
preferences page, because it works in any layout and needs no drawing code. Stage two is a
dockable panel built on the same logic layer, so it becomes a rendering job rather than a
rewrite.

## Menu commands

Registered under **Playback**, through `mainmenu_commands` with
`mainmenu_groups::playback`.

| Command | Behaviour |
| --- | --- |
| Emergency stop | Stop playback, unmute, restore the saved volume. Must survive timeouts. |
| Standby | Stop, mute, let the speaker sleep. |
| Volume up / down | One raw step, `0..30`. |
| Volume to safe level | Jump to the configured startup volume. |
| Play TuneIn preset ▸ | Submenu built from the speaker's stored presets. |

Two rules the transport work made non-negotiable:

- **A timeout is not a failure.** The firmware answers late and often with an unrelated
  event. Emergency stop must retry rather than report failure on the first timeout, and it
  must never leave the speaker muted because a reply did not arrive.
- **Set the "touched" flag before the mutation, never after.** A command whose reply is lost
  may still have been applied. `SpeakerState` in `share_cli.py` is the reference.

Emergency stop exists because the device really does wedge: a bad stream can leave TCP
`55001` unresponsive for tens of seconds, and submode `cp` swallows local playback until the
speaker is power-cycled. The command should say so plainly when it detects that state
instead of silently failing.

## Preferences page

`preferences_page_v3` under Playback, replacing `foobar.ini`:

- speaker alias or IP, with a discovery button reusing `discovery.py`
- startup volume and a maximum startup volume, both raw `0..30`
- output format, defaulting to FLAC
- path to the Python bridge, if not bundled
- a "test connection" button reporting model, firmware and current submode

Keep reading the old `foobar.ini` for one release so existing setups do not break.

## Status and events

The component should hold one persistent reader on TCP `55001` for the whole session,
reusing `wam_events.py`. It provides:

- `StartPlaybackEvent` as the only trustworthy confirmation that audio started,
- `ErrorEvent` with a real error code for the status line,
- `user_identifier` on every event, which distinguishes our own commands from someone
  else's.

That last point drives a behavioural rule worth keeping: **when a human changes volume from
the Samsung app or the speaker's buttons, yield rather than fight**. During protocol capture
a volume limiter and a person ended up in a visible tug of war, and the fix is to compare
`user_identifier` against our own client UUID. Three buckets, not two: our own UUID, a
foreign UUID, and the literal `public` used for unattributed broadcasts.

## Radio

Two paths, and the choice matters to the user:

- **Native TuneIn presets** play autonomously and keep going when the PC is switched off.
  Driving them from here is unverified: `SetPlayPreset` switches submode to `cp` and answers
  long after the timeout, but no application available on this machine can confirm audio.
- **Proxy through FFmpeg and the local HTTP server** works for every station regardless of
  protocol and is confirmed by ear, but needs the PC running.

Offer both, label the difference honestly, and never hand a remote URL straight to
`SetUrlPlayback`. HTTPS fails, Ogg is silent, and HLS wedges the control port.

## Dockable panel, later

A `ui_element` over the same logic: volume slider, station list, status line, emergency
button. No new protocol work should be needed by then.

## Order of work

1. Extract the speaker control calls the component needs into one small layer, so menu,
   preferences and the later panel share it.
2. Menu commands, starting with emergency stop and standby.
3. Preferences page, still reading the legacy ini.
4. Persistent event reader wired to a status line.
5. Radio submenu.
6. Dockable panel.

Steps 1 to 4 need no speaker to develop against; only the last two do.
