"""An unrelated event must never be read as a rejected command.

The firmware answers a command with whatever it happens to be broadcasting.
Treating that body as the command's own result produced two real failures:
``SetIpInfo`` appearing to return ``CurrentFunc``, and radio playback aborting
with "rejected PausePlaybackEvent" while the speaker was working normally.
"""

from __future__ import annotations

import unittest

from wambridge.samsung import WamApiError, parse_response

OK_BODY = (
    "<UIC><method>VolumeLevel</method>"
    '<response result="ok"><volume>4</volume></response></UIC>'
)
EVENT_BODY = (
    "<UIC><method>PausePlaybackEvent</method>"
    '<response result="ng"></response></UIC>'
)
CHILD_ERROR_BODY = (
    "<UIC><method>MusicInfo</method>"
    '<response result="ng"><errCode>No Music</errCode></response></UIC>'
)


class ResponseMatchingTests(unittest.TestCase):
    def test_matching_ok_response_is_returned(self) -> None:
        response = parse_response(OK_BODY, expected="SetVolume")
        self.assertTrue(response.matched)
        self.assertEqual(response.values["volume"], "4")

    def test_unrelated_event_is_not_a_rejection(self) -> None:
        response = parse_response(EVENT_BODY, expected="SetUrlPlayback")
        self.assertFalse(response.matched)
        self.assertEqual(response.method, "PausePlaybackEvent")

    def test_unrelated_event_after_registration_is_not_a_rejection(self) -> None:
        body = '<UIC><method>CurrentFunc</method><response result="ng"/></UIC>'
        response = parse_response(body, expected="SetIpInfo")
        self.assertFalse(response.matched)

    def test_genuine_rejection_still_raises(self) -> None:
        body = (
            "<UIC><method>VolumeLevel</method>"
            '<response result="ng"></response></UIC>'
        )
        with self.assertRaises(WamApiError):
            parse_response(body, expected="SetVolume")

    def test_without_expectation_behaviour_is_unchanged(self) -> None:
        with self.assertRaises(WamApiError):
            parse_response(EVENT_BODY)

    def test_child_element_error_code_is_reported(self) -> None:
        # The firmware spells it errCode as a child element, not an attribute.
        with self.assertRaises(WamApiError) as caught:
            parse_response(CHILD_ERROR_BODY, expected="SetSharePlaybackControl")
        self.assertIn("No Music", str(caught.exception))


class MethodAgreementTests(unittest.TestCase):
    """Names measured on a physical M5, command paired with its real reply."""

    CASES = (
        ("SetVolume", "VolumeLevel"),
        ("GetVolume", "VolumeLevel"),
        ("SetIpInfo", "IpInfo"),
        ("GetFunc", "CurrentFunc"),
        ("GetMainInfo", "MainInfo"),
        ("GetPlayStatus", "PlayStatus"),
        ("SetUrlPlayback", "UrlPlayback"),
        ("SetPlaybackControl", "PlaybackStatus"),
        ("SetSharePlaybackControl", "MusicInfo"),
    )

    def test_known_pairs_agree(self) -> None:
        for command, reply in self.CASES:
            with self.subTest(command=command):
                body = f'<UIC><method>{reply}</method><response result="ok"/></UIC>'
                self.assertTrue(parse_response(body, expected=command).matched)

    def test_unrelated_pairs_disagree(self) -> None:
        body = '<UIC><method>MuteStatus</method><response result="ok"/></UIC>'
        self.assertFalse(parse_response(body, expected="SetIpInfo").matched)


if __name__ == "__main__":
    unittest.main()
