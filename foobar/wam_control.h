#pragma once

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

}  // namespace wam
