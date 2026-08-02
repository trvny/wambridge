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

}  // namespace wam
