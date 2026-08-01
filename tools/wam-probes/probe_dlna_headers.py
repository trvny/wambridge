"""Kandydat na trzeci blad: brak naglowkow DLNA w odpowiedzi HTTP.

Renderery Samsunga potrafia pobrac plik i milczec, jesli serwer nie deklaruje
transferMode.dlna.org / contentFeatures.dlna.org.

Zmieniamy JEDNO: naglowki odpowiedzi. device_udn surowy, sciezka /DLNA/,
playertype bez znaczenia (zmierzone) - zostaje allshare.

Dodatkowo zrzuca pelne MusicInfo, zeby zobaczyc, co glosnik sadzi, ze dostal.
"""

from __future__ import annotations

import threading
import time

import probe_share as ps

DLNA_PN = "MP3"
# DLNA.ORG_OP=01 -> obslugiwany range; FLAGS: streaming, bez konwersji
CONTENT_FEATURES = (
    f"DLNA.ORG_PN={DLNA_PN};DLNA.ORG_OP=01;DLNA.ORG_CI=0;"
    "DLNA.ORG_FLAGS=01700000000000000000000000000000"
)

seen_headers: list[str] = []


class DlnaHandler(ps.Handler):
    def end_headers(self) -> None:
        self.send_header("transferMode.dlna.org", "Streaming")
        self.send_header("contentFeatures.dlna.org", CONTENT_FEATURES)
        super().end_headers()

    def _serve(self, body: bool) -> None:
        interesting = {k: v for k, v in self.headers.items()
                       if k.lower().startswith(("getcontentfeatures", "transfermode",
                                                "range", "user-agent"))}
        if interesting:
            seen_headers.append(f"{self.command} {self.path} :: {interesting}")
            print(f"    [HTTP-REQ] {interesting}", flush=True)
        super()._serve(body)


def main() -> None:
    print(f"klient UUID : {ps.CLIENT_UUID}")
    print(f"contentFeatures: {CONTENT_FEATURES}\n")

    srv = ps.Server(("0.0.0.0", ps.DMS_PORT), DlnaHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    phase = {"name": "dlna"}
    threading.Thread(target=ps.listener, args=(phase,), daemon=True).start()
    time.sleep(1.5)
    ps.send('<name>SetVolume</name><p type="dec" name="volume" val="3"/>')
    print("glosnosc 3/30 (raz)\n")

    print("=== share z naglowkami DLNA ===")
    h0, e0 = len(ps.http_hits), len(ps.events)
    ps.register()
    time.sleep(0.5)
    resp = ps.share(ps.CLIENT_UUID)
    print(f"    odpowiedz: {ps.field(resp, 'method') or '(brak)'}")
    if resp:
        print(f"\n    PELNA ODPOWIEDZ:\n    {resp[:900]}\n")
    time.sleep(20)
    ps.phase_report("share + naglowki DLNA", h0, e0)

    print("\n=== stan odtwarzania wg glosnika ===")
    for cmd in ('<name>GetPlayStatus</name>', '<name>GetMusicInfo</name>'):
        r = ps.send(cmd, wait=3.0)
        print(f"    {r[:600]}\n")

    ps._stop.set()
    srv.shutdown()
    ps.send('<name>SetVolume</name><p type="dec" name="volume" val="4"/>')
    print("glosnosc przywrocona do 4/30")
    if seen_headers:
        print("\nzadania od glosnika:")
        for h in seen_headers:
            print("  " + h)


if __name__ == "__main__":
    main()
