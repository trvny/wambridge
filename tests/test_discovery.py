from unittest import TestCase

from wambridge.discovery import (
    build_search_message,
    candidate_subnets,
    parse_ssdp_response,
    scan_local_subnets,
)
from wambridge.samsung import WamApiError, WamResponse


class ParseSsdpResponseTests(TestCase):
    def test_parses_headers_case_insensitively(self) -> None:
        payload = (
            b"HTTP/1.1 200 OK\r\n"
            b"LOCATION: http://192.168.1.50:55001/description.xml\r\n"
            b"USN: uuid:test::urn:samsung.com:device:RemoteControlReceiver:1\r\n\r\n"
        )

        headers = parse_ssdp_response(payload)

        self.assertEqual(headers["location"], "http://192.168.1.50:55001/description.xml")
        self.assertEqual(
            headers["usn"],
            "uuid:test::urn:samsung.com:device:RemoteControlReceiver:1",
        )

    def test_builds_search_for_requested_target(self) -> None:
        payload = build_search_message("ssdp:all")

        self.assertIn(b"ST: ssdp:all\r\n", payload)
        self.assertTrue(payload.endswith(b"\r\n\r\n"))


class SubnetFallbackTests(TestCase):
    def test_deduplicates_private_subnets(self) -> None:
        networks = candidate_subnets(
            ["192.168.1.10", "192.168.1.20", "10.0.0.4", "127.0.0.1", "bad"]
        )

        self.assertEqual(
            [str(network) for network in networks],
            ["10.0.0.0/24", "192.168.1.0/24"],
        )

    def test_finds_only_host_answering_like_wam(self) -> None:
        def fake_probe(ip: str, *, port: int, timeout: float) -> WamResponse:
            self.assertEqual(port, 55001)
            self.assertEqual(timeout, 0.01)
            if ip == "192.168.1.42":
                return WamResponse(method="SpkName", result="ok", body="<UIC />")
            raise WamApiError("not a WAM")

        speakers = scan_local_subnets(
            ["192.168.1.10"],
            timeout=0.01,
            max_workers=16,
            probe_func=fake_probe,
        )

        self.assertEqual(
            [(speaker.ip, speaker.source) for speaker in speakers],
            [("192.168.1.42", "api-scan")],
        )
