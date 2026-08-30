from unittest import TestCase
from unittest.mock import patch

from wambridge.pcm_cli import PlaybackWatcher
from wambridge.stream import StreamError
from wambridge.wam_events import WamEvent

CLIENT_UUID = "00000000-0000-4000-8000-000000000001"


class PlaybackWatcherErrorTests(TestCase):
    def _run_event(
        self,
        watcher: PlaybackWatcher,
        *,
        code: str,
        identifier: str | None = None,
    ) -> None:
        event = WamEvent(
            method="ErrorEvent",
            result="ng",
            user_identifier=identifier,
            error_code=code,
        )
        with patch(
            "wambridge.pcm_cli.WamEventConnection",
        ) as connection_class:
            connection = connection_class.return_value.__enter__.return_value
            connection.events.return_value = [event]
            watcher._run()

    def test_unmatched_error_before_stream_request_is_diagnostic(self) -> None:
        watcher = PlaybackWatcher("10.0.0.118", CLIENT_UUID, port=55001)
        watcher.arm()

        self._run_event(
            watcher,
            code="NETWORK_TIMEOUT_ERROR",
            identifier="public",
        )

        with self.assertRaisesRegex(
            StreamError,
            "did not confirm StartPlaybackEvent",
        ):
            watcher.wait_for_start(timeout=0.01)

    def test_network_timeout_after_stream_request_aborts_startup(self) -> None:
        watcher = PlaybackWatcher("10.0.0.118", CLIENT_UUID, port=55001)
        watcher.arm()
        watcher.mark_stream_active()

        self._run_event(
            watcher,
            code="NETWORK_TIMEOUT_ERROR",
            identifier=None,
        )

        with self.assertRaisesRegex(
            StreamError,
            "NETWORK_TIMEOUT_ERROR",
        ):
            watcher.wait_for_start(timeout=0.01)

    def test_other_error_after_stream_request_remains_diagnostic(self) -> None:
        watcher = PlaybackWatcher("10.0.0.118", CLIENT_UUID, port=55001)
        watcher.arm()
        watcher.mark_stream_active()

        self._run_event(
            watcher,
            code="71",
            identifier=CLIENT_UUID,
        )

        with self.assertRaisesRegex(
            StreamError,
            "did not confirm StartPlaybackEvent",
        ):
            watcher.wait_for_start(timeout=0.01)

    def test_listener_failure_before_startup_remains_fatal(self) -> None:
        watcher = PlaybackWatcher("10.0.0.118", CLIENT_UUID, port=55001)

        with patch(
            "wambridge.pcm_cli.WamEventConnection",
        ) as connection_class:
            connection = connection_class.return_value.__enter__.return_value
            connection.events.side_effect = ValueError("malformed response")
            watcher._run()

        with self.assertRaisesRegex(StreamError, "malformed response"):
            watcher.raise_if_failed()

    def test_listener_failure_after_startup_is_diagnostic(self) -> None:
        watcher = PlaybackWatcher("10.0.0.118", CLIENT_UUID, port=55001)
        watcher.mark_startup_complete()

        with (
            patch(
                "wambridge.pcm_cli.WamEventConnection",
            ) as connection_class,
            self.assertLogs("wambridge", level="WARNING") as logs,
        ):
            connection = connection_class.return_value.__enter__.return_value
            connection.events.side_effect = ValueError("malformed response")
            watcher._run()

        watcher.raise_if_failed()
        self.assertIn(
            "continuing active PCM stream",
            "\n".join(logs.output),
        )

    def test_start_event_keeps_shared_connection_available(self) -> None:
        watcher = PlaybackWatcher("10.0.0.118", CLIENT_UUID, port=55001)
        watcher.arm()
        event = WamEvent(
            method="StartPlaybackEvent",
            result="ok",
            user_identifier=CLIENT_UUID,
            error_code=None,
        )

        with patch(
            "wambridge.pcm_cli.WamEventConnection",
        ) as connection_class:
            connection = connection_class.return_value.__enter__.return_value

            def events(*, stop):
                yield event
                stop.wait(timeout=1.0)

            connection.events.side_effect = events
            with watcher:
                watcher.wait_for_start(timeout=0.1)
                # The speaker fetched the stream, which is what makes this a
                # session worth releasing on the way out.
                watcher.mark_stream_active()
                watcher.set_volume(7)

            connection.send.assert_any_call(
                method="SetVolume",
                arguments=[("volume", 7, "dec")],
                power_on=True,
            )
            # Leaving the block releases the speaker over this same connection,
            # so the volume command is no longer the last thing sent.
            connection.send.assert_called_with(
                method="SetPlaybackControl",
                arguments=[("playbackcontrol", "pause", "str")],
                power_on=False,
            )
