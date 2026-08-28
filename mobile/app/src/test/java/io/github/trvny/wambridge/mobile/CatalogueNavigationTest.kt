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
        // `startIndex` describes where the accumulated list begins, not where the last
        // fetch did, so that it plus the row count is what has actually been fetched.
        assertEquals(0, merged.startIndex)
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

        assertEquals(3, merged.entries.size)
        assertFalse(merged.hasMore)
    }

    @Test
    fun athirdPageIsStillOfferedOnALevelThatHasOne() {
        // The regression this guards: taking the fresh page's startIndex alongside the
        // accumulated rows counts the first page twice, so a 90-row level looked
        // complete after 60 rows and the last thirty could not be reached.
        val pageSize = 30
        val total = 90
        var accumulated: SamsungCatalogue.Page? = null

        repeat(2) { fetched ->
            val startIndex = fetched * pageSize
            accumulated = mergedCataloguePage(
                accumulated,
                SamsungCatalogue.Page(
                    category = "Local Radio",
                    total = total,
                    startIndex = startIndex,
                    entries = (0 until pageSize).map { station("${startIndex + it}", "row") },
                ),
                append = accumulated != null,
            )
        }

        val page = accumulated!!
        assertEquals(60, page.entries.size)
        assertTrue("60 of 90 rows fetched, so there is more", page.hasMore)
        // What `loadMore` would ask for next.
        assertEquals(60, page.startIndex + page.entries.size)
    }

    @Test
    fun appendingOntoNothingIsJustTheFreshPage() {
        val fresh = SamsungCatalogue.Page(entries = listOf(station("1", "One")))

        assertEquals(fresh, mergedCataloguePage(null, fresh, append = true))
    }
}
