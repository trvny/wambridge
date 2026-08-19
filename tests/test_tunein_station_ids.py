"""A TuneIn id on a saved station, and what it must not break.

Added 2026-08-19 with the feature. The point of storing an id rather than a URL
is that the answer changes: a broadcaster moving its endpoint kills a hardcoded
address, and TuneIn re-resolves it. The saved URLs stay underneath, so every
test here also checks that the old behaviour survives the new field.
"""

from unittest import TestCase
from unittest.mock import patch

from wambridge.radio_cli import _station_candidates
from wambridge.samsung import WamApiError
from wambridge.stations import RadioStation, StationError, validate_tunein_id

STATIC = RadioStation(
    alias="czworka",
    url="http://stream3.polskieradio.pl:8906/;stream",
    fallback_urls=("http://mp3.polskieradio.pl:8956/;",),
)
WITH_ID = RadioStation(
    alias="trojka",
    url="http://41.dktr.pl:8000/trojka.ogg",
    tunein_id="s15984",
)


class TuneInIdValidationTests(TestCase):
    def test_a_station_id_is_accepted(self) -> None:
        self.assertEqual(validate_tunein_id("s15984"), "s15984")

    def test_podcast_and_episode_ids_are_refused(self) -> None:
        # `p` is a podcast and `t` a single episode; neither is a live stream.
        for value in ("p1234", "t9876", "15984", "", "s", "s12x"):
            with self.assertRaises(StationError):
                validate_tunein_id(value)

    def test_the_id_survives_a_round_trip_through_json(self) -> None:
        restored = RadioStation.from_dict(WITH_ID.to_dict())
        self.assertEqual(restored.tunein_id, "s15984")
        self.assertEqual(restored.url, WITH_ID.url)

    def test_a_station_without_an_id_serialises_as_before(self) -> None:
        # Old files have no such key and new files must not grow an empty one.
        self.assertNotIn("tunein_id", STATIC.to_dict())
        self.assertIsNone(RadioStation.from_dict(STATIC.to_dict()).tunein_id)


class StationCandidateTests(TestCase):
    def test_without_an_id_the_saved_urls_are_used_unchanged(self) -> None:
        self.assertEqual(_station_candidates(STATIC), STATIC.all_urls)

    def test_a_resolved_stream_is_tried_before_the_saved_urls(self) -> None:
        fresh = "http://stream3.polskieradio.pl:8954/"
        with patch(
            "wambridge.radio_cli.resolve_tunein_station", return_value=(fresh,)
        ):
            self.assertEqual(_station_candidates(WITH_ID), (fresh, WITH_ID.url))

    def test_an_empty_answer_falls_through_to_the_saved_urls(self) -> None:
        # BBC Radio 1 resolves to HLS only, which the resolver drops, so this
        # is the ordinary case for a station the speaker cannot take directly.
        with patch("wambridge.radio_cli.resolve_tunein_station", return_value=()):
            self.assertEqual(_station_candidates(WITH_ID), WITH_ID.all_urls)

    def test_tunein_being_unreachable_is_not_fatal(self) -> None:
        with patch(
            "wambridge.radio_cli.resolve_tunein_station",
            side_effect=WamApiError("no route"),
        ):
            self.assertEqual(_station_candidates(WITH_ID), WITH_ID.all_urls)

    def test_a_resolved_url_is_not_repeated_from_the_saved_list(self) -> None:
        with patch(
            "wambridge.radio_cli.resolve_tunein_station",
            return_value=(STATIC.url,),
        ):
            station = RadioStation(
                alias=STATIC.alias,
                url=STATIC.url,
                fallback_urls=STATIC.fallback_urls,
                tunein_id="s118200",
            )
            self.assertEqual(_station_candidates(station), STATIC.all_urls)
