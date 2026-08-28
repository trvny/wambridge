package io.github.trvny.wambridge.mobile

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Covers the two decisions the browse screen makes on its own. Everything else it
 * does is either a view or a straight delegation to [SamsungCatalogue], whose
 * parsing is covered against real fixtures in `SamsungCatalogueParsingTest`.
 */
class CatalogueNavigationTest {
    private fun station(id: String, title: String) = SamsungCatalogue.Entry(
        contentId = id,
        title = title,
        itemType = "2",
        mediaId = "s$id",
    )

    @Test
    fun upClimbsOneLevelWhileBrowsing() {
        assertFalse(catalogueUpNeedsReopen(inSearch = false, atRoot = false))
        assertFalse(catalogueUpNeedsReopen(inSearch = false, atRoot = true))
    }

    @Test
    fun upInsideSearchResultsStillClimbsWhileThereIsSomewhereToClimb() {
        assertFalse(catalogueUpNeedsReopen(inSearch = true, atRoot = false))
    }

    @Test
    fun upFromTheTopOfSearchHasToReopen() {
        // `ascend` cannot cross out of the search tree, so climbing from its root
        // would leave the screen showing the same page and no way back.
        assertTrue(catalogueUpNeedsReopen(inSearch = true, atRoot = true))
    }

    @Test
    fun aFreshPageReplacesWhatWasShown() {
        val first = SamsungCatalogue.Page(
            category = "Local Radio",
            total = 90,
            startIndex = 0,
            entries = listOf(station("1", "One"), station("2", "Two")),
        )
        val other = SamsungCatalogue.Page(
            category = "Trending",
            total = 12,
            startIndex = 0,
            entries = listOf(station("3", "Three")),
        )

        val merged = mergedCataloguePage(first, other, append = false)

        assertEquals(other, merged)
    }

    @Test
    fun pagingAppendsSoOneLevelReadsAsOneList() {
        val first = SamsungCatalogue.Page(
            category = "Local Radio",
            total = 90,
            startIndex = 0,
            entries = listOf(station("1", "One"), station("2", "Two")),
        )
        val next = SamsungCatalogue.Page(
            category = "Local Radio",
            total = 90,
            startIndex = 2,
            entries = listOf(station("3", "Three")),
        )

        val merged = mergedCataloguePage(first, next, append = true)

        assertEquals(listOf("One", "Two", "Three"), merged.entries.map { it.title })
        // The fresh page is the speaker's account of the level, so its metadata wins.
        assertEquals(2, merged.startIndex)
        assertEquals(90, merged.total)
    }

    @Test
    fun appendedPagesStopAskingForMoreOnceTheLevelIsComplete() {
        val first = SamsungCatalogue.Page(
            total = 3,
            startIndex = 0,
            entries = listOf(station("1", "One"), station("2", "Two")),
        )
        assertTrue(first.hasMore)

        val merged = mergedCataloguePage(
            first,
            SamsungCatalogue.Page(total = 3, startIndex = 2, entries = listOf(station("3", "Three"))),
            append = true,
        )

        // startIndex 2 plus three accumulated rows would overshoot if `hasMore` counted
        // the merged list against the total; it must count what is still unfetched.
        assertEquals(3, merged.entries.size)
        assertFalse(merged.hasMore)
    }

    @Test
    fun appendingOntoNothingIsJustTheFreshPage() {
        val fresh = SamsungCatalogue.Page(entries = listOf(station("1", "One")))

        assertEquals(fresh, mergedCataloguePage(null, fresh, append = true))
    }
}
