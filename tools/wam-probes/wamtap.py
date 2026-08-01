"""wamtap - pasywny podsluch zdarzen Samsung WAM + diagnostyka portow.

Nie wymaga zaleznosci. Python 3.10+.

  py wamtap.py sniff 192.168.1.50
      Trzyma otwarte polaczenie TCP na porcie 55001 i wypisuje KAZDE zdarzenie,
      ktore glosnik rozsyla - takze te wywolane przez oficjalna apke Samsung
      Multiroom na telefonie. To jest sposob na zdjecie protokolu "share
      playback" bez dekompilacji APK i bez MITM.

  py wamtap.py ports 192.168.1.50
      Sprawdza, ktore porty TCP glosnik ma faktycznie otwarte (m.in. 9197 -
      samsungowy DMR/AVTransport) i probuje pobrac z nich opis UPnP.

  py wamtap.py descr http://192.168.1.50:9197/dmr
      Pobiera i wypisuje pelna liste serviceList z opisu UPnP.

  py wamtap.py cmd 192.168.1.50 UIC GetFeature
      Wysyla jedna komende z naglowkami mobileUUID/mobileName jak oficjalna apka.
"""

from __future__ import annotations

import re
import socket
import sys
import time
import uuid
from urllib.parse import quote
from urllib.request import Request, urlopen

WAM_PORT = 55001
STATUS_LINE = re.compile(rb"HTTP/1\.1 (\d{3})")
CONTENT_LENGTH = re.compile(rb"^Content-Length:\s*(\d+)", re.MULTILINE | re.IGNORECASE)

# Porty warte sprawdzenia: 9197 to samsungowy DMR (AVTransport), 7676/8080 to
# typowe porty opisow UPnP, 3921 to DMS uzywany przez wambridge.
PROBE_PORTS = (80, 1900, 3921, 7676, 8001, 8080, 8090, 9090, 9197, 9998, 55001, 56001)

DESCR_PATHS = (
    "/dmr",
    "/dmr/",
    "/dmr/SamsungMRDesc.xml",
    "/description.xml",
    "/upnp/description.xml",
    "/DeviceDescription.xml",
    "/rootDesc.xml",
    "/",
)

USER = str(uuid.uuid4())


def _header(ip: str) -> str:
    """Naglowki, ktore wysyla oficjalna apka. wambridge ich nie wysyla."""
    return (
        " HTTP/1.1\r\n"
        f"Host: {ip}:{WAM_PORT}\r\n"
        f"mobileUUID: {USER}\r\n"
        "mobileName: Wireless Audio\r\n"
        "mobileVersion: 1.0\r\n"
        "\r\n"
    )


def _bodies(buffer: bytes) -> tuple[list[str], bytes]:
    """Wytnij kompletne ciala odpowiedzi. Glosnik nie zamyka polaczenia
    i nie wstawia separatorow, wiec jedziemy po Content-Length."""
    out: list[str] = []
    while True:
        match = STATUS_LINE.search(buffer)
        if not match:
            return out, buffer
        buffer = buffer[match.start() :]
        split = buffer.split(b"\r\n\r\n", 1)
        if len(split) != 2:
            return out, buffer
        head, rest = split
        length_match = CONTENT_LENGTH.search(head)
        if not length_match:
            buffer = rest
            continue
        length = int(length_match.group(1))
        if len(rest) < length:
            return out, buffer
        out.append(rest[:length].decode("utf-8", "replace"))
        buffer = rest[length:]


def _pretty(body: str) -> str:
    body = body.strip()
    method = re.search(r"<method>(.*?)</method>", body)
    label = method.group(1) if method else "?"
    return f"{label}\n    {body}"


def sniff(ip: str) -> int:
    print(f"[*] laczenie {ip}:{WAM_PORT}  (Ctrl+C konczy)")
    print("[*] teraz odpal oficjalna apke i zrob to, co chcesz podejrzec\n")
    sock = socket.create_connection((ip, WAM_PORT), timeout=10)
    sock.settimeout(None)
    # Jedno zapytanie zeby firmware uznal nas za klienta i zaczal pchac eventy.
    sock.sendall((f"GET /UIC?cmd={quote('<name>GetFunc</name>')}" + _header(ip)).encode())
    buffer = b""
    try:
        while True:
            data = sock.recv(4096)
            if not data:
                print("[!] glosnik zamknal polaczenie")
                return 1
            buffer += data
            bodies, buffer = _bodies(buffer)
            for body in bodies:
                print(f"{time.strftime('%H:%M:%S')}  {_pretty(body)}\n")
    except KeyboardInterrupt:
        return 0
    finally:
        sock.close()


def ports(ip: str) -> int:
    open_ports = []
    for port in PROBE_PORTS:
        try:
            with socket.create_connection((ip, port), timeout=1.5):
                open_ports.append(port)
                print(f"[+] {port}/tcp otwarty")
        except OSError:
            print(f"[-] {port}/tcp zamkniety")
    print()
    for port in open_ports:
        if port in (1900, 55001):
            continue
        for path in DESCR_PATHS:
            url = f"http://{ip}:{port}{path}"
            try:
                with urlopen(  # noqa: S310
                    Request(url, headers={"User-Agent": "wamtap/1.0 UPnP/1.0"}),
                    timeout=3,
                ) as response:
                    payload = response.read(20000).decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                continue
            if "<serviceType>" not in payload and "<deviceType>" not in payload:
                continue
            print(f"[+] opis UPnP: {url}")
            for service in re.findall(r"<serviceType>(.*?)</serviceType>", payload):
                print(f"      {service}")
            print()
            break
    return 0


def descr(url: str) -> int:
    with urlopen(  # noqa: S310
        Request(url, headers={"User-Agent": "wamtap/1.0 UPnP/1.0"}), timeout=5
    ) as response:
        payload = response.read().decode("utf-8", "replace")
    print(payload)
    return 0


def cmd(ip: str, api: str, method: str) -> int:
    sock = socket.create_connection((ip, WAM_PORT), timeout=10)
    sock.sendall(
        (f"GET /{api}?cmd={quote(f'<name>{method}</name>')}" + _header(ip)).encode()
    )
    buffer = b""
    sock.settimeout(6)
    try:
        while True:
            data = sock.recv(4096)
            if not data:
                break
            buffer += data
            bodies, buffer = _bodies(buffer)
            for body in bodies:
                print(_pretty(body))
            if bodies:
                break
    except TimeoutError:
        print("[!] brak odpowiedzi")
    finally:
        sock.close()
    return 0


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    action, *rest = argv[1:]
    if action == "sniff" and rest:
        return sniff(rest[0])
    if action == "ports" and rest:
        return ports(rest[0])
    if action == "descr" and rest:
        return descr(rest[0])
    if action == "cmd" and len(rest) >= 3:
        return cmd(rest[0], rest[1], rest[2])
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
