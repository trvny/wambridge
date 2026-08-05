"""Eksperyment: czy SetSharePlaybackControl cokolwiek robi na fw WAM550WWB-3117.1,
i czy forma device_udn ma znaczenie.

Projekt: kontrola + dwa warianty, zmieniamy JEDNO pole naraz.

  KONTROLA  SetUrlPlayback na moj HTTP  -> dowodzi, ze glosnik SIEGA po moj serwer
                                           (odsiewa firewall od bledu protokolu)
  WARIANT A SetSharePlaybackControl, device_udn = surowy UUID   (hipoteza poprawki)
  WARIANT B SetSharePlaybackControl, device_udn = "uuid:"+UUID  (obecny attempt 1)

Kazdy wariant: rejestracja SetIpInfo -> komenda -> 12 s nasluchu.
Werdykt opiera sie na DWOCH niezaleznych sygnalach: zdarzeniach z 55001
ORAZ czy glosnik faktycznie pobral plik po HTTP.

Glosnosc: ustawiana RAZ na starcie, potem nietykana - nie bijemy sie z czlowiekiem.
"""

from __future__ import annotations

import http.server
import os
import socket
import socketserver
import sys
import threading
import time
import uuid as uuidlib
from pathlib import Path
from urllib.parse import quote

# Adresy i sciezka do pliku pochodza ze srodowiska - patrz README.md w tym katalogu.
SPEAKER = os.environ.get("WAM_SPEAKER", "192.168.1.50")
SPK_PORT = 55001
HOST_IP = os.environ.get("WAM_HOST", "192.168.1.10")
DMS_PORT = 49200  # ten sam, ktorego uzyla oficjalna apka
OBJECT_NAME = "WAMBRIDGEPROBE01.mp3"
MEDIA = Path(os.environ.get("WAM_MEDIA", "sample.mp3"))
START_VOLUME = 3

CLIENT_UUID = str(uuidlib.uuid4())

http_hits: list[str] = []
events: list[tuple[float, str, str, str]] = []
_stop = threading.Event()


# ---------------------------------------------------------------- HTTP (DMS)

class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _serve(self, body: bool) -> None:
        http_hits.append(f"{self.command} {self.path} from {self.client_address[0]}")
        print(f"    [HTTP] {self.command} {self.path} <- {self.client_address[0]}", flush=True)
        # Glosnik prosi o /DLNA/<objectid> przy sciezce share, o /<objectid> przy
        # SetUrlPlayback. Akceptujemy oba, porownujac sama nazwe pliku.
        if self.path.rsplit("/", 1)[-1] != OBJECT_NAME:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        data = MEDIA.read_bytes()
        start, end = 0, len(data) - 1
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            part = rng[6:].split("-")
            start = int(part[0]) if part[0] else 0
            if len(part) > 1 and part[1]:
                end = int(part[1])
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
        else:
            self.send_response(200)
        chunk = data[start : end + 1]
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Content-Length", str(len(chunk)))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        if body:
            self.wfile.write(chunk)

    def do_GET(self) -> None:
        self._serve(True)

    def do_HEAD(self) -> None:
        self._serve(False)

    def log_message(self, *args) -> None:
        pass


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


# ---------------------------------------------------------------- WAM 55001

def header(ip: str) -> str:
    return (
        " HTTP/1.1\r\n"
        f"Host: {ip}:{SPK_PORT}\r\n"
        f"mobileUUID: {CLIENT_UUID}\r\n"
        "mobileName: Wireless Audio\r\n"
        "mobileVersion: 1.0\r\n"
        "Connection: keep-alive\r\n\r\n"
    )


def bodies(raw: bytes) -> tuple[list[str], bytes]:
    out: list[str] = []
    while True:
        head_end = raw.find(b"\r\n\r\n")
        if head_end < 0:
            return out, raw
        head = raw[:head_end].decode("utf-8", "replace")
        length = 0
        for line in head.split("\r\n"):
            if line.lower().startswith("content-length:"):
                length = int(line.split(":", 1)[1].strip())
        total = head_end + 4 + length
        if len(raw) < total:
            return out, raw
        out.append(raw[head_end + 4 : total].decode("utf-8", "replace"))
        raw = raw[total:]


def field(body: str, name: str) -> str:
    open_tag, close_tag = f"<{name}>", f"</{name}>"
    i = body.find(open_tag)
    if i < 0:
        return ""
    j = body.find(close_tag, i)
    return body[i + len(open_tag) : j].replace("<![CDATA[", "").replace("]]>", "")


def listener(phase: dict) -> None:
    sock = socket.create_connection((SPEAKER, SPK_PORT), timeout=5)
    sock.sendall(("GET /UIC?cmd=" + quote("<name>GetFunc</name>") + header(SPEAKER)).encode())
    sock.settimeout(1.0)
    buf = b""
    while not _stop.is_set():
        try:
            got = sock.recv(8192)
            if not got:
                break
            buf += got
        except TimeoutError:
            continue
        except OSError:
            break
        found, buf = bodies(buf)
        for b in found:
            method = field(b, "method")
            who = field(b, "user_identifier")
            tag = "wlasny" if who == CLIENT_UUID else ("public" if who == "public" else "obcy")
            events.append((time.time(), phase["name"], method, tag))
            mark = {
                "DMSAddedEvent": "***",
                "StartPlaybackEvent": "***",
                "MediaBufferStartEvent": "***",
                "ErrorEvent": "!!!",
            }.get(method, "")
            print(f"    [55001] {method:<28} user={tag:<7}{mark}", flush=True)
    sock.close()


def send(cmd: str, wait: float = 2.0) -> str:
    sock = socket.create_connection((SPEAKER, SPK_PORT), timeout=5)
    sock.sendall(("GET /UIC?cmd=" + quote(cmd) + header(SPEAKER)).encode())
    sock.settimeout(wait)
    buf, deadline = b"", time.time() + wait
    while time.time() < deadline:
        try:
            buf += sock.recv(4096)
        except TimeoutError:
            break
    sock.close()
    found, _ = bodies(buf)
    return found[0] if found else ""


# ---------------------------------------------------------------- warianty

def register() -> None:
    send(
        f'<name>SetIpInfo</name><p type="str" name="uuid" val="{CLIENT_UUID}"/>'
        f'<p type="str" name="ip" val="{HOST_IP}:{DMS_PORT}"/>'
    )


def share(device_udn: str) -> str:
    return send(
        '<name>SetSharePlaybackControl</name>'
        '<p type="str" name="playbackcontrol" val="play"/>'
        '<p type="str" name="playertype" val="allshare"/>'
        '<p type="cdata" name="sourcename" val="empty"><![CDATA[WAMBridge]]></p>'
        '<p type="dec" name="playtime" val="0"/>'
        f'<p type="str" name="device_udn" val="{device_udn}"/>'
        f'<p type="str" name="objectid" val="{OBJECT_NAME}"/>'
    )


def url_playback() -> str:
    return send(
        f'<name>SetUrlPlayback</name>'
        f'<p type="cdata" name="url" val="empty">'
        f'<![CDATA[http://{HOST_IP}:{DMS_PORT}/{OBJECT_NAME}]]></p>'
        f'<p type="dec" name="buffersize" val="0"/>'
        f'<p type="dec" name="seektime" val="0"/>'
        f'<p type="dec" name="resume" val="1"/>'
    )


def phase_report(label: str, before_hits: int, before_events: int) -> dict:
    new_http = http_hits[before_hits:]
    new_ev = [e for e in events[before_events:]]
    interesting = [
        m
        for _, _, m, _ in new_ev
        if m
        in (
            "DMSAddedEvent",
            "MediaBufferStartEvent",
            "StartPlaybackEvent",
            "ErrorEvent",
            "MusicInfo",
        )
    ]
    print(
        f"\n  --> {label}: HTTP={len(new_http)}  "
        f"kluczowe zdarzenia={interesting or 'brak'}"
    )
    return {"label": label, "http": new_http, "events": interesting}


def main() -> None:
    if not MEDIA.exists():
        sys.exit(f"brak pliku: {MEDIA}")

    print(f"klient UUID : {CLIENT_UUID}")
    print(f"DMS         : http://{HOST_IP}:{DMS_PORT}/{OBJECT_NAME}")
    print(f"plik        : {MEDIA.name} ({MEDIA.stat().st_size:,} B)\n")

    srv = Server(("0.0.0.0", DMS_PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    phase = {"name": "init"}
    threading.Thread(target=listener, args=(phase,), daemon=True).start()
    time.sleep(1.5)

    send(f'<name>SetVolume</name><p type="dec" name="volume" val="{START_VOLUME}"/>')
    print(f"glosnosc ustawiona na {START_VOLUME}/30 (raz, potem nie ruszam)\n")

    results = []
    for label, action in (
        ("KONTROLA  SetUrlPlayback", url_playback),
        ("WARIANT A device_udn = surowy UUID", lambda: share(CLIENT_UUID)),
        ("WARIANT B device_udn = uuid:UUID", lambda: share(f"uuid:{CLIENT_UUID}")),
    ):
        print(f"\n=== {label} ===")
        phase["name"] = label
        h0, e0 = len(http_hits), len(events)
        register()
        time.sleep(0.5)
        resp = action()
        print(
            f"    odpowiedz: {field(resp, 'method') or '(brak)'} "
            f"err={field(resp, 'errCode') or field(resp, 'errcode') or '-'}"
        )
        time.sleep(12)
        results.append(phase_report(label, h0, e0))
        send(
            '<name>SetPlaybackControl</name>'
            '<p type="str" name="playbackcontrol" val="pause"/>'
        )
        time.sleep(1)

    _stop.set()
    srv.shutdown()

    print("\n" + "=" * 66)
    print("PODSUMOWANIE")
    print("=" * 66)
    for r in results:
        ok = "TAK" if r["http"] else "nie"
        print(
            f"  {r['label']:<40} glosnik pobral plik: {ok:<4} "
            f"zdarzenia: {r['events'] or '-'}"
        )
    print(
        "\nUWAGA: jesli KONTROLA nie pobrala pliku, to firewall blokuje "
        f"port {DMS_PORT} i wyniki wariantow sa nierozstrzygajace."
    )


if __name__ == "__main__":
    main()
