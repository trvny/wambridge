"""SSDP and local-network discovery for Samsung WAM speakers."""

from __future__ import annotations

import logging
import select
import shutil
import socket
import subprocess
import sys
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Network
from time import monotonic
from urllib.parse import urlparse

from .samsung import DEFAULT_PORT, WamApiError, WamResponse, probe

LOGGER = logging.getLogger(__name__)
SSDP_ADDRESS = ("239.255.255.250", 1900)
WAM_SEARCH_TARGET = "urn:samsung.com:device:RemoteControlReceiver:1"
SSDP_SEARCH_TARGETS = (WAM_SEARCH_TARGET, "ssdp:all")


@dataclass(frozen=True, slots=True)
class DiscoveredSpeaker:
    """A Samsung WAM device found in the local network."""

    ip: str
    location: str = ""
    usn: str | None = None
    source: str = "ssdp"


def parse_ssdp_response(payload: bytes) -> dict[str, str]:
    """Parse an SSDP response into lower-case header names."""
    text = payload.decode("utf-8", errors="replace")
    headers: dict[str, str] = {}
    for line in text.replace("\r\n", "\n").split("\n")[1:]:
        if ":" not in line:
            continue
        name, value = line.split(":", 1)
        headers[name.strip().lower()] = value.strip()
    return headers


def build_search_message(target: str) -> bytes:
    """Build one SSDP M-SEARCH request."""
    return "\r\n".join(
        [
            "M-SEARCH * HTTP/1.1",
            f"HOST: {SSDP_ADDRESS[0]}:{SSDP_ADDRESS[1]}",
            'MAN: "ssdp:discover"',
            "MX: 2",
            f"ST: {target}",
            "",
            "",
        ]
    ).encode("ascii")


def _usable_address(value: str) -> bool:
    try:
        address = IPv4Address(value)
    except ValueError:
        return False
    return not (
        address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_unspecified
    )


def _windows_ipv4_addresses() -> list[str]:
    """Ask Windows for active adapter addresses without relying on DNS."""
    if sys.platform != "win32":
        return []
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if not powershell:
        return []
    command = (
        "Get-NetIPConfiguration | "
        "Where-Object { $_.NetAdapter.Status -eq 'Up' } | "
        "ForEach-Object { $_.IPv4Address.IPAddress }"
    )
    try:
        result = subprocess.run(  # nosec B603 - fixed local PowerShell command
            [powershell, "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if _usable_address(line.strip())]


def local_ipv4_addresses() -> list[str]:
    """Return local IPv4 addresses that may lead to the speaker LAN."""
    addresses: list[str] = []

    def add(value: str) -> None:
        if _usable_address(value) and value not in addresses:
            addresses.append(value)

    for address in _windows_ipv4_addresses():
        add(address)

    for destination in (SSDP_ADDRESS, ("1.1.1.1", 53)):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.connect(destination)
                add(str(sock.getsockname()[0]))
        except OSError:
            continue

    for name in {socket.gethostname(), socket.getfqdn()}:
        try:
            results = socket.getaddrinfo(name, None, socket.AF_INET, socket.SOCK_DGRAM)
        except OSError:
            continue
        for result in results:
            add(result[4][0])

    return addresses


def _looks_like_wam(headers: dict[str, str]) -> bool:
    identifying_text = " ".join(
        headers.get(name, "") for name in ("st", "usn", "server", "location")
    ).lower()
    return "remotecontrolreceiver" in identifying_text or (
        "samsung" in identifying_text and "audio" in identifying_text
    )


def _open_discovery_socket(local_ip: str) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(local_ip))
        sock.bind((local_ip, 0))
        sock.setblocking(False)
        return sock
    except OSError:
        sock.close()
        raise


def discover_ssdp(
    timeout: float = 4.0,
    *,
    local_addresses: Iterable[str] | None = None,
) -> list[DiscoveredSpeaker]:
    """Search every suitable local interface instead of trusting one OS route."""
    addresses = list(dict.fromkeys(local_addresses or local_ipv4_addresses()))
    sockets: list[socket.socket] = []
    found: dict[str, DiscoveredSpeaker] = {}

    for local_ip in addresses:
        if not _usable_address(local_ip):
            continue
        try:
            sock = _open_discovery_socket(local_ip)
        except OSError as error:
            LOGGER.debug("Cannot use %s for SSDP: %s", local_ip, error)
            continue
        try:
            for target in SSDP_SEARCH_TARGETS:
                message = build_search_message(target)
                # Old WAM firmware occasionally ignores the first multicast packet.
                sock.sendto(message, SSDP_ADDRESS)
                sock.sendto(message, SSDP_ADDRESS)
        except OSError as error:
            LOGGER.debug("Cannot send SSDP through %s: %s", local_ip, error)
            sock.close()
            continue
        sockets.append(sock)

    if not sockets:
        LOGGER.debug("No usable IPv4 interface found for SSDP")
        return []

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
                headers = parse_ssdp_response(payload)
                if not _looks_like_wam(headers):
                    continue
                location = headers.get("location", "")
                ip = urlparse(location).hostname or sender[0]
                if ip and _usable_address(ip):
                    found[ip] = DiscoveredSpeaker(
                        ip=ip,
                        location=location,
                        usn=headers.get("usn"),
                        source="ssdp",
                    )
    finally:
        for sock in sockets:
            sock.close()

    return sorted(found.values(), key=lambda item: item.ip)


def candidate_subnets(local_addresses: Iterable[str]) -> list[IPv4Network]:
    """Return unique private /24 networks for the API-probe fallback."""
    networks: set[IPv4Network] = set()
    for value in local_addresses:
        try:
            address = IPv4Address(value)
        except ValueError:
            continue
        if not address.is_private or not _usable_address(value):
            continue
        networks.add(IPv4Network(f"{address}/24", strict=False))
    return sorted(networks, key=lambda network: int(network.network_address))


def scan_local_subnets(
    local_addresses: Iterable[str],
    *,
    port: int = DEFAULT_PORT,
    timeout: float = 0.25,
    max_workers: int = 64,
    probe_func: Callable[..., WamResponse] | None = None,
) -> list[DiscoveredSpeaker]:
    """Find WAM speakers by probing their local HTTP API on nearby /24 networks."""
    check = probe if probe_func is None else probe_func
    own_addresses = set(local_addresses)
    candidates = [
        str(host)
        for network in candidate_subnets(own_addresses)
        for host in network.hosts()
        if str(host) not in own_addresses
    ]
    if not candidates:
        return []

    found: dict[str, DiscoveredSpeaker] = {}

    def check_host(ip: str) -> DiscoveredSpeaker | None:
        try:
            check(ip, port=port, timeout=timeout)
        except (WamApiError, OSError, TimeoutError):
            return None
        return DiscoveredSpeaker(ip=ip, source="api-scan")

    with ThreadPoolExecutor(max_workers=min(max_workers, len(candidates))) as executor:
        futures = {executor.submit(check_host, ip): ip for ip in candidates}
        for future in as_completed(futures):
            speaker = future.result()
            if speaker is not None:
                found[speaker.ip] = speaker

    return sorted(found.values(), key=lambda item: item.ip)


def discover(
    timeout: float = 4.0,
    *,
    local_addresses: Iterable[str] | None = None,
    port: int = DEFAULT_PORT,
    scan: bool = True,
) -> list[DiscoveredSpeaker]:
    """Discover WAM speakers through SSDP, then optionally probe nearby LAN hosts."""
    addresses = list(dict.fromkeys(local_addresses or local_ipv4_addresses()))
    speakers = discover_ssdp(timeout, local_addresses=addresses)
    if speakers or not scan:
        return speakers
    LOGGER.info("SSDP found no WAM speaker; scanning local /24 network on port %s", port)
    return scan_local_subnets(addresses, port=port)


def local_ip_for(remote_ip: str) -> str:
    """Return the local address selected by the OS for reaching a speaker."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.connect((remote_ip, 9))
        return str(sock.getsockname()[0])
