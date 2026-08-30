"""Podsluch protokolu Samsung WAM z twardym limiterem glosnosci.

Loguje kazde zdarzenie z glosnika (takze wywolane przez apke Samsung Multiroom)
i natychmiast sciaga glosnosc, jesli przekroczy CEILING.
"""

from __future__ import annotations

import os
import re
import socket
import sys
import time
import uuid as uuidlib
from urllib.parse import quote

IP = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("WAM_SPEAKER", "192.168.1.50")
SECONDS = int(sys.argv[2]) if len(sys.argv) > 2 else 240
CEILING = 5          # maks 5/30 -- pozno jest
CLAMP_TO = 3
PORT = 55001

MOBILE_UUID = str(uuidlib.uuid4())  # glosnik nie sprawdza wartosci, tylko obecnosc naglowka

STATUS_LINE = re.compile(rb"HTTP/1\.1 (\d{3})")
CONTENT_LENGTH = re.compile(rb"^Content-Length:\s*(\d+)", re.MULTILINE | re.IGNORECASE)


def header() -> str:
    return (
        " HTTP/1.1\r\n"
        f"Host: {IP}:{PORT}\r\n"
        f"mobileUUID: {MOBILE_UUID}\r\n"
        "mobileName: Wireless Audio\r\n"
        "mobileVersion: 1.0\r\n"
        "\r\n"
    )


def bodies(buffer: bytes) -> tuple[list[str], bytes]:
    out: list[str] = []
    while True:
        match = STATUS_LINE.search(buffer)
        if not match:
            return out, buffer
        buffer = buffer[match.start():]
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


def send(command: str) -> None:
    """Jednorazowa komenda z osobnego socketu, zeby nie mieszac w strumieniu."""
    try:
        sock = socket.create_connection((IP, PORT), timeout=4)
        sock.sendall((f"GET /UIC?cmd={quote(command)}" + header()).encode())
        time.sleep(0.2)
        sock.close()
    except OSError as error:
        print(f"[!] nie udalo sie wyslac komendy: {error}", flush=True)


def main() -> int:
    send(f'<name>SetVolume</name><p type="dec" name="volume" val="{CLAMP_TO}"/>')
    print(f"[*] glosnosc ustawiona na {CLAMP_TO}/30, limiter na {CEILING}/30", flush=True)

    sock = socket.create_connection((IP, PORT), timeout=10)
    sock.sendall((f"GET /UIC?cmd={quote('<name>GetFunc</name>')}" + header()).encode())
    sock.settimeout(1.0)
    print(f"[*] nasluch {SECONDS}s -- mozesz klikac play w apce\n", flush=True)

    buffer = b""
    deadline = time.monotonic() + SECONDS
    while time.monotonic() < deadline:
        try:
            data = sock.recv(8192)
        except TimeoutError:
            continue
        except OSError:
            break
        if not data:
            print("[!] glosnik zamknal polaczenie", flush=True)
            break
        buffer += data
        found, buffer = bodies(buffer)
        for body in found:
            method = re.search(r"<method>(.*?)</method>", body)
            label = method.group(1) if method else "?"
            stamp = time.strftime("%H:%M:%S")
            print(f"{stamp}  {label}", flush=True)
            print(f"    {body.strip()}\n", flush=True)

            volume = re.search(r"<volume>(\d+)</volume>", body)
            if volume and int(volume.group(1)) > CEILING:
                print(
                    f"    !! {volume.group(1)}/30 powyzej limitu -> sciagam do {CLAMP_TO}",
                    flush=True,
                )
                send(
                    f'<name>SetVolume</name><p type="dec" name="volume" '
                    f'val="{CLAMP_TO}"/>'
                )

    sock.close()
    print("[*] koniec nasluchu", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
