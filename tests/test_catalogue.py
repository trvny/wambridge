from unittest import TestCase
from unittest.mock import patch

from wambridge.catalogue import (
    BROWSE_ROOT,
    RadioEntry,
    RadioPage,
    ascend,
    current_page,
    descend,
    open_catalogue,
    parse_radio_page,
    parse_station_detail,
    search,
    station_detail,
)
from wambridge.samsung import WamApiError, WamResponse

# Trimmed from a real answer of the physical M5, 2026-08-26. The partner id and
# serial in `stationurl` are that speaker's TuneIn credentials and are replaced
# here; everything else is verbatim, including the two shapes of `menuitem` and
# the field order, which is not stable between rows.
ROOT_BODY = (
    '<CPM><method>RadioList</method><response result="ok">'
    "<cpname>TuneIn</cpname><root>Browse</root>"
    '<category isroot="1">Browse</category>'
    "<totallistcount>12</totallistcount>"
    "<startindex>0</startindex><listcount>12</listcount>"
    "<menulist>"
    '<menuitem type="0"><title>Favorites</title><contentid>0</contentid></menuitem>'
    '<menuitem type="0"><title>Local Radio</title><contentid>1</contentid></menuitem>'
    "</menulist></response></CPM>"
)

LEVEL_BODY = (
    '<CPM><method>RadioList</method><response result="ok">'
    "<cpname>TuneIn</cpname><root>Browse</root>"
    '<category isroot="0">Local Radio</category>'
    "<totallistcount>90</totallistcount>"
    "<startindex>0</startindex><listcount>30</listcount>"
    "<menulist>"
    '<menuitem type="2" cat="stations">'
    "<thumbnail>http://cdn-profiles.tunein.com/s87779/images/logot.jpg</thumbnail>"
    "<description>Hity Na Czasie</description>"
    "<mediaid>s87779</mediaid>"
    "<title>ESKA Kraków 97.7</title>"
    "<contentid>0</contentid></menuitem>"
    '<menuitem type="2" cat="stations">'
    "<mediaid>s2467</mediaid>"
    "<title>Polskie Radio 102.2</title>"
    "<contentid>1</contentid>"
    "<thumbnail>http://cdn-profiles.tunein.com/s2467/images/logot.png</thumbnail>"
    "</menuitem>"
    "</menulist></response></CPM>"
)

EMPTY_BODY = (
    '<CPM><method>RadioList</method><response result="ok">'
    "<cpname>TuneIn</cpname>"
    '<category isroot="0">Favorites</category>'
    "<totallistcount>0</totallistcount><startindex>0</startindex>"
    "<menulist></menulist></response></CPM>"
)

EMPTY_ROOT_BODY = (
    '<CPM><method>RadioList</method><response result="ok">'
    "<cpname>TuneIn</cpname>"
    '<root>Browse</root>'
    '<category isroot="1">Browse</category>'
    "<totallistcount>0</totallistcount><startindex>0</startindex>"
    "<menulist></menulist></response></CPM>"
)

EMPTY_SEARCH_BODY = (
    '<CPM><method>RadioList</method><response result="ok">'
    "<cpname>TuneIn</cpname>"
    "<root>Search</root>"
    '<category isroot="1">Search</category>'
    "<totallistcount>0</totallistcount><startindex>0</startindex>"
    "<menulist></menulist></response></CPM>"
)

STATION_BODY = (
    '<CPM><method>StationData</method><response result="ok">'
    "<cpname>TuneIn</cpname>"
    "<title>ESKA Kraków 97.7</title>"
    "<description>Hity Na Czasie</description>"
    "<thumbnail>http://cdn-profiles.tunein.com/s87779/images/logod.jpg</thumbnail>"
    "<stationurl>http://opml.radiotime.com/Tune.ashx?id=s87779"
    "&amp;partnerId=REDACTED&amp;serial=REDACTED</stationurl>"
    "</response></CPM>"
)

SEARCH_BODY = (
    '<CPM><method>RadioList</method><response result="ok">'
    "<cpname>TuneIn</cpname><root>Search</root>"
    "<searchquery>Trojka</searchquery>"
    '<category isroot="1">Search</category>'
    "<totallistcount>66</totallistcount><startindex>0</startindex>"
    "<menulist>"
    '<menuitem type="0"><title>Artist: Trojka</title><contentid>0</contentid></menuitem>'
    '<menuitem type="2" cat="stations"><mediaid>s15984</mediaid>'
    "<title>PR3 Trójka</title><contentid>1</contentid></menuitem>"
    "</menulist></response></CPM>"
)


def response(body: str) -> WamResponse:
    return WamResponse(method="RadioList", result="ok", body=body)


class RadioPageParsingTests(TestCase):
    def test_reads_the_root_level(self) -> None:
        page = parse_radio_page(ROOT_BODY)

        self.assertTrue(page.is_root)
        self.assertEqual(page.category, "Browse")
        self.assertEqual(page.total, 12)
        self.assertEqual([entry.title for entry in page.entries], ["Favorites", "Local Radio"])
        self.assertTrue(all(entry.is_folder for entry in page.entries))

    def test_reads_stations_regardless_of_field_order(self) -> None:
        # The two rows in this fixture list their children in different orders,
        # which is what the speaker actually does.
        page = parse_radio_page(LEVEL_BODY)

        self.assertFalse(page.is_root)
        self.assertEqual(page.category, "Local Radio")
        first, second = page.entries
        self.assertEqual(first.media_id, "s87779")
        self.assertEqual(first.description, "Hity Na Czasie")
        self.assertEqual(second.media_id, "s2467")
        self.assertIsNone(second.description)
        self.assertTrue(second.thumbnail.endswith("logot.png"))
        self.assertTrue(all(entry.is_station for entry in page.entries))

    def test_paging_state_says_when_a_level_continues(self) -> None:
        page = parse_radio_page(LEVEL_BODY)

        # Two rows of ninety: the fixture is trimmed, and so is the real page -
        # `Local Radio` answers `totallistcount=90` for a `listcount` of 30.
        self.assertEqual(page.start_index, 0)
        self.assertTrue(page.has_more)
        self.assertFalse(parse_radio_page(EMPTY_BODY).has_more)

    def test_the_page_says_which_tree_it_came_from(self) -> None:
        self.assertEqual(parse_radio_page(ROOT_BODY).root, BROWSE_ROOT)
        search_page = parse_radio_page(SEARCH_BODY)
        self.assertEqual(search_page.root, "Search")
        self.assertTrue(search_page.is_root)

    def test_a_search_heading_is_neither_folder_nor_station(self) -> None:
        heading, station = parse_radio_page(SEARCH_BODY).entries

        self.assertTrue(heading.is_folder)
        self.assertFalse(heading.is_station)
        self.assertIsNone(heading.media_id)
        self.assertTrue(station.is_station)
        self.assertEqual(station.media_id, "s15984")

    def test_a_podcast_episode_is_not_a_station(self) -> None:
        # Measured on the M5: descending into a podcast programme reached through
        # a search lists episodes that are type="2" exactly like stations, but
        # carry t… ids nothing here can resolve.
        episode = RadioEntry(
            content_id="0",
            title="Information scarcity after Nepal flash floods (47m)",
            item_type="2",
            media_id="t573501779",
        )

        self.assertFalse(episode.is_station)
        self.assertFalse(episode.is_folder)

    def test_a_station_id_is_still_a_station(self) -> None:
        bbc = RadioEntry(
            content_id="4",
            title="BBC Radio 2",
            item_type="2",
            media_id="s24940",
        )

        self.assertTrue(bbc.is_station)

    def test_content_id_is_the_number_the_commands_expect(self) -> None:
        entry = parse_radio_page(LEVEL_BODY).entries[1]

        self.assertEqual(entry.index, 1)

    def test_invalid_xml_is_reported_as_such(self) -> None:
        with self.assertRaises(WamApiError):
            parse_radio_page("<CPM><method>RadioList")


class StationDetailTests(TestCase):
    def test_reads_the_station_url_the_speaker_cannot_play(self) -> None:
        detail = parse_station_detail(STATION_BODY)

        self.assertEqual(detail.title, "ESKA Kraków 97.7")
        self.assertTrue(detail.station_url.startswith("http://opml.radiotime.com/Tune.ashx"))
        self.assertIn("id=s87779", detail.station_url)


class CursorTests(TestCase):
    def test_open_catalogue_walks_up_until_the_root(self) -> None:
        # The cursor survives whoever moved it last, so the first answer here is
        # a level part-way down - exactly what a fresh process finds.
        bodies = [LEVEL_BODY, ROOT_BODY]
        with patch("wambridge.catalogue.request") as send, patch(
            "wambridge.catalogue.time.sleep"
        ):
            send.side_effect = [response("<CPM/>"), *[response(b) for b in bodies]]
            page = open_catalogue("10.0.0.104")

        self.assertTrue(page.is_root)
        self.assertEqual(send.call_args_list[0].args[1], "SetSelectRadio")
        self.assertEqual(send.call_args_list[1].args[1], "GetUpperRadioList")

    def test_the_search_tree_is_left_with_browse_main(self) -> None:
        # Both trees answer isroot="1", so walking up stops in Search and
        # handing that page back would label search results as the catalogue.
        # BrowseMain is the only command measured to cross back.
        with patch("wambridge.catalogue.request") as send, patch(
            "wambridge.catalogue.time.sleep"
        ):
            send.side_effect = [
                response("<CPM/>"),
                response(SEARCH_BODY),
                response("<CPM/>"),
                response(ROOT_BODY),
            ]
            page = open_catalogue("10.0.0.104")

        self.assertEqual(page.root, BROWSE_ROOT)
        methods = [call.args[1] for call in send.call_args_list]
        self.assertEqual(methods[2:], ["BrowseMain", "GetCurrentRadioList"])

    def test_a_cursor_browse_main_cannot_rescue_is_an_error(self) -> None:
        with patch("wambridge.catalogue.request") as send, patch(
            "wambridge.catalogue.time.sleep"
        ):
            send.side_effect = [
                response("<CPM/>"),
                response(SEARCH_BODY),
                response("<CPM/>"),
                response(SEARCH_BODY),
            ]
            with self.assertRaises(WamApiError) as caught:
                open_catalogue("10.0.0.104")

        self.assertIn("Search", str(caught.exception))
        self.assertIn(BROWSE_ROOT, str(caught.exception))

    def test_an_empty_root_is_retried_rather_than_believed(self) -> None:
        # A recovering CPM answers totallistcount=0 for a level that has content.
        # The Browse root is never genuinely empty, so an empty one means the
        # speaker is not ready yet - not that there is nothing to show.
        with patch("wambridge.catalogue.request") as send, patch(
            "wambridge.catalogue.time.sleep"
        ):
            send.side_effect = [
                response("<CPM/>"),
                response(EMPTY_ROOT_BODY),
                response(ROOT_BODY),
            ]
            page = open_catalogue("10.0.0.104")

        self.assertTrue(page.is_root)
        self.assertTrue(page.entries)

    def test_a_root_that_stays_empty_is_an_error(self) -> None:
        with patch("wambridge.catalogue.request") as send, patch(
            "wambridge.catalogue.time.sleep"
        ):
            send.side_effect = [response("<CPM/>"), *[response(EMPTY_ROOT_BODY)] * 5]
            with self.assertRaises(WamApiError):
                open_catalogue("10.0.0.104", attempts=5)

    def test_a_search_that_matched_nothing_is_still_crossed_out_of(self) -> None:
        # The Search root is legitimately empty after a search with no hits, and
        # only BrowseMain leaves that tree. Retrying it as if CPM were recovering
        # would strand the cursor there and fail every later browse.
        with patch("wambridge.catalogue.request") as send, patch(
            "wambridge.catalogue.time.sleep"
        ):
            send.side_effect = [
                response("<CPM/>"),
                response(EMPTY_SEARCH_BODY),
                response("<CPM/>"),
                response(ROOT_BODY),
            ]
            page = open_catalogue("10.0.0.104")

        self.assertEqual(page.root, BROWSE_ROOT)
        self.assertTrue(page.entries)
        methods = [call.args[1] for call in send.call_args_list]
        self.assertEqual(methods[2:], ["BrowseMain", "GetCurrentRadioList"])

    def test_a_cursor_that_will_not_normalise_is_an_error(self) -> None:
        # Reporting the wrong level as the root would make every later call
        # describe somewhere else, so this fails instead.
        with patch("wambridge.catalogue.request") as send, patch(
            "wambridge.catalogue.time.sleep"
        ):
            send.side_effect = [response("<CPM/>"), *[response(LEVEL_BODY)] * 5]
            with self.assertRaises(WamApiError):
                open_catalogue("10.0.0.104", attempts=5)

    def test_descend_sends_the_page_local_content_id(self) -> None:
        with patch("wambridge.catalogue.request") as send:
            send.return_value = response(LEVEL_BODY)
            page = descend("10.0.0.104", 1)

        self.assertEqual(page.category, "Local Radio")
        method = send.call_args.args[1]
        arguments = dict((name, value) for name, value, _ in send.call_args.args[2])
        self.assertEqual(method, "GetSelectRadioList")
        self.assertEqual(arguments["contentid"], 1)
        self.assertEqual(arguments["startindex"], 0)

    def test_ascend_and_current_page_do_not_move_sideways(self) -> None:
        with patch("wambridge.catalogue.request") as send:
            send.return_value = response(ROOT_BODY)
            ascend("10.0.0.104")
            self.assertEqual(send.call_args.args[1], "GetUpperRadioList")

            send.return_value = response(LEVEL_BODY)
            current_page("10.0.0.104", start_index=30)
            arguments = dict((n, v) for n, v, _ in send.call_args.args[2])
            self.assertEqual(send.call_args.args[1], "GetCurrentRadioList")
            self.assertEqual(arguments["startindex"], 30)

    def test_an_empty_page_is_retried_before_it_is_believed(self) -> None:
        # `totallistcount=0` on a level with content is the CPM subsystem
        # recovering, not an empty category.
        with patch("wambridge.catalogue.request") as send, patch(
            "wambridge.catalogue.time.sleep"
        ):
            send.side_effect = [response(EMPTY_BODY), response(LEVEL_BODY)]
            page = descend("10.0.0.104", 1, attempts=3)

        self.assertEqual(send.call_count, 2)
        self.assertEqual(len(page.entries), 2)

    def test_a_genuinely_empty_level_is_returned_not_raised(self) -> None:
        with patch("wambridge.catalogue.request") as send, patch(
            "wambridge.catalogue.time.sleep"
        ):
            send.side_effect = [response(EMPTY_BODY)] * 3
            page = descend("10.0.0.104", 0, attempts=3)

        self.assertIsInstance(page, RadioPage)
        self.assertEqual(page.entries, ())
        self.assertEqual(page.category, "Favorites")

    def test_station_detail_asks_by_page_index(self) -> None:
        with patch("wambridge.catalogue.request") as send:
            send.return_value = response(STATION_BODY)
            detail = station_detail("10.0.0.104", 0)

        arguments = dict((n, v) for n, v, _ in send.call_args.args[2])
        self.assertEqual(send.call_args.args[1], "GetStationData")
        self.assertEqual(arguments["selectitemid"], 0)
        self.assertIsNotNone(detail.station_url)


class SearchTests(TestCase):
    def test_search_passes_the_query_and_pages(self) -> None:
        with patch("wambridge.catalogue.request") as send:
            send.return_value = response(SEARCH_BODY)
            page = search("10.0.0.104", "Trojka", start_index=10)

        arguments = dict((n, v) for n, v, _ in send.call_args.args[2])
        self.assertEqual(send.call_args.args[1], "SearchQuery")
        self.assertEqual(arguments["query"], "Trojka")
        self.assertEqual(arguments["startindex"], 10)
        self.assertEqual(page.total, 66)

    def test_an_empty_query_is_refused_before_the_speaker_is_asked(self) -> None:
        with patch("wambridge.catalogue.request") as send:
            with self.assertRaises(WamApiError):
                search("10.0.0.104", "   ")
        send.assert_not_called()
