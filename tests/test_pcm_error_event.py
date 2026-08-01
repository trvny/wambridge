from unittest import TestCase
from unittest.mock import patch

from wambridge.pcm_cli import PlaybackWatcher
from wambridge.stream import StreamError
from wambridge.wam_events import WamEvent


CLIENT_UUID = "00000000-0000-4000-8000-000000000001"


class FakeConnection:
    def __init__(self, event: WamEvent) -> None:
        self.event = event

    def __enter__(self):
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def events(self, *, stop):
        return [self.event]


class PlaybackWatcherErrorTests(TestCase):
    def test_armed_error_event_aborts_without_identity_filtering(self) -> None:
        for identifier in (
            CLIENT_UUID,
            "public",
            "00000000-0000-4000-8000-000000000099",
            None,
        ):
            with self.subTest(identifier=identifier):
                watcher = PlaybackWatcher(
                    "10.0.0.118",
                    CLIENT_UUID,
                    port=55001,
                )
                event = WamEvent(
                    method="ErrorEvent",
                    result="ng",
                    user_identifier=identifier,
                    error_code="NETWORK_TIMEOUT_ERROR",
                )
                with patch(
                    "wambridge.pcm_cli.WamEventConnection",
                    return_value=FakeConnection(event),
                ):
                    watcher.arm()
                    watcher._run()

                with self.assertRaisesRegex(
                    StreamError,
                    "NETWORK_TIMEOUT_ERROR",
                ):
                    watcher.wait_for_start(timeout=0.01)
