"""Czy sciezka PCM trzyma tempo, i jak gleboki zapas polyka, zanim zacznie hamowac?

Filtr wpinany w rure miedzy ffmpeg a pcm_cli. Przepuszcza bajty bez zmian, a co
INTERVAL sekund wypisuje na stderr przeplyw PRZYROSTOWY. Skumulowany srednia
maskuje poczatkowy zryw buforowania i podpowiada bledny wniosek - stad przyrost.

    ffmpeg -i utwor.flac -f s16le -ar 44100 -ac 2 - \
      | py probe_clock_drift.py \
      | py -m wambridge.pcm_cli --speaker $WAM_SPEAKER \
            --sample-rate 44100 --channels 2 --sample-format s16le

Kolumna "dryf" to sekundy audio oddane ponad czas rzeczywisty, czyli ile audio
wisi w locie: w rurze, w ffmpegu, w gniezdzie i w buforze glosnika.

Zmierzone na M5 (2026-08-01, 100 s ciaglej gry, 44.1/16 stereo):

    czas    x realtime   dryf
      5.7s     6.41x    +30.8s   <- zryw, downstream lyka zapas
     16.0s     0.57x    +26.4s   <- korekta
     26.9s     0.91x    +23.7s
     52.8s     1.02x    +23.0s
     99.7s     1.07x    +23.7s

WNIOSEK: po ~25 s petla sie zatrzaskuje - przeplyw oscyluje wokol 1.00x, a zapas
stoi na +23 s i nie rosnie. Backpressure sam narzuca tempo, reczny pacing zbedny.

Te +23 s to WLASCIWOSC GLOSNIKA, nie blad. Warstwa foobarowa musi je poprawnie
raportowac (get_latency), a nie probowac ich zlikwidowac. Dryf rosnacy bez konca
oznaczalby brak backpressure; dryf malejacy do zera - zaglodzenie glosnika.
"""

from __future__ import annotations

import os
import sys
import time

# 44.1 kHz, 16 bit, stereo = 176400 B/s. Nadpisz, jesli mierzysz inny format.
SAMPLE_RATE = int(os.environ.get("WAM_SAMPLE_RATE", "44100"))
CHANNELS = int(os.environ.get("WAM_CHANNELS", "2"))
BYTES_PER_SAMPLE = 2
REALTIME = SAMPLE_RATE * CHANNELS * BYTES_PER_SAMPLE

INTERVAL = float(os.environ.get("WAM_INTERVAL", "5"))
CHUNK = 8192


def main() -> int:
    src = sys.stdin.buffer
    dst = sys.stdout.buffer

    start = time.time()
    total = 0
    mark_time = start
    mark_bytes = 0

    print("czas   przyrost   x realtime   audio oddane   dryf", file=sys.stderr, flush=True)

    while True:
        data = src.read(CHUNK)
        if not data:
            break
        dst.write(data)
        total += len(data)

        now = time.time()
        if now - mark_time < INTERVAL:
            continue

        rate = (total - mark_bytes) / (now - mark_time)
        elapsed = now - start
        audio = total / REALTIME
        print(
            "%5.1fs %8.1f kB/s %8.2fx %12.1fs %+7.1fs"
            % (elapsed, rate / 1024, rate / REALTIME, audio, audio - elapsed),
            file=sys.stderr,
            flush=True,
        )
        mark_time, mark_bytes = now, total

    dst.flush()
    print(
        "KONIEC: %.1f MB w %.1fs" % (total / 1048576, time.time() - start),
        file=sys.stderr,
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
