#pragma once

#include <optional>

// Narrow bridge from the output adapter to the control-helper dispatcher.
// Everything in wam_menu.cpp lives in an anonymous namespace, so routing the
// volume slider to the speaker needs this one declaration rather than an
// include of that translation unit.

namespace wam {

// Ask the speaker for a raw 0..30 level over the 55001 control path.
//
// Calls are coalesced and rate limited by the dispatcher: dragging the slider
// produces one request per pixel, and each request that survived would spawn
// its own control-helper process. Only the newest level matters, so older
// pending ones are dropped rather than queued.
void request_volume_step(int step);

// Deliver a level over the running helper's control channel, returning false
// when there is no helper to deliver it through.
//
// This is the whole point of routing the slider this way. The helper already
// holds a persistent connection to port 55001; spawning `wambridge-control.exe`
// opens a second one beside it, which is the documented way to make playback
// time out. Counted on hardware 2026-08-08: four connections per single menu
// press, two of them re-verifying the identity of a speaker the helper is
// mid-conversation with.
//
// False is normal, not an error: nothing is playing, so there is no helper, and
// the caller falls back to the process it would have spawned anyway.
bool send_volume_over_helper(int step);

// Route foobar pause/resume through the active helper. The helper temporarily
// writes raw speaker volume 0 and restores the previous level while the output
// continues feeding paced silence. False leaves the PCM pause fallback intact.
bool send_pause_over_helper(bool paused);

// Arm or cancel the M5 sleep timer over the active helper. nullopt means there
// is no helper, true means the helper accepted the request, false means its
// local control channel rejected or lost the request.
std::optional<bool> send_sleep_timer_over_helper(int seconds);

// Remember an explicit menu timer in the existing foobar INI so a helper
// replacement or foobar restart does not mistake it for a teardown timer and
// clear it on playback startup. Zero clears the remembered timer.
void note_menu_sleep_timer(int seconds);
bool menu_sleep_timer_active();

// Tell the output that the speaker is now at this raw step because something
// other than the slider moved it.
//
// With the slider routed there are two ways to change one level, and a menu
// action that moved the speaker without moving the slider left them disagreeing
// until the next drag yanked the speaker back. Reported by a listener: "volume
// to safe level went quiet but the slider did not move". The output puts the
// slider where the speaker actually is, so there is one visible truth again.
//
// Does nothing when the slider is not routed: the menu is then the only way to
// reach the speaker's own level and foobar's slider means something else.
void note_speaker_step(int step);

}  // namespace wam
