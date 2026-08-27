package io.github.trvny.wambridge.mobile

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Test

class RadioStationDefaultsTest {
    private val bundled = listOf(
        MobileRadioStation("trojka", listOf("http://builtin/trojka"), "s15984"),
        MobileRadioStation("czworka", listOf("http://builtin/czworka"), "s118200"),
    )

    @Test
    fun bundledStationsMakeAnEmptyStoreUseful() {
        assertEquals(bundled, mergeRadioStations(emptyList(), bundled))
    }

    @Test
    fun userStationOverridesBundledAlias() {
        val custom = MobileRadioStation("trojka", listOf("http://custom/trojka"))
        val merged = mergeRadioStations(listOf(custom), bundled)

        assertEquals(custom, merged.first { it.alias == "trojka" })
        assertEquals(2, merged.size)
    }
    @Test
    fun hiddenBundledStationStaysDeleted() {
        val merged = mergeRadioStations(
            saved = emptyList(),
            bundled = bundled,
            hiddenBundledAliases = setOf("TROJKA"),
        )

        assertFalse(merged.any { it.alias.equals("trojka", ignoreCase = true) })
        assertEquals(listOf("czworka"), merged.map { it.alias })
    }

    @Test
    fun userOverrideWinsEvenIfAnOldHideMarkerExists() {
        val custom = MobileRadioStation("trojka", listOf("http://custom/trojka"))
        val merged = mergeRadioStations(
            saved = listOf(custom),
            bundled = bundled,
            hiddenBundledAliases = setOf("trojka"),
        )
        assertEquals(custom, merged.first { it.alias == "trojka" })
    }

    @Test
    fun customStationsRemainAlongsideBundledOnes() {
        val custom = MobileRadioStation("my-radio", listOf("http://custom/radio"))
        val merged = mergeRadioStations(listOf(custom), bundled)

        assertEquals(listOf("trojka", "czworka", "my-radio"), merged.map { it.alias })
    }
}
