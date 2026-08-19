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


NEWLINE = chr(10)


class PlaylistExpansionTests(TestCase):
    """TuneIn hands back a .pls for some stations and FFmpeg will not open one."""

    def _resolve(self, answers: list[str]) -> tuple[str, ...]:
        from wambridge import tunein

        class FakeResponse:
            def __init__(self, payload: str) -> None:
                self._payload = payload.encode()

            def read(self, _size: int = 0) -> bytes:
                return self._payload

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return None

        remaining = list(answers)
        with patch.object(
            tunein.LOCAL_OPENER,
            "open",
            side_effect=lambda *_a, **_k: FakeResponse(remaining.pop(0)),
        ):
            return tunein.resolve_tunein_station("s118200")

    def test_a_pls_answer_is_read_and_its_entries_returned(self) -> None:
        playlist = "@[playlist]@NumberOfEntries=1@File1=http://example.test:8956/@"
        resolved = self._resolve(
            ["http://example.test/listen.pls", playlist.replace("@", NEWLINE)]
        )
        self.assertEqual(resolved, ("http://example.test:8956/",))

    def test_hls_inside_a_playlist_is_still_dropped(self) -> None:
        playlist = (
            "@[playlist]@File1=http://example.test/live.m3u8"
            "@File2=http://example.test:8000/stream@"
        )
        resolved = self._resolve(
            ["http://example.test/listen.pls", playlist.replace("@", NEWLINE)]
        )
        self.assertEqual(resolved, ("http://example.test:8000/stream",))

    def test_an_unreadable_playlist_leaves_the_original_url(self) -> None:
        from wambridge import tunein

        calls = {"n": 0}

        class FirstAnswer:
            def read(self, _size: int = 0) -> bytes:
                return b"http://example.test/listen.pls"

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return None

        def opener(*_a, **_k):
            calls["n"] += 1
            if calls["n"] == 1:
                return FirstAnswer()
            raise OSError("no route")

        with patch.object(tunein.LOCAL_OPENER, "open", side_effect=opener):
            self.assertEqual(
                tunein.resolve_tunein_station("s118200"),
                ("http://example.test/listen.pls",),
            )
