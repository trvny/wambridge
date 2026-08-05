"""Kluczowe pytanie dla wtyczki wyjsciowej foobara:
czy M5 odtworzy strumien HTTP BEZ znanej dlugosci?

Jesli tak, backpressure HTTP zalatwia pacing za darmo i cala warstwa
recznego rozkladania tempa (PR #4) jest zbedna.

Warianty naglowkow odpowiedzi (audio zawsze to samo, podawane ~1x realtime):
  A  WAV + Content-Length ogromny (klasyczny trik)
  B  WAV + Transfer-Encoding: chunked
  C  WAV + brak Content-Length, HTTP/1.0, zamkniecie polaczenia
  D  MP3 + brak Content-Length (styl radia internetowego)

Komenda: SetUrlPlayback - wlasciwa dla zrodel na zywo.
Glosnosci nie ruszamy.
"""

from __future__ import annotations

import os
import socketserver
import threading
import time
from pathlib import Path

import probe_share as ps

# Katalog na pliki testowe generowane przez ffmpeg; nadpisywalny przez WAM_SCRATCH.
SCRATCH = Path(os.environ.get("WAM_SCRATCH", Path(__file__).parent / "_scratch"))
SCRATCH.mkdir(parents=True, exist_ok=True)
WAV = SCRATCH / "t_wav16.wav"
MP3 = SCRATCH / "t_mp3.mp3"
FAKE_LEN = 0x7FFFFFF0
STREAM_SECONDS = 16

mode = {"variant": "A"}
served_bytes = {"n": 0}


class RawServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class RawHandler(socketserver.BaseRequestHandler):
    """Recznie sklecony HTTP - potrzebujemy pelnej kontroli nad naglowkami."""

    def handle(self) -> None:
        sock = self.request
        sock.settimeout(5)
        try:
            req = sock.recv(4096).decode("utf-8", "replace")
        except OSError:
            return
        if not req.startswith(("GET", "HEAD")):
            return
        line = req.split("\r\n")[0]
        print(f"    [HTTP] {line}  (wariant {mode['variant']})", flush=True)

        variant = mode["variant"]
        is_mp3 = variant == "D"
        src = MP3 if is_mp3 else WAV
        data = src.read_bytes()
        ctype = "audio/mpeg" if is_mp3 else "audio/wav"

        if variant == "A":
            head = (
                f"HTTP/1.1 200 OK\r\nContent-Type: {ctype}\r\n"
                f"Content-Length: {FAKE_LEN}\r\nAccept-Ranges: bytes\r\n"
                "transferMode.dlna.org: Streaming\r\n\r\n"
            )
        elif variant == "B":
            head = (
                f"HTTP/1.1 200 OK\r\nContent-Type: {ctype}\r\n"
                "Transfer-Encoding: chunked\r\n"
                "transferMode.dlna.org: Streaming\r\n\r\n"
            )
        else:  # C, D
            head = (
                f"HTTP/1.0 200 OK\r\nContent-Type: {ctype}\r\n"
                "Connection: close\r\n"
                "transferMode.dlna.org: Streaming\r\n\r\n"
            )

        try:
            sock.sendall(head.encode())
        except OSError:
            return

        # podajemy ~1x realtime: 44100*2*2 B/s dla WAV, ~40 kB/s dla MP3
        rate = 176400 if not is_mp3 else 40000
        step = rate // 8
        pos, deadline = 0, time.time() + STREAM_SECONDS
        while pos < len(data) and time.time() < deadline:
            piece = data[pos : pos + step]
            pos += step
            try:
                if variant == "B":
                    sock.sendall(f"{len(piece):X}\r\n".encode() + piece + b"\r\n")
                else:
                    sock.sendall(piece)
                served_bytes["n"] += len(piece)
            except OSError:
                print("    [HTTP] glosnik zerwal polaczenie", flush=True)
                return
            time.sleep(0.125)
        try:
            if variant == "B":
                sock.sendall(b"0\r\n\r\n")
            sock.close()
        except OSError:
            pass


def url_play(name: str) -> None:
    ps.send(
        f'<name>SetUrlPlayback</name>'
        f'<p type="cdata" name="url" val="empty">'
        f'<![CDATA[http://{ps.HOST_IP}:{ps.DMS_PORT}/{name}]]></p>'
        f'<p type="dec" name="buffersize" val="0"/>'
        f'<p type="dec" name="seektime" val="0"/>'
        f'<p type="dec" name="resume" val="1"/>'
    )


def main() -> None:
    srv = RawServer(("0.0.0.0", ps.DMS_PORT), RawHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    phase = {"name": "live"}
    threading.Thread(target=ps.listener, args=(phase,), daemon=True).start()
    time.sleep(1.5)
    vol = ps.field(ps.send("<name>GetVolume</name>"), "volume")
    print(f"glosnosc {vol}/30 - nie ruszam\n")

    cases = [
        ("A  WAV + ogromny Content-Length", "A", "live.wav"),
        ("B  WAV + chunked", "B", "live.wav"),
        ("C  WAV + brak dlugosci, close", "C", "live.wav"),
        ("D  MP3 + brak dlugosci (radio)", "D", "live.mp3"),
    ]
    results = []
    for label, variant, name in cases:
        print(f"\n=== {label} ===")
        mode["variant"] = variant
        served_bytes["n"] = 0
        e0 = len(ps.events)
        url_play(name)
        time.sleep(STREAM_SECONDS + 4)
        methods = [m for _, _, m, _ in ps.events[e0:]]
        kb = served_bytes["n"] // 1024
        if "ErrorEvent" in methods:
            verdict = "BLAD"
        elif kb > 400:
            verdict = "STRUMIEN LECI"
        elif kb > 0:
            verdict = f"urwal sie ({kb} kB)"
        else:
            verdict = "cisza"
        print(f"  --> {verdict}  oddane {kb} kB, zdarzenia={methods or '-'}")
        results.append((label, verdict, kb))
        ps.send(
            '<name>SetPlaybackControl</name>'
            '<p type="str" name="playbackcontrol" val="pause"/>'
        )
        time.sleep(1.5)

    ps._stop.set()
    srv.shutdown()
    print("\n" + "=" * 64)
    print("  CZY M5 PRZELKNIE STRUMIEN O NIEZNANEJ DLUGOSCI")
    print("=" * 64)
    for label, verdict, kb in results:
        print(f"  {label:<36} {verdict:<18} {kb} kB")


if __name__ == "__main__":
    main()
