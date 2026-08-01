"""Co M5 faktycznie odtworzy przez sciezke share? Odpowiedz empiryczna.

Dla kazdego formatu: rejestracja -> share -> czekaj na StartPlaybackEvent.
Werdykt opiera sie na zdarzeniach z 55001, NIE na MusicInfo/PlayStatus
(zmierzone: potrafia klamac).

Glosnosci nie ruszamy.
"""

from __future__ import annotations

import http.server
import socketserver
import threading
import time
import os
from pathlib import Path

import probe_share as ps

# Katalog na pliki testowe generowane przez ffmpeg; nadpisywalny przez WAM_SCRATCH.
SCRATCH = Path(os.environ.get("WAM_SCRATCH", Path(__file__).parent / "_scratch"))
SCRATCH.mkdir(parents=True, exist_ok=True)

# (etykieta, plik, nazwa obiektu, mime, DLNA.ORG_PN)
CASES = [
    ("MP3 44.1/16 (kontrola)", "t_mp3.mp3", "T_MP3.mp3", "audio/mpeg", "MP3"),
    ("WAV 44.1/16 PCM", "t_wav16.wav", "T_WAV.wav", "audio/wav", "LPCM"),
    ("FLAC 44.1/16", "t_flac16.flac", "T_FLAC16.flac", "audio/flac", "FLAC"),
    ("FLAC 96/24 hi-res", "t_flac24.flac", "T_FLAC24.flac", "audio/flac", "FLAC"),
    ("AAC 44.1 (m4a)", "t_aac.m4a", "T_AAC.m4a", "audio/mp4", "AAC_ISO_320"),
    ("Opus 48k", "t_opus.opus", "T_OPUS.opus", "audio/ogg", ""),
]

state = {"file": SCRATCH / "t_mp3.mp3", "mime": "audio/mpeg", "pn": "MP3", "hits": 0}


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _serve(self, body: bool) -> None:
        state["hits"] += 1
        rng = self.headers.get("Range", "-")
        print(f"    [HTTP] {self.command} {self.path}  Range={rng}", flush=True)
        data = state["file"].read_bytes()
        start, end = 0, len(data) - 1
        if rng and rng.startswith("bytes="):
            part = rng[6:].split("-")
            start = int(part[0]) if part[0] else 0
            if len(part) > 1 and part[1]:
                end = int(part[1])
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
        else:
            self.send_response(200)
        chunk = data[start:end + 1]
        self.send_header("Content-Type", state["mime"])
        self.send_header("Content-Length", str(len(chunk)))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("transferMode.dlna.org", "Streaming")
        if state["pn"]:
            self.send_header(
                "contentFeatures.dlna.org",
                f"DLNA.ORG_PN={state['pn']};DLNA.ORG_OP=01;DLNA.ORG_CI=0;"
                "DLNA.ORG_FLAGS=01700000000000000000000000000000",
            )
        self.end_headers()
        if body:
            try:
                self.wfile.write(chunk)
            except OSError:
                pass  # glosnik zerwal - normalne przy odrzuceniu formatu

    def do_GET(self) -> None:
        self._serve(True)

    def do_HEAD(self) -> None:
        self._serve(False)

    def log_message(self, *a) -> None:
        pass


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def share_object(object_id: str) -> None:
    ps.send(
        '<name>SetSharePlaybackControl</name>'
        '<p type="str" name="playbackcontrol" val="play"/>'
        '<p type="str" name="playertype" val="allshare"/>'
        '<p type="cdata" name="sourcename" val="empty"><![CDATA[WAMBridge]]></p>'
        '<p type="dec" name="playtime" val="0"/>'
        f'<p type="str" name="device_udn" val="{ps.CLIENT_UUID}"/>'
        f'<p type="str" name="objectid" val="{object_id}"/>'
    )


def main() -> None:
    srv = Server(("0.0.0.0", ps.DMS_PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    phase = {"name": "fmt"}
    threading.Thread(target=ps.listener, args=(phase,), daemon=True).start()
    time.sleep(1.5)

    vol = ps.field(ps.send("<name>GetVolume</name>"), "volume")
    print(f"glosnosc {vol}/30 - nie ruszam\n")

    results = []
    for label, fname, objid, mime, pn in CASES:
        path = SCRATCH / fname
        if not path.exists():
            results.append((label, "BRAK PLIKU", 0))
            continue
        print(f"\n=== {label} ===")
        state.update(file=path, mime=mime, pn=pn, hits=0)
        e0 = len(ps.events)
        ps.register()
        time.sleep(0.4)
        share_object(objid)
        time.sleep(14)

        methods = [m for _, _, m, _ in ps.events[e0:]]
        if "StartPlaybackEvent" in methods:
            verdict = "GRA"
        elif "ErrorEvent" in methods:
            verdict = "BLAD"
        elif state["hits"]:
            verdict = "pobral, nie zagral"
        else:
            verdict = "cisza"
        print(f"  --> {verdict}  (HTTP={state['hits']}, zdarzenia={methods or '-'})")
        results.append((label, verdict, state["hits"]))

        ps.send('<name>SetPlaybackControl</name>'
                '<p type="str" name="playbackcontrol" val="pause"/>')
        time.sleep(1.5)

    ps._stop.set()
    srv.shutdown()

    print("\n" + "=" * 62)
    print("  CO M5 ODTWARZA PRZEZ SCIEZKE SHARE")
    print("=" * 62)
    for label, verdict, hits in results:
        print(f"  {label:<26} {verdict:<20} HTTP={hits}")


if __name__ == "__main__":
    main()
