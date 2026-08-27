package io.github.trvny.wambridge.mobile

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class SpeakerTargetTest {
    private val old = WamDiscovery.Speaker("10.0.0.20", "SSDP")
    private val moved = WamDiscovery.Speaker("10.0.0.44", "SSDP")

    @Test
    fun stableDeviceIdWinsAfterDhcpChangesTheAddress() {
        val identities = mapOf(
            old.ip to "OTHER",
            moved.ip to "A1B2C3D4E5F6",
        )
        val selected = SpeakerTarget.selectCandidate(
            savedIp = old.ip,
            savedId = "A1B2C3D4E5F6",
            speakers = listOf(old, moved),
            identify = identities::get,
        )
        assertEquals(moved, selected)
    }

    @Test
    fun stableDeviceIdNeverFallsBackToTheWrongSingleSpeaker() {
        val selected = SpeakerTarget.selectCandidate(
            savedIp = old.ip,
            savedId = "A1B2C3D4E5F6",
            speakers = listOf(moved),
            identify = { "OTHER" },
        )
        assertNull(selected)
    }

    @Test
    fun legacySavedIpStillDisambiguatesMultipleSpeakers() {
        val selected = SpeakerTarget.selectCandidate(
            savedIp = moved.ip,
            savedId = "",
            speakers = listOf(old, moved),
            identify = { null },
        )
        assertEquals(moved, selected)
    }

    @Test
    fun ambiguousLegacyDiscoveryDoesNotGuess() {
        val selected = SpeakerTarget.selectCandidate(
            savedIp = "10.0.0.99",
            savedId = "",
            speakers = listOf(old, moved),
            identify = { null },
        )
        assertNull(selected)
    }
}