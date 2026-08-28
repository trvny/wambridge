package io.github.trvny.wambridge.mobile

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The same trimmed answers of the physical M5 the desktop suite parses, so the
 * two implementations cannot drift apart quietly. The partner id and serial in
 * `stationurl` are that speaker's TuneIn credentials and are replaced here;
 * everything else is verbatim, including the two shapes of `menuitem` and the
 * field order, which is not stable between rows.
 */
class SamsungCatalogueParsingTest {
    @Test
    fun readsTheRootLevel() {
        val page = SamsungCatalogue.parsePage(ROOT_BODY)

        assertTrue(page.isRoot)
        assertEquals("Browse", page.category)
        assertEquals(12, page.total)
        assertEquals(listOf("Favorites", "Local Radio"), page.entries.map { it.title })
        assertTrue(page.entries.all { it.isFolder })
    }

    @Test
    fun readsStationsRegardlessOfFieldOrder() {
        val page = SamsungCatalogue.parsePage(LEVEL_BODY)

        assertFalse(page.isRoot)
        assertEquals("Local Radio", page.category)
        val (first, second) = page.entries
        assertEquals("s87779", first.mediaId)
        assertEquals("Hity Na Czasie", first.description)
        assertEquals("s2467", second.mediaId)
        assertNull(second.description)
        assertTrue(second.thumbnail!!.endsWith("logot.png"))
        assertTrue(page.entries.all { it.isStation })
    }

    @Test
    fun pagingStateSaysWhenALevelContinues() {
        val page = SamsungCatalogue.parsePage(LEVEL_BODY)

        // Two rows of ninety: the fixture is trimmed, and so is the real page -
        // `Local Radio` answers `totallistcount=90` for a `listcount` of 30.
        assertEquals(0, page.startIndex)
        assertTrue(page.hasMore)
        assertFalse(SamsungCatalogue.parsePage(EMPTY_BODY).hasMore)
    }

    @Test
    fun thePageSaysWhichTreeItCameFrom() {
        assertEquals(SamsungCatalogue.BROWSE_ROOT, SamsungCatalogue.parsePage(ROOT_BODY).root)
        val search = SamsungCatalogue.parsePage(SEARCH_BODY)
        assertEquals("Search", search.root)
        assertTrue(search.isRoot)
    }

    @Test
    fun aSearchHeadingIsNeitherFolderNorStation() {
        val (heading, station) = SamsungCatalogue.parsePage(SEARCH_BODY).entries

        // A heading answers type 0 like a folder, but carries no `mediaid` and
        // descending into it returns nothing.
        assertFalse(heading.isStation)
        assertNull(heading.mediaId)
        assertTrue(station.isStation)
        assertEquals("s15984", station.mediaId)
    }

    @Test
    fun stationDetailCarriesThePlayableUrl() {
        val detail = SamsungCatalogue.parseStationDetail(STATION_BODY)

        assertEquals("ESKA Kraków 97.7", detail.title)
        assertEquals("Hity Na Czasie", detail.description)
        assertTrue(detail.stationUrl!!.startsWith("http://opml.radiotime.com/Tune.ashx?id=s87779"))
    }

    @Test
    fun malformedXmlIsReportedRatherThanReturnedEmpty() {
        val error = runCatching { SamsungCatalogue.parsePage("<CPM><response") }.exceptionOrNull()

        assertTrue(error?.message.orEmpty().contains("invalid catalogue XML"))
    }

    private companion object {
        const val ROOT_BODY =
            "<CPM><method>RadioList</method><response result=\"ok\">" +
                "<cpname>TuneIn</cpname><root>Browse</root>" +
                "<category isroot=\"1\">Browse</category>" +
                "<totallistcount>12</totallistcount>" +
                "<startindex>0</startindex><listcount>12</listcount>" +
                "<menulist>" +
                "<menuitem type=\"0\"><title>Favorites</title><contentid>0</contentid></menuitem>" +
                "<menuitem type=\"0\"><title>Local Radio</title><contentid>1</contentid></menuitem>" +
                "</menulist></response></CPM>"

        const val LEVEL_BODY =
            "<CPM><method>RadioList</method><response result=\"ok\">" +
                "<cpname>TuneIn</cpname><root>Browse</root>" +
                "<category isroot=\"0\">Local Radio</category>" +
                "<totallistcount>90</totallistcount>" +
                "<startindex>0</startindex><listcount>30</listcount>" +
                "<menulist>" +
                "<menuitem type=\"2\" cat=\"stations\">" +
                "<thumbnail>http://cdn-profiles.tunein.com/s87779/images/logot.jpg</thumbnail>" +
                "<description>Hity Na Czasie</description>" +
                "<mediaid>s87779</mediaid>" +
                "<title>ESKA Kraków 97.7</title>" +
                "<contentid>0</contentid></menuitem>" +
                "<menuitem type=\"2\" cat=\"stations\">" +
                "<mediaid>s2467</mediaid>" +
                "<title>Polskie Radio 102.2</title>" +
                "<contentid>1</contentid>" +
                "<thumbnail>http://cdn-profiles.tunein.com/s2467/images/logot.png</thumbnail>" +
                "</menuitem>" +
                "</menulist></response></CPM>"

        const val EMPTY_BODY =
            "<CPM><method>RadioList</method><response result=\"ok\">" +
                "<cpname>TuneIn</cpname>" +
                "<category isroot=\"0\">Favorites</category>" +
                "<totallistcount>0</totallistcount><startindex>0</startindex>" +
                "<menulist></menulist></response></CPM>"

        const val STATION_BODY =
            "<CPM><method>StationData</method><response result=\"ok\">" +
                "<cpname>TuneIn</cpname>" +
                "<title>ESKA Kraków 97.7</title>" +
                "<description>Hity Na Czasie</description>" +
                "<thumbnail>http://cdn-profiles.tunein.com/s87779/images/logod.jpg</thumbnail>" +
                "<stationurl>http://opml.radiotime.com/Tune.ashx?id=s87779" +
                "&amp;partnerId=REDACTED&amp;serial=REDACTED</stationurl>" +
                "</response></CPM>"

        const val SEARCH_BODY =
            "<CPM><method>RadioList</method><response result=\"ok\">" +
                "<cpname>TuneIn</cpname><root>Search</root>" +
                "<searchquery>Trojka</searchquery>" +
                "<category isroot=\"1\">Search</category>" +
                "<totallistcount>66</totallistcount><startindex>0</startindex>" +
                "<menulist>" +
                "<menuitem type=\"0\"><title>Artist: Trojka</title><contentid>0</contentid></menuitem>" +
                "<menuitem type=\"2\" cat=\"stations\"><mediaid>s15984</mediaid>" +
                "<title>PR3 Trójka</title><contentid>1</contentid></menuitem>" +
                "</menulist></response></CPM>"
    }

    @Test
    fun aPodcastEpisodeIsNotOfferedAsAStation() {
        // Measured on the M5: descending into a podcast programme found by search
        // lists episodes that are `type="2"` like stations, but carry `t…` media
        // ids the resolver cannot use. A Play button there could only fail.
        val episode = SamsungCatalogue.Entry(
            contentId = "0",
            title = "Information scarcity after Nepal flash floods (47m)",
            itemType = "2",
            mediaId = "t573501779",
        )

        assertFalse(episode.isStation)
        assertFalse(episode.isFolder)
    }

    @Test
    fun aStationIdIsStillOfferedAsAStation() {
        val bbc = SamsungCatalogue.Entry(
            contentId = "4",
            title = "BBC Radio 2",
            itemType = "2",
            mediaId = "s24940",
        )

        assertTrue(bbc.isStation)
    }
}
