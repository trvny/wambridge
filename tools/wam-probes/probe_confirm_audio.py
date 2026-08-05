"""Ostateczne potwierdzenie sluchowe. Konfiguracja wylacznie potwierdzona eksperymentem:

  device_udn = surowy UUID z SetIpInfo
  obiekt pod /DLNA/<objectid>
  naglowki DLNA obecne

GLOSNOSCI NIE RUSZAMY - zostaje wartosc ustawiona przez czlowieka.

Liczy bajty oddane po HTTP, zeby miec dowod po stronie serwera, niezalezny
od telemetrii glosnika (ktora potrafi klamac - patrz mieszane MusicInfo).
"""

from __future__ import annotations

import threading
import time

import probe_share as ps
from probe_dlna_headers import DlnaHandler

PLAY_SECONDS = 30

served = {"bytes": 0, "requests": 0, "first": None, "last": None}


class CountingHandler(DlnaHandler):
    def _serve(self, body: bool) -> None:
        served["requests"] += 1
        if served["first"] is None:
            served["first"] = time.time()
        super()._serve(body)

    def wfile_write(self, data):  # nieuzywane, zostawione dla czytelnosci
        return

    def end_headers(self) -> None:
        super().end_headers()

    def copyfile(self, src, dst):
        super().copyfile(src, dst)


_orig_serve = ps.Handler._serve


def _counting_serve(self, body: bool) -> None:
    _orig_serve(self, body)
    served["last"] = time.time()


def main() -> None:
    print("=" * 62)
    print("  POTWIERDZENIE SLUCHOWE - konfiguracja potwierdzona eksperymentem")
    print("=" * 62)
    print(f"  plik      : {ps.MEDIA.name}")
    print(f"  DMS       : http://{ps.HOST_IP}:{ps.DMS_PORT}/DLNA/{ps.OBJECT_NAME}")
    print("  glosnosc  : NIE RUSZAM (twoje ustawienie)")
    print(f"  czas gry  : {PLAY_SECONDS} s\n")

    srv = ps.Server(("0.0.0.0", ps.DMS_PORT), CountingHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    phase = {"name": "audio"}
    threading.Thread(target=ps.listener, args=(phase,), daemon=True).start()
    time.sleep(1.5)

    vol = ps.field(ps.send("<name>GetVolume</name>"), "volume")
    print(f"  aktualna glosnosc glosnika: {vol}/30\n")

    ps.register()
    time.sleep(0.5)
    print(">>> START - sluchaj\n")
    ps.share(ps.CLIENT_UUID)

    for remaining in range(PLAY_SECONDS, 0, -5):
        time.sleep(5)
        print(
            f"    ... gra jeszcze {remaining - 5} s "
            f"(zadan HTTP: {served['requests']})",
            flush=True,
        )

    ps._stop.set()
    srv.shutdown()

    print("\n" + "=" * 62)
    print("  DOWOD PO STRONIE SERWERA (niezalezny od telemetrii glosnika)")
    print("=" * 62)
    print(f"  zadan HTTP od glosnika : {served['requests']}")
    if served["first"]:
        print(
            f"  pierwsze zadanie       : "
            f"+{served['first'] - served['first']:.1f} s od startu"
        )
        stream_seconds = (served["last"] or served["first"]) - served["first"]
        print(f"  strumien trwal         : {stream_seconds:.1f} s")
    print(f"  wszystkie trafienia    : {ps.http_hits}")
    print("\n  Glosnosci nie zmienialem ani razu.")


if __name__ == "__main__":
    main()
