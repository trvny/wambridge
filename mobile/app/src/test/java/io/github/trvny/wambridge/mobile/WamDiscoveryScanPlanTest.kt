package io.github.trvny.wambridge.mobile

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The subnet arithmetic behind the LAN fallback.
 *
 * These are the numbers that decide whether an empty discovery result is
 * allowed to say "no speaker here" or has to admit it only looked at part of
 * the network, and none of them need a phone or a speaker to check.
 */
class WamDiscoveryScanPlanTest {
    private fun ip(value: String): Long =
        value.split('.').fold(0L) { acc, part -> (acc shl 8) or part.toLong() }

    private fun hosts(address: String, prefix: Int): List<String> =
        WamDiscovery.scanPlan(ip(address), prefix).hosts.toList()

    @Test
    fun `a 24 covers every host and skips network and broadcast`() {
        val plan = WamDiscovery.scanPlan(ip("10.0.0.108"), 24)
        val list = plan.hosts.toList()

        assertFalse(plan.narrowed)
        assertEquals(254, list.size)
        assertEquals(254L, plan.subnetHosts)
        assertEquals("10.0.0.1", list.first())
        assertEquals("10.0.0.254", list.last())
    }

    @Test
    fun `a 22 is still swept whole because it sits on the threshold`() {
        // MAX_SCAN_HOSTS is 1024 and a /22 has 1022 usable addresses, so this is
        // the widest prefix that still gets a complete sweep. One bit wider must not.
        val plan = WamDiscovery.scanPlan(ip("10.0.4.108"), 22)

        assertFalse(plan.narrowed)
        assertEquals(1022L, plan.subnetHosts)
        assertEquals(1022, plan.hosts.toList().size)
    }

    @Test
    fun `a 21 is narrowed to the window around this address`() {
        val plan = WamDiscovery.scanPlan(ip("10.0.4.108"), 21)
        val list = plan.hosts.toList()

        assertTrue(plan.narrowed)
        assertEquals(2046L, plan.subnetHosts)
        assertEquals(254, list.size)
        assertEquals("10.0.4.1", list.first())
        assertEquals("10.0.4.254", list.last())
    }

    @Test
    fun `a 16 reports the full subnet size it did not sweep`() {
        val plan = WamDiscovery.scanPlan(ip("172.16.31.20"), 16)
        val list = plan.hosts.toList()

        assertTrue(plan.narrowed)
        assertEquals(65_534L, plan.subnetHosts)
        assertEquals(254, list.size)
        assertEquals("172.16.31.1", list.first())
        assertEquals("172.16.31.254", list.last())
    }

    @Test
    fun `a 31 has no network or broadcast address to skip`() {
        val list = hosts("10.0.0.108", 31)

        assertEquals(listOf("10.0.0.108", "10.0.0.109"), list)
    }

    @Test
    fun `a 32 is the address itself`() {
        assertEquals(listOf("10.0.0.108"), hosts("10.0.0.108", 32))
    }

    @Test
    fun `a 30 leaves two usable addresses`() {
        assertEquals(listOf("10.0.0.109", "10.0.0.110"), hosts("10.0.0.108", 30))
    }

    @Test
    fun `an unusable prefix yields nothing rather than a bogus range`() {
        val plan = WamDiscovery.scanPlan(ip("10.0.0.108"), 100)

        // Prefixes are clamped, so this is a /32 - one address, nothing dropped.
        assertEquals(32, plan.prefixLength)
        assertEquals(listOf("10.0.0.108"), plan.hosts.toList())
    }

    @Test
    fun `a narrowed window never leaves the subnet`() {
        // The phone sits in the last /24 of a /16, where the window's upper end
        // would run past the subnet broadcast if it were not clamped.
        val plan = WamDiscovery.scanPlan(ip("192.168.255.250"), 16)
        val list = plan.hosts.toList()

        assertTrue(plan.narrowed)
        assertEquals("192.168.255.1", list.first())
        assertEquals("192.168.255.254", list.last())
    }
}
