"""Small Samsung-compatible UPnP media server for one MP3 file."""

from __future__ import annotations

import logging
import re
import secrets
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from xml.etree import ElementTree

from .dlna import MP3_CONTENT_FEATURES, MP3_PROTOCOL_INFO

LOGGER = logging.getLogger(__name__)
COPY_CHUNK_SIZE = 64 * 1024
SOAP_ENVELOPE = "http://schemas.xmlsoap.org/soap/envelope/"
SOAP_ENCODING = "http://schemas.xmlsoap.org/soap/encoding/"
CONTENT_DIRECTORY = "urn:schemas-upnp-org:service:ContentDirectory:1"
CONNECTION_MANAGER = "urn:schemas-upnp-org:service:ConnectionManager:1"
DIDL_LITE = "urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/"
DUBLIN_CORE = "http://purl.org/dc/elements/1.1/"
UPNP_METADATA = "urn:schemas-upnp-org:metadata-1-0/upnp/"


class ByteRangeError(ValueError):
    """Raised when an HTTP Range header cannot be satisfied."""


def parse_byte_range(value: str | None, size: int) -> tuple[int, int] | None:
    """Parse one RFC 7233 byte range and return inclusive bounds."""

    if value is None:
        return None
    if not value.startswith("bytes=") or "," in value:
        raise ByteRangeError("Only one byte range is supported")
    spec = value[6:].strip()
    if "-" not in spec:
        raise ByteRangeError("Invalid byte range")
    first, last = (part.strip() for part in spec.split("-", 1))
    if not first:
        try:
            suffix_length = int(last)
        except ValueError as error:
            raise ByteRangeError("Invalid suffix range") from error
        if suffix_length <= 0:
            raise ByteRangeError("Invalid suffix range")
        start = max(0, size - suffix_length)
        return start, size - 1
    try:
        start = int(first)
        end = size - 1 if not last else int(last)
    except ValueError as error:
        raise ByteRangeError("Invalid byte range") from error
    if start < 0 or end < start or start >= size:
        raise ByteRangeError("Unsatisfiable byte range")
    return start, min(end, size - 1)


def _synchsafe(value: bytes) -> int:
    if len(value) != 4:
        return 0
    return (
        (value[0] & 0x7F) << 21
        | (value[1] & 0x7F) << 14
        | (value[2] & 0x7F) << 7
        | (value[3] & 0x7F)
    )


def _mpeg_frame(header: bytes) -> tuple[int, int] | None:
    """Return frame length and samples-per-frame for an MPEG audio header."""

    if len(header) < 4 or header[0] != 0xFF or header[1] & 0xE0 != 0xE0:
        return None
    version_bits = (header[1] >> 3) & 0x03
    layer_bits = (header[1] >> 1) & 0x03
    bitrate_index = (header[2] >> 4) & 0x0F
    sample_index = (header[2] >> 2) & 0x03
    padding = (header[2] >> 1) & 0x01
    if version_bits == 1 or layer_bits == 0 or bitrate_index in {0, 15}:
        return None
    if sample_index == 3:
        return None

    version = {3: 1, 2: 2, 0: 25}[version_bits]
    layer = {3: 1, 2: 2, 1: 3}[layer_bits]
    sample_rate = (44100, 48000, 32000)[sample_index]
    if version == 2:
        sample_rate //= 2
    elif version == 25:
        sample_rate //= 4

    if layer == 1:
        bitrates = (
            0,
            32,
            64,
            96,
            128,
            160,
            192,
            224,
            256,
            288,
            320,
            352,
            384,
            416,
            448,
        )
        bitrate = bitrates[bitrate_index] * 1000
        return ((12 * bitrate // sample_rate) + padding) * 4, 384
    if layer == 2:
        bitrates = (
            0,
            32,
            48,
            56,
            64,
            80,
            96,
            112,
            128,
            160,
            192,
            224,
            256,
            320,
            384,
        )
        bitrate = bitrates[bitrate_index] * 1000
        return 144 * bitrate // sample_rate + padding, 1152

    if version == 1:
        bitrates = (
            0,
            32,
            40,
            48,
            56,
            64,
            80,
            96,
            112,
            128,
            160,
            192,
            224,
            256,
            320,
        )
        samples = 1152
        coefficient = 144
    else:
        bitrates = (
            0,
            8,
            16,
            24,
            32,
            40,
            48,
            56,
            64,
            80,
            96,
            112,
            128,
            144,
            160,
        )
        samples = 576
        coefficient = 72
    bitrate = bitrates[bitrate_index] * 1000
    return coefficient * bitrate // sample_rate + padding, samples


def mp3_duration_ms(source: str | Path) -> int | None:
    """Estimate MP3 duration by walking MPEG frames, including VBR files."""

    data = Path(source).read_bytes()
    offset = 0
    if len(data) >= 10 and data[:3] == b"ID3":
        offset = 10 + _synchsafe(data[6:10])
        if data[5] & 0x10:
            offset += 10

    total_samples = 0
    sample_rate: int | None = None
    position = offset
    while position + 4 <= len(data):
        frame = _mpeg_frame(data[position : position + 4])
        if frame is None:
            position += 1
            continue
        frame_length, samples = frame
        version_bits = (data[position + 1] >> 3) & 0x03
        sample_index = (data[position + 2] >> 2) & 0x03
        rate = (44100, 48000, 32000)[sample_index]
        if version_bits == 2:
            rate //= 2
        elif version_bits == 0:
            rate //= 4
        if frame_length <= 4 or position + frame_length > len(data):
            position += 1
            continue
        sample_rate = rate
        total_samples += samples
        position += frame_length

    if not sample_rate or not total_samples:
        return None
    return round(total_samples * 1000 / sample_rate)


def format_duration(milliseconds: int | None) -> str | None:
    if milliseconds is None:
        return None
    seconds, millis = divmod(milliseconds, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}.{millis:03d}"


class _DlnaHttpServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class DlnaFileServer:
    """Expose one MP3 as a tiny UPnP MediaServer understood by Samsung WAM."""

    def __init__(
        self,
        source: str | Path,
        *,
        bind: str = "0.0.0.0",  # nosec B104 - speaker must reach the LAN server
        port: int = 0,
    ) -> None:
        path = Path(source).expanduser().resolve()
        if not path.is_file():
            raise ValueError(f"DLNA source is not a file: {path}")
        if path.suffix.casefold() != ".mp3":
            raise ValueError("Samsung share playback currently supports local MP3 only")

        self.source = path
        self.size = path.stat().st_size
        if self.size == 0:
            raise ValueError("DLNA source file is empty")
        self.uuid = str(uuid.uuid4())
        self.udn = f"uuid:{self.uuid}"
        self.object_id = secrets.token_hex(16).upper()
        self.duration_ms = mp3_duration_ms(path)
        self.duration = format_duration(self.duration_ms)
        self.description_requested = threading.Event()
        self.browse_requested = threading.Event()
        self.request_started = threading.Event()
        self.request_finished = threading.Event()
        self._started = False
        self._closing = threading.Event()
        self._lock = threading.Lock()
        self._request_count = 0
        self._bytes_sent = 0
        self._server = _DlnaHttpServer((bind, port), self._make_handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    @property
    def path(self) -> str:
        return f"/DLNA/{self.object_id}.mp3"

    @property
    def protocol_info(self) -> str:
        return MP3_PROTOCOL_INFO

    @property
    def request_count(self) -> int:
        with self._lock:
            return self._request_count

    @property
    def bytes_sent(self) -> int:
        with self._lock:
            return self._bytes_sent

    def url(self, host: str) -> str:
        return f"http://{host}:{self.port}{self.path}"

    def description_url(self, host: str) -> str:
        return f"http://{host}:{self.port}/description.xml"

    def start(self) -> None:
        self._thread.start()
        self._started = True

    def close(self) -> None:
        self._closing.set()
        if self._started:
            self._server.shutdown()
            if self._thread.is_alive():
                self._thread.join(timeout=3)
        self._server.server_close()

    def _record_request(self, bytes_sent: int) -> None:
        with self._lock:
            self._request_count += 1
            self._bytes_sent += bytes_sent

    def _device_description(self) -> bytes:
        return f'''<?xml version="1.0" encoding="UTF-8"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
  <specVersion><major>1</major><minor>0</minor></specVersion>
  <device>
    <dlna:X_DLNADOC xmlns:dlna="urn:schemas-dlna-org:device-1-0">DMS-1.50</dlna:X_DLNADOC>
    <deviceType>urn:schemas-upnp-org:device:MediaServer:1</deviceType>
    <friendlyName>WAM Bridge ({self.source.stem})</friendlyName>
    <manufacturer>trvny</manufacturer>
    <manufacturerURL>https://github.com/trvny/wambridge</manufacturerURL>
    <modelDescription>Single-file Samsung WAM media server</modelDescription>
    <modelName>WAM Bridge</modelName>
    <modelNumber>1</modelNumber>
    <serialNumber>{self.object_id}</serialNumber>
    <UDN>{self.udn}</UDN>
    <serviceList>
      <service>
        <serviceType>{CONTENT_DIRECTORY}</serviceType>
        <serviceId>urn:upnp-org:serviceId:ContentDirectory</serviceId>
        <SCPDURL>/DLNA/cdsdescription.xml</SCPDURL>
        <controlURL>/DLNA/cdscontrol</controlURL>
        <eventSubURL>/DLNA/cdsevent</eventSubURL>
      </service>
      <service>
        <serviceType>{CONNECTION_MANAGER}</serviceType>
        <serviceId>urn:upnp-org:serviceId:ConnectionManager</serviceId>
        <SCPDURL>/DLNA/cmsdescription.xml</SCPDURL>
        <controlURL>/DLNA/cmscontrol</controlURL>
        <eventSubURL>/DLNA/cmsevent</eventSubURL>
      </service>
    </serviceList>
  </device>
</root>'''.encode()

    @staticmethod
    def _content_directory_description() -> bytes:
        return b'''<?xml version="1.0"?>
<scpd xmlns="urn:schemas-upnp-org:service-1-0">
 <specVersion><major>1</major><minor>0</minor></specVersion>
 <actionList>
  <action><name>Browse</name></action>
  <action><name>GetSearchCapabilities</name></action>
  <action><name>GetSortCapabilities</name></action>
  <action><name>GetSystemUpdateID</name></action>
 </actionList>
 <serviceStateTable/>
</scpd>'''

    @staticmethod
    def _connection_manager_description() -> bytes:
        return b'''<?xml version="1.0"?>
<scpd xmlns="urn:schemas-upnp-org:service-1-0">
 <specVersion><major>1</major><minor>0</minor></specVersion>
 <actionList>
  <action><name>GetProtocolInfo</name></action>
  <action><name>GetCurrentConnectionIDs</name></action>
  <action><name>GetCurrentConnectionInfo</name></action>
 </actionList>
 <serviceStateTable/>
</scpd>'''

    def _didl(self, host: str) -> str:
        ElementTree.register_namespace("", DIDL_LITE)
        ElementTree.register_namespace("dc", DUBLIN_CORE)
        ElementTree.register_namespace("upnp", UPNP_METADATA)
        root = ElementTree.Element(f"{{{DIDL_LITE}}}DIDL-Lite")
        item = ElementTree.SubElement(
            root,
            f"{{{DIDL_LITE}}}item",
            {"id": self.object_id, "parentID": "0", "restricted": "1"},
        )
        ElementTree.SubElement(item, f"{{{DUBLIN_CORE}}}title").text = self.source.stem
        ElementTree.SubElement(item, f"{{{UPNP_METADATA}}}artist").text = "WAM Bridge"
        ElementTree.SubElement(item, f"{{{UPNP_METADATA}}}class").text = (
            "object.item.audioItem.musicTrack"
        )
        attributes = {
            "protocolInfo": MP3_PROTOCOL_INFO,
            "size": str(self.size),
        }
        if self.duration:
            attributes["duration"] = self.duration
        ElementTree.SubElement(item, f"{{{DIDL_LITE}}}res", attributes).text = self.url(host)
        return ElementTree.tostring(root, encoding="unicode")

    @staticmethod
    def _action_name(handler: BaseHTTPRequestHandler, body: bytes) -> str:
        soap_action = handler.headers.get("SOAPACTION", "").strip('"')
        if "#" in soap_action:
            return soap_action.rsplit("#", 1)[-1]
        text = body.decode("utf-8", errors="ignore")
        for match in re.finditer(
            r"<(?:[A-Za-z_][\w.-]*:)?([A-Za-z_][\w.-]*)\b",
            text,
        ):
            if match.group(1) not in {"Envelope", "Body"}:
                return match.group(1)
        return ""

    @staticmethod
    def _soap_response(service: str, action: str, values: dict[str, str]) -> bytes:
        envelope = ElementTree.Element(
            f"{{{SOAP_ENVELOPE}}}Envelope",
            {f"{{{SOAP_ENVELOPE}}}encodingStyle": SOAP_ENCODING},
        )
        body = ElementTree.SubElement(envelope, f"{{{SOAP_ENVELOPE}}}Body")
        response = ElementTree.SubElement(body, f"{{{service}}}{action}Response")
        for name, value in values.items():
            ElementTree.SubElement(response, name).text = value
        return ElementTree.tostring(envelope, encoding="utf-8", xml_declaration=True)

    def _make_handler(self) -> type[BaseHTTPRequestHandler]:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_HEAD(self) -> None:  # noqa: N802
                self._route_get(include_body=False)

            def do_GET(self) -> None:  # noqa: N802
                self._route_get(include_body=True)

            def do_POST(self) -> None:  # noqa: N802
                path = urlsplit(self.path).path
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    length = 0
                body = self.rfile.read(max(0, length))
                action = owner._action_name(self, body)
                host = self.headers.get("Host", f"127.0.0.1:{owner.port}").split(":", 1)[0]
                LOGGER.debug("Samsung DMS SOAP %s %s", path, action)

                if path == "/DLNA/cdscontrol":
                    if action == "Browse":
                        owner.browse_requested.set()
                        payload = owner._soap_response(
                            CONTENT_DIRECTORY,
                            action,
                            {
                                "Result": owner._didl(host),
                                "NumberReturned": "1",
                                "TotalMatches": "1",
                                "UpdateID": "1",
                            },
                        )
                    elif action == "GetSystemUpdateID":
                        payload = owner._soap_response(
                            CONTENT_DIRECTORY, action, {"Id": "1"}
                        )
                    elif action == "GetSearchCapabilities":
                        payload = owner._soap_response(
                            CONTENT_DIRECTORY, action, {"SearchCaps": ""}
                        )
                    elif action == "GetSortCapabilities":
                        payload = owner._soap_response(
                            CONTENT_DIRECTORY, action, {"SortCaps": ""}
                        )
                    else:
                        self.send_error(500, "Unsupported ContentDirectory action")
                        return
                elif path == "/DLNA/cmscontrol":
                    if action == "GetProtocolInfo":
                        values = {"Source": MP3_PROTOCOL_INFO, "Sink": ""}
                    elif action == "GetCurrentConnectionIDs":
                        values = {"ConnectionIDs": "0"}
                    elif action == "GetCurrentConnectionInfo":
                        values = {
                            "RcsID": "-1",
                            "AVTransportID": "-1",
                            "ProtocolInfo": MP3_PROTOCOL_INFO,
                            "PeerConnectionManager": "",
                            "PeerConnectionID": "-1",
                            "Direction": "Output",
                            "Status": "OK",
                        }
                    else:
                        self.send_error(500, "Unsupported ConnectionManager action")
                        return
                    payload = owner._soap_response(CONNECTION_MANAGER, action, values)
                else:
                    self.send_error(404)
                    return

                self.send_response(200)
                self.send_header("Content-Type", 'text/xml; charset="utf-8"')
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Connection", "close")
                self.send_header("EXT", "")
                self.end_headers()
                self.wfile.write(payload)

            def do_SUBSCRIBE(self) -> None:  # noqa: N802
                self.send_response(200)
                self.send_header("SID", f"uuid:{uuid.uuid4()}")
                self.send_header("TIMEOUT", "Second-1800")
                self.send_header("Content-Length", "0")
                self.send_header("Connection", "close")
                self.end_headers()

            def do_UNSUBSCRIBE(self) -> None:  # noqa: N802
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.send_header("Connection", "close")
                self.end_headers()

            def _route_get(self, *, include_body: bool) -> None:
                path = urlsplit(self.path).path
                if path in {"/description.xml", "/DLNA/description.xml"}:
                    owner.description_requested.set()
                    self._send_xml(owner._device_description(), include_body)
                elif path == "/DLNA/cdsdescription.xml":
                    self._send_xml(owner._content_directory_description(), include_body)
                elif path == "/DLNA/cmsdescription.xml":
                    self._send_xml(owner._connection_manager_description(), include_body)
                elif path == owner.path:
                    self._serve_file(include_body=include_body)
                else:
                    self.send_error(404)

            def _send_xml(self, payload: bytes, include_body: bool) -> None:
                self.send_response(200)
                self.send_header("Content-Type", 'text/xml; charset="utf-8"')
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Connection", "close")
                self.end_headers()
                if include_body:
                    self.wfile.write(payload)

            def _serve_file(self, *, include_body: bool) -> None:
                try:
                    byte_range = parse_byte_range(self.headers.get("Range"), owner.size)
                except ByteRangeError:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{owner.size}")
                    self.send_header("Content-Length", "0")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    return

                if byte_range is None:
                    start, end = 0, owner.size - 1
                    status = 200
                else:
                    start, end = byte_range
                    status = 206
                length = end - start + 1

                self.send_response(status)
                self.send_header("Content-Type", "audio/mpeg")
                self.send_header("Content-Length", str(length))
                self.send_header("Accept-Ranges", "bytes")
                if status == 206:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{owner.size}")
                self.send_header("transferMode.dlna.org", "Streaming")
                self.send_header("contentFeatures.dlna.org", MP3_CONTENT_FEATURES)
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.send_header("EXT", "")
                self.end_headers()

                if not include_body:
                    return

                owner.request_started.set()
                sent = 0
                try:
                    with owner.source.open("rb") as stream:
                        stream.seek(start)
                        remaining = length
                        while remaining and not owner._closing.is_set():
                            chunk = stream.read(min(COPY_CHUNK_SIZE, remaining))
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                            sent += len(chunk)
                            remaining -= len(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    LOGGER.info("Speaker closed DLNA file request")
                finally:
                    owner._record_request(sent)
                    owner.request_finished.set()

            def log_message(self, format_string: str, *args: object) -> None:
                LOGGER.debug("Samsung DMS HTTP: " + format_string, *args)

        return Handler
