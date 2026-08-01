"""Czy backpressure TCP sam narzuca tempo?

To rozstrzyga sens PR #4. Poprzedni test podawal dane w tempie 1x realtime,
wiec glosnik nie musial hamowac - to nic nie dowodzilo.

Tutaj pchamy TAK SZYBKO, JAK SIE DA, i mierzymy osiagniety przeplyw:

  ~176 kB/s (realtime WAV 44.1/16)  -> glosnik hamuje przez okno TCP,
                                        warstwa recznego pacingu ZBEDNA
  duzo wiecej                       -> glosnik buforuje bez ograniczen,
                                        pacing POTRZEBNY

Glosnosci nie ruszamy.
"""

from __future__ import annotations

import socketserver
import threading
import time
import os
from pathlib import Path

import probe_share as ps

# Katalog na pliki testowe generowane przez ffmpeg; nadpisywalny przez WAM_SCRATCH.
SCRATCH = Path(os.environ.get("WAM_SCRATCH", Path(__file__).parent / "_scratch"))
SCRATCH.mkdir(parents=True, exist_ok=True)
WAV = SCRATCH / "t_wav16.wav"
REALTIME_BPS = 44100 * 2 * 2  # 176400 B/s
TEST_SECONDS = 20

stats = {"sent": 0, "start": None, "samples": []}


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class Handler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        sock = self.request
        sock.settimeout(10)
        try:
            req = sock.recv(4096).decode("utf-8", "replace")
        except OSError:
            return
        if not req.startswith("GET"):
            return
        print(f"    [HTTP] {req.split(chr(13))[0]}", flush=True)

        sock.sendall(
            b"HTTP/1.0 200 OK\r\nContent-Type: audio/wav\r\n"
            b"Connection: close\r\ntransferMode.dlna.org: Streaming\r\n\r\n"
        )
        data = WAV.read_bytes()
        # petla po pliku, zeby nie zabraknac materialu
        stats["start"] = time.time()
        last_report = stats["start"]
        pos = 0
        while time.time() - stats["start"] < TEST_SECONDS:
            piece = data[pos:pos + 32768]
            pos += 32768
            if pos >= len(data):
                pos = 44  # od nowa, pomijajac naglowek WAV
            try:
                sock.sendall(piece)  # BEZ sleep - pchamy ile wlezie
            except OSError:
                print("    [HTTP] polaczenie zerwane", flush=True)
                break
            stats["sent"] += len(piece)
            now = time.time()
            if now - last_report >= 4:
                elapsed = now - stats["start"]
                bps = stats["sent"] / elapsed
                stats["samples"].append(bps)
                print(f"    +{elapsed:4.1f}s  {stats['sent']/1024:8.0f} kB  "
                      f"{bps/1024:6.1f} kB/s  ({bps/REALTIME_BPS:.2f}x realtime)",
                      flush=True)
                last_report = now
        try:
            sock.close()
        except OSError:
            pass


def main() -> None:
    srv = Server(("0.0.0.0", ps.DMS_PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    threading.Thread(target=ps.listener, args=({"name": "bp"},), daemon=True).start()
    time.sleep(1.5)
    vol = ps.field(ps.send("<name>GetVolume</name>"), "volume")
    print(f"glosnosc {vol}/30 - nie ruszam")
    print(f"realtime dla WAV 44.1/16 = {REALTIME_BPS/1024:.0f} kB/s\n")

    ps.send('<name>SetUrlPlayback</name>'
            '<p type="cdata" name="url" val="empty">'
            f'<![CDATA[http://{ps.HOST_IP}:{ps.DMS_PORT}/bp.wav]]></p>'
            '<p type="dec" name="buffersize" val="0"/>'
            '<p type="dec" name="seektime" val="0"/>'
            '<p type="dec" name="resume" val="1"/>')

    time.sleep(TEST_SECONDS + 6)
    ps._stop.set()
    srv.shutdown()

    elapsed = time.time() - (stats["start"] or time.time())
    avg = stats["sent"] / max(elapsed, 0.001)
    print("\n" + "=" * 60)
    print(f"  oddane lacznie : {stats['sent']/1024/1024:.1f} MB")
    print(f"  sredni przeplyw: {avg/1024:.1f} kB/s  = {avg/REALTIME_BPS:.2f}x realtime")
    print("=" * 60)
    if avg < REALTIME_BPS * 2:
        print("  WNIOSEK: glosnik hamuje przez TCP. Reczny pacing ZBEDNY.")
    else:
        print("  WNIOSEK: glosnik buforuje bez ograniczen. Pacing POTRZEBNY.")
    ps.send('<name>SetPlaybackControl</name>'
            '<p type="str" name="playbackcontrol" val="pause"/>')


if __name__ == "__main__":
    main()
