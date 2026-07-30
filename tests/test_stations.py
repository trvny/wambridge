import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from wambridge.stations import RadioStation, StationError, StationStore


class StationStoreTests(TestCase):
    def test_round_trip_and_case_insensitive_lookup(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "stations.json"
            store = StationStore(path)
            store.put(
                RadioStation(
                    alias="Paradise",
                    url="https://example.net/radio.mp3",
                    fallback_urls=("https://backup.example.net/radio.mp3",),
                )
            )

            station = store.get("paradise")
            self.assertEqual(
                station.all_urls,
                (
                    "https://example.net/radio.mp3",
                    "https://backup.example.net/radio.mp3",
                ),
            )
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["version"], 1)
            self.assertEqual(
                payload["stations"][0]["fallback_urls"],
                ["https://backup.example.net/radio.mp3"],
            )

    def test_loads_existing_single_url_config(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "stations.json"
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "stations": [
                            {
                                "alias": "Old",
                                "url": "http://old.example/live",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(
                StationStore(path).get("old").all_urls,
                ("http://old.example/live",),
            )

    def test_deduplicates_fallback_urls(self) -> None:
        with TemporaryDirectory() as directory:
            store = StationStore(Path(directory) / "stations.json")
            store.put(
                RadioStation(
                    "Radio",
                    "http://one.example/live",
                    (
                        "http://two.example/live",
                        "http://one.example/live",
                        "http://two.example/live",
                    ),
                )
            )

            self.assertEqual(
                store.get("radio").all_urls,
                (
                    "http://one.example/live",
                    "http://two.example/live",
                ),
            )

    def test_replaces_existing_alias(self) -> None:
        with TemporaryDirectory() as directory:
            store = StationStore(Path(directory) / "stations.json")
            store.put(RadioStation("Radio", "http://one.example/live"))
            store.put(RadioStation("radio", "https://two.example/live"))

            self.assertEqual(len(store.all()), 1)
            self.assertEqual(
                store.get("RADIO").url,
                "https://two.example/live",
            )

    def test_puts_multiple_stations_at_once(self) -> None:
        with TemporaryDirectory() as directory:
            store = StationStore(Path(directory) / "stations.json")
            imported = store.put_many(
                [
                    RadioStation("One", "http://one.example/live"),
                    RadioStation("Two", "http://two.example/live"),
                ]
            )

            self.assertEqual([station.alias for station in imported], ["One", "Two"])
            self.assertEqual(len(store.all()), 2)

    def test_removes_station(self) -> None:
        with TemporaryDirectory() as directory:
            store = StationStore(Path(directory) / "stations.json")
            store.put(RadioStation("Radio", "https://example.net/live"))

            removed = store.remove("radio")

            self.assertEqual(removed.alias, "Radio")
            self.assertEqual(store.all(), [])

    def test_rejects_non_http_url(self) -> None:
        with TemporaryDirectory() as directory:
            store = StationStore(Path(directory) / "stations.json")
            with self.assertRaisesRegex(StationError, "HTTP or HTTPS"):
                store.put(RadioStation("Bad", "file:///tmp/radio.mp3"))
