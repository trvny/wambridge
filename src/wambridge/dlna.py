"""UPnP AVTransport discovery and control for Samsung WAM speakers."""

from __future__ import annotations

import logging
import select
import socket
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from urllib.error import HTTPError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen
from xml.etree import ElementTree

from .discovery import SSDP_ADDRESS, local_ipv4_addresses

LOGGER = logging.getLogger(__name__)

AV_TRANSPORT_SERVICE = "urn:schemas-upnp-org:service:AVTransport:1"
MEDIA_RENDERER_DEVICE = "urn:schemas-upnp-org:device:MediaRenderer:1"
SOAP_ENVELOPE = "http://schemas.xmlsoap.org/soap/envelope/"
SOAP_ENCODING = "http://schemas.xmlsoap.org/soap/encoding/"
DIDL_LITE = "urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/"
DUBLIN_CORE = "http://purl.org/dc/elements/1.1/"
UPNP_METADATA = "urn:schemas-upnp-org:metadata-1-0/upnp/"
MP3_CONTENT_FEATURES = (
    "DLNA.ORG_PN=MP3;"
    "DLNA.ORG_OP=01;"
    "DLNA.ORG_CI=0;"
    "DLNA.ORG_FLAGS=01700000000000000000000000000000"
)
MP3_PROTOCOL_INFO = f"http-get:*:audio/mpeg:{MP3_CONTENT_FEATURES}"
USER_AGENT = "WAMBridge/0.2 UPnP/1.0"


class DlnaError(RuntimeError):
    """Raised when discovery or AVTransport control fails."""


@dataclass(frozen=True, slots=True)
class UpnpService:
    """One service resolved from a UPnP device description."""

    service_type: str
    service_id: str
    control_url: str
    event_sub_url: str = ""
    scpd_url: str = ""


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child_text(element: ElementTree.Element, name: str) -> str:
    for child in element:
        if _local_name(child.tag) == name:
            return (child.text or "").strip()
    return ""


def parse_device_description(payload: bytes, location: str) -> tuple[UpnpService, ...]:
    """Parse service URLs from one UPnP device description."""

    try:
        root = ElementTree.fromstring(payload)
    except ElementTree.ParseError as error:
        raise DlnaError(f"Invalid UPnP device description at {location}") from error

    url_base = ""
    for element in root.iter():
        if _local_name(element.tag) == "URLBase":
            url_base = (element.text or "").strip()
            if url_base:
                break
    base = url_base or location

    services: list[UpnpService] = []
    for element in root.iter():
        if _local_name(element.tag) != "service":
            continue
        service_type = _child_text(element, "serviceType")
        control_url = _child_text(element, "controlURL")
        if not service_type or not control_url:
            continue
        services.append(
            UpnpService(
                service_type=service_type,
                service_id=_child_text(element, "serviceId"),
                control_url=urljoin(base, control_url),
                event_sub_url=urljoin(base, _child_text(element, "eventSubURL")),
                scpd_url=urljoin(base, _child_text(element, "SCPDURL")),
            )
        )
    return tuple(services)


def fetch_services(location: str, *, timeout: float = 5.0) -> tuple[UpnpService, ...]:
    """Fetch and parse one UPnP device description."""

    request = Request(
        location,
        headers={
            "User-Agent": USER_AGENT,
            "Connection": "close",
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - LAN UPnP URL
            payload = response.read()
    except OSError as error:
        raise DlnaError(f"Cannot read UPnP description {location}: {error}") from error
    return parse_device_description(payload, location)


def find_service(
    services: Iterable[UpnpService],
    service_name: str,
) -> UpnpService | None:
    """Return the first service whose UPnP type matches a name or full URN."""

    for service in services:
        if service.service_type == service_name:
            return service
        if f":service:{service_name}:" in service.service_type:
            return service
    return None


def _build_msearch(target: str) -> bytes:
    return "\r\n".join(
        (
            "M-SEARCH * HTTP/1.1",
            f"HOST: {SSDP_ADDRESS[0]}:{SSDP_ADDRESS[1]}",
            'MAN: "ssdp:discover"',
            "MX: 2",
            f"ST: {target}",
            "",
            "",
        )
    ).encode("ascii")


def _parse_ssdp_headers(payload: bytes) -> dict[str, str]:
    text = payload.decode("utf-8", errors="replace")
    headers: dict[str, str] = {}
    for line in text.replace("\r\n", "\n").split("\n")[1:]:
        if ":" not in line:
            continue
        name, value = line.split(":", 1)
        headers[name.strip().lower()] = value.strip()
    return headers


def _open_ssdp_socket(local_ip: str) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        sock.setsockopt(
            socket.IPPROTO_IP,
            socket.IP_MULTICAST_IF,
            socket.inet_aton(local_ip),
        )
        sock.bind((local_ip, 0))
        sock.setblocking(False)
        return sock
    except OSError:
        sock.close()
        raise


def discover_av_transport(
    speaker_ip: str,
    *,
    timeout: float = 4.0,
    local_addresses: Iterable[str] | None = None,
) -> UpnpService:
    """Find the AVTransport control endpoint advertised by one speaker."""

    addresses = list(dict.fromkeys(local_addresses or local_ipv4_addresses()))
    sockets: list[socket.socket] = []
    locations: set[str] = set()
    targets = (AV_TRANSPORT_SERVICE, MEDIA_RENDERER_DEVICE, "ssdp:all")

    for local_ip in addresses:
        try:
            sock = _open_ssdp_socket(local_ip)
        except OSError as error:
            LOGGER.debug("Cannot use %s for DLNA discovery: %s", local_ip, error)
            continue
        try:
            for target in targets:
                message = _build_msearch(target)
                sock.sendto(message, SSDP_ADDRESS)
                sock.sendto(message, SSDP_ADDRESS)
        except OSError as error:
            LOGGER.debug("Cannot send DLNA discovery through %s: %s", local_ip, error)
            sock.close()
            continue
        sockets.append(sock)

    if not sockets:
        raise DlnaError("No usable local IPv4 interface for DLNA discovery")

    try:
        deadline = monotonic() + timeout
        while sockets and monotonic() < deadline:
            remaining = max(0.0, deadline - monotonic())
            readable, _, _ = select.select(sockets, [], [], min(remaining, 0.5))
            for sock in readable:
                try:
                    payload, sender = sock.recvfrom(65535)
                except OSError:
                    continue
                headers = _parse_ssdp_headers(payload)
                location = headers.get("location", "")
                location_host = urlparse(location).hostname
                if location and (
                    sender[0] == speaker_ip or location_host == speaker_ip
                ):
                    locations.add(location)
    finally:
        for sock in sockets:
            sock.close()

    failures: list[str] = []
    for location in sorted(locations):
        try:
            services = fetch_services(location, timeout=min(5.0, timeout + 1))
        except DlnaError as error:
            failures.append(str(error))
            continue
        service = find_service(services, "AVTransport")
        if service is not None:
            LOGGER.info("Found AVTransport at %s", service.control_url)
            return service

    details = f" ({'; '.join(failures)})" if failures else ""
    raise DlnaError(
        f"Speaker {speaker_ip} did not advertise an AVTransport service{details}"
    )


def _soap_envelope(
    service: UpnpService,
    action: str,
    arguments: Mapping[str, object],
) -> bytes:
    envelope = ElementTree.Element(
        f"{{{SOAP_ENVELOPE}}}Envelope",
        {f"{{{SOAP_ENVELOPE}}}encodingStyle": SOAP_ENCODING},
    )
    body = ElementTree.SubElement(envelope, f"{{{SOAP_ENVELOPE}}}Body")
    action_element = ElementTree.SubElement(
        body,
        f"{{{service.service_type}}}{action}",
    )
    for name, value in arguments.items():
        child = ElementTree.SubElement(action_element, name)
        child.text = str(value)
    return ElementTree.tostring(
        envelope,
        encoding="utf-8",
        xml_declaration=True,
    )


def soap_action(
    service: UpnpService,
    action: str,
    arguments: Mapping[str, object],
    *,
    timeout: float = 10.0,
) -> dict[str, str]:
    """Call one SOAP action and return leaf values from the response."""

    payload = _soap_envelope(service, action, arguments)
    request = Request(
        service.control_url,
        data=payload,
        method="POST",
        headers={
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPACTION": f'"{service.service_type}#{action}"',
            "Content-Length": str(len(payload)),
            "User-Agent": USER_AGENT,
            "Connection": "close",
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - LAN UPnP URL
            response_payload = response.read()
    except HTTPError as error:
        details = error.read().decode("utf-8", errors="replace").strip()
        suffix = f": {details[:400]}" if details else ""
        raise DlnaError(
            f"AVTransport {action} failed with HTTP {error.code}{suffix}"
        ) from error
    except OSError as error:
        raise DlnaError(f"AVTransport {action} failed: {error}") from error

    if not response_payload.strip():
        return {}
    try:
        root = ElementTree.fromstring(response_payload)
    except ElementTree.ParseError as error:
        raise DlnaError(f"AVTransport {action} returned invalid XML") from error

    for element in root.iter():
        if _local_name(element.tag) == "Fault":
            text = " ".join(
                (child.text or "").strip()
                for child in element.iter()
                if (child.text or "").strip()
            )
            raise DlnaError(f"AVTransport {action} fault: {text or 'unknown fault'}")

    values: dict[str, str] = {}
    for element in root.iter():
        if len(element) == 0 and element.text is not None:
            values[_local_name(element.tag)] = element.text.strip()
    return values


def build_mp3_metadata(uri: str, source: Path) -> str:
    """Build DIDL-Lite metadata matching Samsung's MP3 DLNA profile."""

    ElementTree.register_namespace("", DIDL_LITE)
    ElementTree.register_namespace("dc", DUBLIN_CORE)
    ElementTree.register_namespace("upnp", UPNP_METADATA)

    root = ElementTree.Element(f"{{{DIDL_LITE}}}DIDL-Lite")
    item = ElementTree.SubElement(
        root,
        f"{{{DIDL_LITE}}}item",
        {
            "id": source.stem,
            "parentID": "0",
            "restricted": "1",
        },
    )
    title = ElementTree.SubElement(item, f"{{{DUBLIN_CORE}}}title")
    title.text = source.stem
    media_class = ElementTree.SubElement(item, f"{{{UPNP_METADATA}}}class")
    media_class.text = "object.item.audioItem.musicTrack"
    resource = ElementTree.SubElement(
        item,
        f"{{{DIDL_LITE}}}res",
        {
            "protocolInfo": MP3_PROTOCOL_INFO,
            "size": str(source.stat().st_size),
        },
    )
    resource.text = uri
    return ElementTree.tostring(root, encoding="unicode")


def set_transport_uri(
    service: UpnpService,
    uri: str,
    metadata: str,
    *,
    timeout: float = 10.0,
) -> None:
    soap_action(
        service,
        "SetAVTransportURI",
        {
            "InstanceID": 0,
            "CurrentURI": uri,
            "CurrentURIMetaData": metadata,
        },
        timeout=timeout,
    )


def play(service: UpnpService, *, timeout: float = 10.0) -> None:
    soap_action(
        service,
        "Play",
        {"InstanceID": 0, "Speed": "1"},
        timeout=timeout,
    )


def pause(service: UpnpService, *, timeout: float = 10.0) -> None:
    soap_action(
        service,
        "Pause",
        {"InstanceID": 0},
        timeout=timeout,
    )


def stop(service: UpnpService, *, timeout: float = 10.0) -> None:
    soap_action(
        service,
        "Stop",
        {"InstanceID": 0},
        timeout=timeout,
    )


def get_transport_info(
    service: UpnpService,
    *,
    timeout: float = 5.0,
) -> dict[str, str]:
    return soap_action(
        service,
        "GetTransportInfo",
        {"InstanceID": 0},
        timeout=timeout,
    )
