package io.github.trvny.wambridge.mobile

import org.junit.Assert.assertEquals
import org.junit.Test

/**
 * What a radio session does when the Wi-Fi moves under it.
 *
 * The bug being closed is not the silence - that is unavoidable once the phone carrying the relay
 * leaves the network. It is that nothing noticed, so the foreground notification kept claiming
 * playback that had stopped minutes earlier, and coming back into range recovered neither side.
 */
class NetworkChangeTest {
    @Test
    fun `a session that is not running ignores everything`() {
        for (lost in listOf(false, true)) {
            for (available in listOf(false, true)) {
                assertEquals(
                    "running=false, lostEarlier=$lost, available=$available",
                    NetworkChangeAction.Ignore,
                    networkChangeAction(running = false, lostEarlier = lost, available = available),
                )
            }
        }
    }

    @Test
    fun `the first loss is reported`() {
        assertEquals(
            NetworkChangeAction.ReportLoss,
            networkChangeAction(running = true, lostEarlier = false, available = false),
        )
    }

    @Test
    fun `a second loss is not reported again`() {
        assertEquals(
            NetworkChangeAction.Ignore,
            networkChangeAction(running = true, lostEarlier = true, available = false),
        )
    }

    @Test
    fun `coming back after a loss releases the speaker`() {
        assertEquals(
            NetworkChangeAction.ReleaseAfterLoss,
            networkChangeAction(running = true, lostEarlier = true, available = true),
        )
    }

    @Test
    fun `the callback firing on registration changes nothing`() {
        // registerNetworkCallback delivers onAvailable for the current network straight away, and
        // that must not be read as a recovery from a loss that never happened.
        assertEquals(
            NetworkChangeAction.Ignore,
            networkChangeAction(running = true, lostEarlier = false, available = true),
        )
    }

    @Test
    fun `a full round trip reports once and releases once`() {
        var lost = false
        val seen = mutableListOf<NetworkChangeAction>()

        // registration, walk out of range, a duplicate callback, then back in range
        for (available in listOf(true, false, false, true)) {
            val action = networkChangeAction(running = true, lostEarlier = lost, available = available)
            seen += action
            when (action) {
                NetworkChangeAction.ReportLoss -> lost = true
                NetworkChangeAction.ReleaseAfterLoss -> lost = false
                NetworkChangeAction.Ignore -> Unit
            }
        }

        assertEquals(
            listOf(
                NetworkChangeAction.Ignore,
                NetworkChangeAction.ReportLoss,
                NetworkChangeAction.Ignore,
                NetworkChangeAction.ReleaseAfterLoss,
            ),
            seen,
        )
    }
}
