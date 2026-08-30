"""A rejected command on the shared control socket must not stay silent.

``pcm_cli`` mutes the speaker before playback and unmutes it once audio is
flowing. Both commands travel on the same socket that carries events, whose
loop used to look only at ``StartPlaybackEvent`` and ``ErrorEvent``. A
``result="ng"`` answer to ``SetVolume`` therefore vanished, ``WAMBRIDGE
PLAYING`` was printed and the speaker stayed at volume zero, with the volume
restore skipped because startup counted as complete.
"""

from __future__ import annotations

import os
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from wambridge.pcm_cli import PlaybackWatcher
from wambridge.stream import StreamError
from wambridge.wam_events import WamEvent

CLIENT_UUID = "00000000-0000-4000-8000-000000000001"

_lease_dir = TemporaryDirectory()
_previous_lease_env: str | None = None


def setUpModule() -> None:
    # arm() below writes a real lease file - point it at a throwaway directory
    # rather than the machine-wide default every other wambridge session reads.
    # Save whatever was there rather than assuming absent, so a test runner
    # that already set this for its own reasons gets it back afterward.
    global _previous_lease_env
    _previous_lease_env = os.environ.get("WAMBRIDGE_LEASES")
    os.environ["WAMBRIDGE_LEASES"] = _lease_dir.name


def tearDownModule() -> None:
    if _previous_lease_env is None:
        os.environ.pop("WAMBRIDGE_LEASES", None)
    else:
        os.environ["WAMBRIDGE_LEASES"] = _previous_lease_env
    _lease_dir.cleanup()


def _response(method: str, result: str, **values: str) -> WamEvent:
    return WamEvent(
        method=method,
        result=result,
        user_identifier=None,
        error_code=None,
        values=dict(values),
    )


class CommandRejectionTests(TestCase):
    def _watch(self, watcher: PlaybackWatcher, events: list[WamEvent]) -> None:
        """Run the listener body over a fixed set of speaker responses."""
        with patch("wambridge.pcm_cli.WamEventConnection") as connection_class:
            connection = connection_class.return_value.__enter__.return_value
            connection.events.return_value = events
            watcher._run()

    def _armed_watcher(self) -> PlaybackWatcher:
        watcher = PlaybackWatcher("10.0.0.118", CLIENT_UUID, port=55001)
        watcher.arm()
        watcher._connection = object()  # accept commands without a socket
        return watcher

    def _send(self, watcher: PlaybackWatcher, method: str) -> None:
        with patch.object(watcher, "_connection") as connection:
            connection.send.return_value = None
            watcher._send_command(method=method)

    def test_rejected_volume_is_reported_to_the_caller(self) -> None:
        watcher = self._armed_watcher()
        self._send(watcher, "SetVolume")

        with self.assertLogs("wambridge", level="WARNING") as logs:
            self._watch(watcher, [_response("VolumeLevel", "ng", errCode="3")])

        self.assertIn("rejected SetVolume", "\n".join(logs.output))
        self.assertEqual(
            watcher.wait_for_response("SetVolume", timeout=0.0),
            "Speaker rejected SetVolume (error 3)",
        )
        # A rejected volume must not poison the whole attempt on its own.
        watcher.raise_if_failed()

    def test_accepted_volume_reports_nothing(self) -> None:
        watcher = self._armed_watcher()
        self._send(watcher, "SetVolume")

        self._watch(watcher, [_response("VolumeLevel", "ok", volume="4")])

        self.assertIsNone(watcher.wait_for_response("SetVolume", timeout=0.0))

    def test_resending_clears_the_previous_verdict(self) -> None:
        watcher = self._armed_watcher()
        self._send(watcher, "SetVolume")
        self._watch(watcher, [_response("VolumeLevel", "ng")])
        self.assertIsNotNone(watcher.wait_for_response("SetVolume", timeout=0.0))

        self._send(watcher, "SetVolume")
        self.assertIsNone(watcher.wait_for_response("SetVolume", timeout=0.0))

        self._watch(watcher, [_response("VolumeLevel", "ok")])
        self.assertIsNone(watcher.wait_for_response("SetVolume", timeout=0.0))

    def test_silent_speaker_is_not_a_rejection(self) -> None:
        watcher = self._armed_watcher()
        self._send(watcher, "SetVolume")

        self.assertIsNone(watcher.wait_for_response("SetVolume", timeout=0.05))

    def test_rejected_url_playback_is_fatal(self) -> None:
        watcher = self._armed_watcher()
        self._send(watcher, "SetUrlPlayback")

        self._watch(watcher, [_response("UrlPlayback", "ng", errCode="71")])

        with self.assertRaisesRegex(StreamError, "rejected SetUrlPlayback"):
            watcher.raise_if_failed()

    def test_unrelated_broadcast_stays_diagnostic(self) -> None:
        watcher = self._armed_watcher()
        self._send(watcher, "SetVolume")

        self._watch(watcher, [_response("MusicInfo", "ng")])

        watcher.raise_if_failed()
        self.assertIsNone(watcher.wait_for_response("SetVolume", timeout=0.0))

    def test_start_event_never_answers_a_pending_command(self) -> None:
        watcher = self._armed_watcher()
        self._send(watcher, "SetUrlPlayback")

        self._watch(
            watcher,
            [
                WamEvent(
                    method="StartPlaybackEvent",
                    result="ok",
                    user_identifier="public",
                    error_code=None,
                )
            ],
        )

        watcher.wait_for_start(timeout=0.01)
        self.assertIsNone(watcher.wait_for_response("SetUrlPlayback", timeout=0.0))
