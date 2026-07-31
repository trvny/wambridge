"""Samsung-compatible DMS identity, commands and SSDP diagnostics."""

from __future__ import annotations

import logging
import socket
import threading
from collections.abc import Iterable
from pathlib import Path
from xml.sax.saxutils import escape

from .dlna_server import CONNECTION_MANAGER, CONTENT_DIRECTORY, DlnaFileServer
from .samsung import DEFAULT_PORT, WamResponse, request

LOGGER = logging.getLogger(__name__)
DEFAULT_DMS_PORT = 3921
MEDIA_SERVER_DEVICE_TYPE = "urn:schemas-upnp-org:device:MediaServer:1"
SSDP_ADDRESS = "239.255.255.250"
SSDP_PORT = 1900
SSDP_SERVER = "Windows/10 UPnP/1.0 Samsung-RoomSpeaker/1.0"


class SamsungDmsServer(DlnaFileServer):
    """Expose one MP3 with metadata close to Samsung Multiroom's native DMS."""

    def __init__(
        self,
        source: str | Path,
        *,
        bind: str = "0.0.0.0",  # nosec B104 - speaker must reach the LAN server
        port: int = DEFAULT_DMS_PORT,
    ) -> None:
        super().__init__(source, bind=bind, port=port)

    @property
    def has_contact(self) -> bool:
        """Return whether the speaker contacted any MediaServer endpoint."""

        return any(
            event.is_set()
            for event in (
                self.description_requested,
                self.browse_requested,
                self.request_started,
            )
        )

    def _device_description(self) -> bytes:
        friendly_name = escape(f"WAM Bridge ({self.source.stem})")
        serial = escape(self.uuid)
        udn = escape(self.udn)
        return f'''<?xml version="1.0" encoding="UTF-8"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
  <specVersion><major>1</major><minor>0</minor></specVersion>
  <device>
    <dlna:X_DLNADOC xmlns:dlna="urn:schemas-dlna-org:device-1-0">DMS-1.50</dlna:X_DLNADOC>
    <deviceType>{MEDIA_SERVER_DEVICE_TYPE}</deviceType>
    <friendlyName>{friendly_name}</friendlyName>
    <manufacturer>SEC</manufacturer>
    <manufacturerURL>http://www.samsung.com/sec</manufacturerURL>
    <modelDescription>Room Speaker 2</modelDescription>
    <modelName>SAMSUNG VISUAL DIGITAL</modelName>
    <modelNumber>1.0</modelNumber>
    <modelURL>http://www.samsung.com/sec/roomspeaker</modelURL>
    <serialNumber>{serial}</serialNumber>
    <UDN>{udn}</UDN>
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


def set_ip_info_apk(
    speaker_ip: str,
    server_uuid: str,
    server_address: str,
    *,
    port: int = DEFAULT_PORT,
    timeout: float = 3.0,
) -> WamResponse:
    """Send the literal SetIpInfo form found in Samsung Multiroom."""

    if not server_uuid or server_uuid.startswith("uuid:"):
        raise ValueError("Server UUID must be raw, without the uuid: prefix")
    if not server_address:
        raise ValueError("Server address must be host:port")
    return request(
        speaker_ip,
        "SetIpInfo",
        [("uuid", server_uuid, "str"), ("ip", server_address, "str")],
        port=port,
        timeout=timeout,
    )


def play_share_control_apk(
    speaker_ip: str,
    *,
    source_name: str,
    device_udn: str,
    object_id: str,
    playtime: int = 0,
    port: int = DEFAULT_PORT,
    timeout: float = 3.0,
) -> WamResponse:
    """Send Samsung Multiroom's literal SetSharePlaybackControl command."""

    _validate_target(source_name, device_udn, object_id)
    _validate_non_negative("Playtime", playtime)
    return request(
        speaker_ip,
        "SetSharePlaybackControl",
        [
            ("playbackcontrol", "play", "str"),
            ("playertype", "allshare", "str"),
            ("sourcename", source_name, "cdata"),
            ("playtime", playtime, "dec"),
            ("device_udn", device_udn, "str"),
            ("objectid", object_id, "str"),
        ],
        port=port,
        timeout=timeout,
    )


def play_new_folder_control_apk(
    speaker_ip: str,
    *,
    source_name: str,
    device_udn: str,
    object_id: str,
    parent_id: str = "0",
    play_index: int = 0,
    playtime: int = 0,
    port: int = DEFAULT_PORT,
    timeout: float = 3.0,
) -> WamResponse:
    """Send the literal SetNewFolderPlaybackControl fallback from the APK."""

    _validate_target(source_name, device_udn, object_id)
    if not parent_id:
        raise ValueError("Parent ID cannot be empty")
    _validate_non_negative("Play index", play_index)
    _validate_non_negative("Playtime", playtime)
    return request(
        speaker_ip,
        "SetNewFolderPlaybackControl",
        [
            ("device_udn", device_udn, "str"),
            ("playbackcontol", "play", "str"),
            ("playertype", "allshare", "str"),
            ("sourcename", source_name, "cdata"),
            ("parentid", parent_id, "str"),
            ("playindex", play_index, "dec"),
            ("playtime", playtime, "dec"),
            ("objectid", object_id, "str"),
        ],
        port=port,
        timeout=timeout,
        power_on=True,
    )


def _validate_target(source_name: str, device_udn: str, object_id: str) -> None:
    if not source_name:
        raise ValueError("Source name cannot be empty")
    if not device_udn:
        raise ValueError("MediaServer UDN cannot be empty")
    if not object_id:
        raise ValueError("Object ID cannot be empty")


def _validate_non_negative(label: str, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")


class SsdpAdvertiser:
    """Advertise one UPnP device and answer local M-SEARCH requests."""

    def __init__(
        self,
        *,
        host_ip: str,
        location: str,
        udn: str,
        device_type: str = MEDIA_SERVER_DEVICE_TYPE,
        service_types: Iterable[str] = (CONTENT_DIRECTORY, CONNECTION_MANAGER),
    ) -> None:
        self.host_ip = host_ip
        self.location = location
        self.udn = udn
        self.device_type = device_type
        self.service_types = tuple(service_types)
        self._socket: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._closing = threading.Event()

    @property
    def targets(self) -> tuple[str, ...]:
        return ("upnp:rootdevice", self.udn, self.device_type, *self.service_types)

    def start(self) -> None:
        """Start the responder when UDP 1900 can be shared on this host."""

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((self.host_ip, SSDP_PORT))
            membership = socket.inet_aton(SSDP_ADDRESS) + socket.inet_aton(
                self.host_ip
            )
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
            sock.setsockopt(
                socket.IPPROTO_IP,
                socket.IP_MULTICAST_IF,
                socket.inet_aton(self.host_ip),
            )
            sock.settimeout(0.5)
        except OSError as error:
            sock.close()
            LOGGER.warning(
                "Could not listen for SSDP M-SEARCH on UDP 1900: %s; "
                "multicast announcements will still be sent",
                error,
            )
            return

        self._socket = sock
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        LOGGER.info("SSDP responder listening on %s:%s", self.host_ip, SSDP_PORT)

    def announce(self, repeats: int = 2) -> None:
        """Send ssdp:alive notifications for the root, device and services."""

        LOGGER.debug("Sending SSDP alive announcements (%s round(s))", repeats)
        for _ in range(max(1, repeats)):
            for target in self.targets:
                self._send_multicast(self._notify_payload(target, "ssdp:alive"))

    def close(self) -> None:
        """Send byebye and stop the responder."""

        for target in self.targets:
            self._send_multicast(self._notify_payload(target, "ssdp:byebye"))
        self._closing.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2)
        if self._socket is not None:
            self._socket.close()
            self._socket = None

    def _serve(self) -> None:
        assert self._socket is not None
        while not self._closing.is_set():
            try:
                data, address = self._socket.recvfrom(65535)
            except TimeoutError:
                continue
            except OSError:
                return
            text = data.decode("iso-8859-1", errors="ignore")
            if not text.upper().startswith("M-SEARCH * HTTP/1.1"):
                continue
            headers = self._headers(text)
            if headers.get("man", "").strip('"').casefold() != "ssdp:discover":
                continue
            requested = headers.get("st", "ssdp:all")
            LOGGER.debug(
                "Samsung DMS SSDP M-SEARCH from %s for %s",
                address[0],
                requested,
            )
            for target in self._matching_targets(requested):
                try:
                    self._socket.sendto(self._search_response(target), address)
                except OSError as error:
                    LOGGER.debug("Could not answer SSDP request: %s", error)

    def _matching_targets(self, requested: str) -> tuple[str, ...]:
        normalized = requested.casefold()
        if normalized == "ssdp:all":
            return self.targets
        return tuple(
            target for target in self.targets if target.casefold() == normalized
        )

    def _send_multicast(self, payload: bytes) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        try:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
            sock.setsockopt(
                socket.IPPROTO_IP,
                socket.IP_MULTICAST_IF,
                socket.inet_aton(self.host_ip),
            )
            sock.sendto(payload, (SSDP_ADDRESS, SSDP_PORT))
        except OSError as error:
            LOGGER.debug("Could not send SSDP notification: %s", error)
        finally:
            sock.close()

    def _notify_payload(self, target: str, subtype: str) -> bytes:
        lines = [
            "NOTIFY * HTTP/1.1",
            f"HOST: {SSDP_ADDRESS}:{SSDP_PORT}",
            "CACHE-CONTROL: max-age=1800",
            f"LOCATION: {self.location}",
            f"NT: {target}",
            f"NTS: {subtype}",
            f"SERVER: {SSDP_SERVER}",
            f"USN: {self._usn(target)}",
            "",
            "",
        ]
        return "\r\n".join(lines).encode("ascii")

    def _search_response(self, target: str) -> bytes:
        lines = [
            "HTTP/1.1 200 OK",
            "CACHE-CONTROL: max-age=1800",
            "EXT:",
            f"LOCATION: {self.location}",
            f"SERVER: {SSDP_SERVER}",
            f"ST: {target}",
            f"USN: {self._usn(target)}",
            "",
            "",
        ]
        return "\r\n".join(lines).encode("ascii")

    def _usn(self, target: str) -> str:
        if target == self.udn:
            return self.udn
        return f"{self.udn}::{target}"

    @staticmethod
    def _headers(message: str) -> dict[str, str]:
        headers: dict[str, str] = {}
        for line in message.split("\r\n")[1:]:
            if ":" not in line:
                continue
            name, value = line.split(":", 1)
            headers[name.strip().casefold()] = value.strip()
        return headers
