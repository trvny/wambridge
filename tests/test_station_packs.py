from unittest import TestCase

from wambridge.station_packs import get_station_pack, station_pack_names
from wambridge.stations import StationError


class StationPackTests(TestCase):
    def test_top3_contains_user_stations_with_fallbacks(self) -> None:
        stations = get_station_pack("TOP3")

        self.assertEqual(
            [station.alias for station in stations],
            ["bbc1", "trojka", "czworka"],
        )
        self.assertTrue(all(len(station.all_urls) == 2 for station in stations))

    def test_favorites_contains_top3_and_extended_stations(self) -> None:
        stations = get_station_pack("favorites")
        aliases = [station.alias for station in stations]

        self.assertEqual(len(stations), 17)
        self.assertEqual(aliases[:3], ["bbc1", "trojka", "czworka"])
        self.assertIn("radioparadise", aliases)
        self.assertIn("bbc6", aliases)
        self.assertIn("radiozet", aliases)
        self.assertIn("streamingsoundtracks", aliases)

    def test_favorite_fallbacks_preserve_order(self) -> None:
        stations = {
            station.alias: station for station in get_station_pack("favorites")
        }

        self.assertEqual(
            stations["electroswing"].all_urls,
            (
                "https://streamer.radio.co/s2c3cc784b/listen",
                "https://streamer.radio.co:80/s2c3cc784b/listen",
            ),
        )
        self.assertEqual(
            stations["rmfmaxxx"].all_urls,
            (
                "https://rs201-krk.rmfstream.pl/rmf_maxxx",
                "http://195.150.20.7/rmf_maxxx",
            ),
        )

    def test_lists_available_packs(self) -> None:
        self.assertEqual(station_pack_names(), ("favorites", "top3"))

    def test_rejects_unknown_pack(self) -> None:
        with self.assertRaisesRegex(
            StationError,
            "available: favorites, top3",
        ):
            get_station_pack("missing")
