import argparse
from unittest import TestCase
from urllib.parse import parse_qs, urlsplit

from wambridge.event_cli import client_uuid, format_event
from wambridge.wam_events import (
    WamEvent,
    WamHttpStreamParser,
    build_mobile_request,
    parse_event,
)


def http_response(body: str, status: int = 200) -> bytes:
    payload = body.encode()
    return (
        f"HTTP/1.1 {status} OK\r\n"
        f"Content-Length: {len(payload)}\r\n"
        "Content-Type: text/xml\r\n"
        "\r\n"
    ).encode() + payload


class WamHttpStreamParserTests(TestCase):
    def test_parses_fragmented_response(self) -> None:
        parser = WamHttpStreamParser()
        message = http_response(
            "<UIC><method>CurrentFunc</method>"
            '<response result="ok"><function>wifi</function></response></UIC>'
        )

        self.assertEqual(parser.feed(message[:25]), [])
        bodies = parser.feed(message[25:])

        self.assertEqual(len(bodies), 1)
        self.assertIn("CurrentFunc", bodies[0])

    def test_parses_adjacent_responses(self) -> None:
        parser = WamHttpStreamParser()
        first = http_response(
            "<UIC><method>VolumeLevel</method>"
            '<response result="ok"><volume>3</volume></response></UIC>'
        )
        second = http_response(
            "<UIC><method>MuteStatus</method>"
            '<response result="ok"><mute>off</mute></response></UIC>'
        )

        bodies = parser.feed(first + second)

        self.assertEqual(len(bodies), 2)
        self.assertIn("VolumeLevel", bodies[0])
        self.assertIn("MuteStatus", bodies[1])

    def test_skips_non_success_http_body(self) -> None:
        parser = WamHttpStreamParser()

        self.assertEqual(parser.feed(http_response("failure", status=500)), [])


class WamEventTests(TestCase):
    def test_parses_event_fields_and_mixed_error_case(self) -> None:
        event = parse_event(
            "<UIC><method>ErrorEvent</method>"
            '<response result="ng" errCode="71">'
            "<user_identifier>abc-123</user_identifier>"
            "<objectid><![CDATA[TRACK.mp3]]></objectid>"
            "</response></UIC>"
        )

        self.assertEqual(event.method, "ErrorEvent")
        self.assertEqual(event.result, "ng")
        self.assertEqual(event.error_code, "71")
        self.assertEqual(event.user_identifier, "abc-123")
        self.assertEqual(event.values["objectid"], "TRACK.mp3")

    def test_preserves_markup_inside_cdata(self) -> None:
        event = parse_event(
            "<UIC><method>MusicInfo</method>"
            '<response result="ok">'
            "<title><![CDATA[Song <Live>]]></title>"
            "</response></UIC>"
        )

        self.assertEqual(event.values["title"], "Song <Live>")

    def test_parses_parameter_style_value(self) -> None:
        event = parse_event(
            "<UIC><method>SpkName</method>"
            '<response result="ok"><p name="spkname" val="M5"/></response></UIC>'
        )

        self.assertEqual(event.values["spkname"], "M5")

    def test_builds_official_mobile_headers(self) -> None:
        request = build_mobile_request(
            "10.0.0.118",
            "b00524c5-87b8-4439-9bb6-010545a40948",
        ).decode()
        first_line, *headers = request.split("\r\n")
        target = first_line.split(" ", 2)[1]
        parsed = urlsplit(target)

        self.assertEqual(first_line.split(" ", 2)[0], "GET")
        self.assertEqual(parsed.path, "/UIC")
        self.assertEqual(
            parse_qs(parsed.query)["cmd"],
            ["<name>GetFunc</name>"],
        )
        self.assertIn(
            "mobileUUID: b00524c5-87b8-4439-9bb6-010545a40948",
            headers,
        )
        self.assertIn("mobileName: Wireless Audio", headers)
        self.assertIn("mobileVersion: 1.0", headers)

    def test_formats_compact_summary(self) -> None:
        line = format_event(
            WamEvent(
                method="StartPlaybackEvent",
                result="ok",
                user_identifier="abc",
                error_code=None,
                values={"user_identifier": "abc", "playtime": "0"},
            )
        )

        self.assertIn("StartPlaybackEvent", line)
        self.assertIn("result=ok", line)
        self.assertIn("user=abc", line)
        self.assertIn("playtime=0", line)

    def test_validates_client_uuid(self) -> None:
        value = client_uuid("B00524C5-87B8-4439-9BB6-010545A40948")

        self.assertEqual(value, "b00524c5-87b8-4439-9bb6-010545a40948")
        with self.assertRaisesRegex(argparse.ArgumentTypeError, "valid UUID"):
            client_uuid("not-a-uuid")
