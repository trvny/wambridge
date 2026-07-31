from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from unittest import TestCase

from wambridge.dlna import (
    AV_TRANSPORT_SERVICE,
    MP3_PROTOCOL_INFO,
    UpnpService,
    build_mp3_metadata,
    parse_device_description,
    soap_action,
)


class DeviceDescriptionTests(TestCase):
    def test_resolves_avtransport_control_url(self) -> None:
        payload = b"""<?xml version="1.0"?>
        <root xmlns="urn:schemas-upnp-org:device-1-0">
          <URLBase>http://10.0.0.118:9197/</URLBase>
          <device><serviceList><service>
            <serviceType>urn:schemas-upnp-org:service:AVTransport:1</serviceType>
            <serviceId>urn:upnp-org:serviceId:AVTransport</serviceId>
            <controlURL>/upnp/control/AVTransport1</controlURL>
            <eventSubURL>/upnp/event/AVTransport1</eventSubURL>
            <SCPDURL>/AVTransport1.xml</SCPDURL>
          </service></serviceList></device>
        </root>"""

        services = parse_device_description(
            payload,
            "http://10.0.0.118:9197/description.xml",
        )

        self.assertEqual(len(services), 1)
        self.assertEqual(services[0].service_type, AV_TRANSPORT_SERVICE)
        self.assertEqual(
            services[0].control_url,
            "http://10.0.0.118:9197/upnp/control/AVTransport1",
        )

    def test_builds_mp3_didl_metadata(self) -> None:
        with TemporaryDirectory() as directory:
            source = Path(directory) / "Starburster.mp3"
            source.write_bytes(b"mp3-data")
            uri = "http://10.0.0.103:1234/DLNA/ABC.mp3"

            metadata = build_mp3_metadata(uri, source)

        self.assertIn("Starburster", metadata)
        self.assertIn(uri, metadata)
        self.assertIn('size="8"', metadata)
        self.assertIn(MP3_PROTOCOL_INFO, metadata)


class SoapActionTests(TestCase):
    def test_posts_soap_action_and_parses_transport_state(self) -> None:
        seen: dict[str, str] = {}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers["Content-Length"])
                seen["action"] = self.headers["SOAPACTION"]
                seen["body"] = self.rfile.read(length).decode()
                payload = b"""<?xml version="1.0"?>
                <s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">
                  <s:Body>
                    <u:GetTransportInfoResponse
                      xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">
                      <CurrentTransportState>PLAYING</CurrentTransportState>
                    </u:GetTransportInfoResponse>
                  </s:Body>
                </s:Envelope>"""
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *_args: object) -> None:
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        service = UpnpService(
            service_type=AV_TRANSPORT_SERVICE,
            service_id="AVTransport",
            control_url=f"http://127.0.0.1:{server.server_port}/control",
        )
        try:
            values = soap_action(
                service,
                "GetTransportInfo",
                {"InstanceID": 0},
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual(values["CurrentTransportState"], "PLAYING")
        self.assertEqual(
            seen["action"],
            f'"{AV_TRANSPORT_SERVICE}#GetTransportInfo"',
        )
        self.assertIn("<InstanceID>0</InstanceID>", seen["body"])
