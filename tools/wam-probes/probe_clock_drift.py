"""Czy sciezka PCM trzyma tempo, i ile audio wisi w locie, gdy juz gra?

Filtr wpinany w rure miedzy ffmpeg a pcm_cli. Przepuszcza bajty bez zmian, a co
INTERVAL sekund wypisuje na stderr przeplyw PRZYROSTOWY. Skumulowana srednia
maskuje poczatkowy zryw buforowania i podpowiada bledny wniosek - stad przyrost.

    WAM_SPEAKER=192.168.1.50 PYTHONPATH=src \
    ffmpeg -i utwor.flac -f s16le -ar 44100 -ac 2 - \
      | py tools/wam-probes/probe_clock_drift.py \
      | py -m wambridge.pcm_cli --speaker $WAM_SPEAKER \
            --sample-rate 44100 --channels 2 --sample-format s16le

KOTWICA. Sam czas od startu procesu NIE nadaje sie na punkt odniesienia: zanim
poleci dzwiek, mija discovery, przekazanie URL-a i wlasne buforowanie pcm_cli,
a te sekundy wpadlyby do wyniku jako rzekomy zapas glosnika. Dlatego sonda
podlacza sie w tle na 55001 i czeka na StartPlaybackEvent - jedyne zdarzenie,
ktore potwierdza dzwiek (AGENTS.md L18-19; MusicInfo i PlayStatus potrafia
klamac). Kolumna "w locie" liczy sie dopiero od niego.

Bez WAM_SPEAKER sonda dziala dalej, ale mierzy wylacznie przeplyw: kolumna
"w locie" pokazuje "-", bo bez potwierdzenia nie ma od czego liczyc. Sesja
pobrana i przemilczana wygladalaby wtedy identycznie jak grajaca.

Zmierzone na M5 (2026-08-01, 100 s ciaglej gry, 44.1/16 stereo) BEZ kotwicy,
czyli z czasem liczonym od startu procesu:

    czas    x realtime   dryf
      5.7s     6.41x    +30.8s
     26.9s     0.91x    +23.7s
     52.8s     1.02x    +23.0s
     99.7s     1.07x    +23.7s

WNIOSEK, ktory sie broni: po ~25 s petla sie zatrzaskuje - przeplyw oscyluje
wokol 1.00x, a zapas stoi i nie rosnie. Backpressure sam narzuca tempo, reczny
pacing zbedny. Dryf rosnacy bez konca oznaczalby brak backpressure, malejacy do
zera - zaglodzenie glosnika.

CZEGO TAMTEN POMIAR NIE DOWODZI: liczba ~23 s jest GORNYM OGRANICZENIEM, bo
zawiera rozruch. Prawdziwy zapas glosnika trzeba odczytac z kolumny "w locie",
liczonej od StartPlaybackEvent. Nie uzywac +23 s jako celu dla get_latency().
"""

from __future__ import annotations

import os
import sys
import threading
import time

SAMPLE_RATE = int(os.environ.get("WAM_SAMPLE_RATE", "44100"))
CHANNELS = int(os.environ.get("WAM_CHANNELS", "2"))
BYTES_PER_SAMPLE = 2
REALTIME = SAMPLE_RATE * CHANNELS * BYTES_PER_SAMPLE

INTERVAL = float(os.environ.get("WAM_INTERVAL", "5"))
CHUNK = 8192
SPEAKER = os.environ.get("WAM_SPEAKER", "")

# Ustawiany przez watek nasluchu w chwili StartPlaybackEvent (zegar monotoniczny).
_playback_start: float | None = None
_stop = threading.Event()


def _watch_for_playback() -> None:
    """Ustaw kotwice, gdy glosnik potwierdzi odtwarzanie."""
    global _playback_start
    try:
        import uuid

        from wambridge.wam_events import listen_events
    except ImportError:
        print(
            "[kotwica] brak modulu wambridge (PYTHONPATH=src?) - mierze sam przeplyw",
            file=sys.stderr,
            flush=True,
        )
        return

    try:
        for event in listen_events(SPEAKER, str(uuid.uuid4()), stop=_stop):
            if event.method == "StartPlaybackEvent":
                _playback_start = time.monotonic()
                print("[kotwica] StartPlaybackEvent", file=sys.stderr, flush=True)
                return
    except OSError as error:  # glosnik nieosiagalny - nie przerywamy pomiaru
        print("[kotwica] nasluch padl: %s" % error, file=sys.stderr, flush=True)


def _in_flight(audio_emitted: float) -> str:
    """Sekundy audio oddane, a jeszcze nieuslyszane. Bez kotwicy nie do policzenia."""
    if _playback_start is None:
        return "      -"
    return "%+7.1fs" % (audio_emitted - (time.monotonic() - _playback_start))


def main() -> int:
    if SPEAKER:
        threading.Thread(target=_watch_for_playback, daemon=True).start()
    else:
        print(
            "[kotwica] WAM_SPEAKER nieustawiony - kolumna 'w locie' bedzie pusta",
            file=sys.stderr,
            flush=True,
        )

    src = sys.stdin.buffer
    dst = sys.stdout.buffer

    start = time.monotonic()
    total = 0
    mark_time = start
    mark_bytes = 0

    print(
        "czas   przyrost   x realtime   audio oddane   od startu   w locie",
        file=sys.stderr,
        flush=True,
    )

    try:
        while True:
            data = src.read(CHUNK)
            if not data:
                break
            dst.write(data)
            total += len(data)

            now = time.monotonic()
            if now - mark_time < INTERVAL:
                continue

            rate = (total - mark_bytes) / (now - mark_time)
            elapsed = now - start
            audio = total / REALTIME
            print(
                "%5.1fs %8.1f kB/s %8.2fx %12.1fs %+9.1fs %s"
                % (elapsed, rate / 1024, rate / REALTIME, audio, audio - elapsed, _in_flight(audio)),
                file=sys.stderr,
                flush=True,
            )
            mark_time, mark_bytes = now, total
    finally:
        _stop.set()

    dst.flush()
    print(
        "KONIEC: %.1f MB w %.1fs" % (total / 1048576, time.monotonic() - start),
        file=sys.stderr,
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
