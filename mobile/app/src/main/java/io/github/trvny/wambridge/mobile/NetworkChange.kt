package io.github.trvny.wambridge.mobile

/**
 * What a running radio session should do when the Wi-Fi under it changes.
 *
 * The asymmetry this exists for: on the relayed path the speaker holds a session only this side
 * can end, so a phone that walks out of range strands it. The speaker falls silent, keeps the
 * session, and never idles - measured 2026-08-16, 33 minutes still lit after a session that sent no
 * release, against 17 minutes to dark after one that did.
 *
 * Two things were wrong here at once, and the second is the one a listener actually sees: nothing
 * watched for the network going away, so the foreground notification went on claiming playback that
 * had stopped minutes earlier.
 */
internal enum class NetworkChangeAction {
    /** Nothing is playing, or the change says nothing about this session. */
    Ignore,

    /** The Wi-Fi went away mid-session. Say so, and stop claiming the speaker is playing. */
    ReportLoss,

    /**
     * The Wi-Fi came back after a loss.
     *
     * Release the speaker rather than resume. The stream was interrupted for an unknown length of
     * time and the proxy's resolved sources may be stale, while a re-offer would mean a second run
     * at the `55001` socket - and this project's most expensive measured failure was a restart loop
     * against that port, 77 helper restarts in 90 seconds, ending with the speaker refusing
     * commands entirely. Letting go is the outcome that cannot go wrong; the listener presses play
     * again.
     */
    ReleaseAfterLoss,
}

/**
 * Decide from the session state alone, so the decision can be tested without a phone.
 *
 * @param running whether a radio session is live at all.
 * @param lostEarlier whether this session has already seen the network go away.
 * @param available whether a matching network is present now.
 */
internal fun networkChangeAction(
    running: Boolean,
    lostEarlier: Boolean,
    available: Boolean,
): NetworkChangeAction = when {
    !running -> NetworkChangeAction.Ignore
    !available && !lostEarlier -> NetworkChangeAction.ReportLoss
    // A second `onLost` for another interface, or a callback arriving twice. The loss has been
    // reported once already and reporting it again would only rewrite the same notification.
    !available -> NetworkChangeAction.Ignore
    lostEarlier -> NetworkChangeAction.ReleaseAfterLoss
    // Wi-Fi present and never lost: the ordinary case, and by far the most frequent, since
    // `onAvailable` also fires once at registration.
    else -> NetworkChangeAction.Ignore
}
