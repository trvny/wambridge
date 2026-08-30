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

// Record a menu-owned timer after the speaker accepts it. Zero clears menu
// ownership and lets the configured after-stop timer take over again.
void note_menu_sleep_timer(int seconds);

// Report whether a persisted menu-owned sleep deadline is still active. The
// output uses this only to mark replacement helpers as inheriting menu timer
// ownership while still passing the configured after-stop duration separately.
bool menu_sleep_timer_active();

// Tell the active helper this is a real stop, so its own release() (pause,
// then conditionally arm the sleep timer) runs immediately over the control
// channel rather than waiting for the helper's own exit path - which an
// encoder that never exits could otherwise delay well past when the process
// is already being torn down. Send this before killing the helper, while its
// control socket is still open; best effort, like every other call here.
bool send_release_over_helper();

// Tell the active helper it is being replaced (a seek or format change), not
// ended - the same release as above minus arming a sleep timer, since a
// replacement is about to keep the speaker awake on its own and arming one
// here is exactly the race cancel_sleep_timer() exists to clear after the
// fact today.
bool send_discard_over_helper();

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
