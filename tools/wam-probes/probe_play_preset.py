"""Czemu `SetPlayPreset` oddaje `StopPlaybackEvent` i nic nie gra.

Pytanie otwarte z punktu 14 w `DEVELOPMENT_STATUS.md`: `--tunein-list` zwraca
wszystkie 15 presetow, ale odtworzenie ktoregokolwiek konczy sie zdarzeniem
zatrzymania. Dwie hipotezy warte rozroznienia:

  H1  Usluga TuneIn po stronie Samsunga juz nie dziala i zaden argument nie pomoze.
  H2  Wolamy poprawna komende ze zlym argumentem.

**Odpowiedz, zmierzona 25.08.2026: zadna z nich.** Komenda dziala, a mylila
metoda obserwacji. `StopPlaybackEvent` wraca po **kazdym** `SetPlayPreset`, takze
po tych, ktore zaraz potem graja - to potwierdzenie zerwania poprzedniego
odtwarzania, nie blad. Do tego start trwa 2-5 s i waha sie dla tego samego
presetu, wiec pojedyncza probka po stalym czasie raz mowi `play`, raz `stop`.
Dwa pierwsze przebiegi tej proby wskazaly z tego powodu **rozne** dzialajace
presety, zanim zmierzono samo opoznienie.

Obalone przy okazji: `contentid` w `GetPresetList` **jest** pozycja na liscie
(0..N-1), wiec `WamPreset.preset_index` zwracajacy `int(content_id)` jest
poprawny, a mapowanie `kind` na `presettype` zgadza sie z tym, co glosnik
przyjmuje.

Ta proba zostaje jako narzedzie: czyta liste surowo, probuje trzech wariantow
i po kazdym **odpytuje** do skutku zamiast probkowac raz.

ODTWARZA DZWIEK. Ustawia glosnosc na 3 przed pierwsza proba (regula z AGENTS.md)
i na koniec wychodzi z `cp` objazdem przez `aux` - `SetPlaybackControl stop`
odpowiada czysto i nie zatrzymuje niczego. Nie odpalac przy zywym PCM: port
55001 ma miec jednego wlasciciela.

Podsystem CPM zacina sie przy szybkiej serii zapytan (patrz probe_radio_browse),
stad pauzy.
"""

from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path
from urllib.request import ProxyHandler, build_opener

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from wambridge.samsung import build_api_url  # noqa: E402

SPEAKER = os.environ.get("WAM_SPEAKER", "10.0.0.104")
PORT = int(os.environ.get("WAM_PORT", "55001"))
OPENER = build_opener(ProxyHandler({}))
PAUSE = float(os.environ.get("WAM_PAUSE", "4"))


def call(api: str, command: str, args=None, timeout: float = 12.0) -> str:
    """Wyslij jedna komende i oddaj surowe cialo odpowiedzi."""
    url = build_api_url(SPEAKER, command, args, port=PORT, api_type=api)
    try:
        with OPENER.open(url, timeout=timeout) as response:
            return response.read().decode("utf-8", "replace")
    except Exception as error:  # noqa: BLE001 - proba raportuje, nie przerywa
        return f"<<BLAD {type(error).__name__}: {error}>>"


def show(label: str, body: str) -> None:
    method = re.search(r"<method>([^<]+)</method>", body)
    result = re.search(r'result="([^"]+)"', body)
    print(f"\n### {label}")
    print(f"    method={method.group(1) if method else '?'} "
          f"result={result.group(1) if result else '?'}")
    print("    " + body.strip()[:600].replace("\n", "\n    "))


def field(body: str, name: str) -> str:
    match = re.search(rf"<{name}>([^<]*)</{name}>", body)
    return match.group(1) if match else "-"


def wait_for_play(budget: float = 25.0) -> None:
    """Odpytuj, az zagra - jedna probka po stalym czasie klamie.

    Start trwa 2-5 s i waha sie dla tego samego presetu, wiec pojedynczy odczyt
    po czterech sekundach raz mowi `play`, raz `stop`. To jest dokladnie ten blad,
    ktory kazal uznac cala komende za zepsuta.
    """
    deadline = time.monotonic() + budget
    while time.monotonic() < deadline:
        info = call("CPM", "GetRadioInfo")
        elapsed = budget - (deadline - time.monotonic())
        status = field(info, "playstatus")
        if status == "play":
            print(f"    +{elapsed:4.1f}s GRA  idx={field(info, 'presetindex')} "
                  f"title={field(info, 'title')[:40]}")
            return
        time.sleep(2)
    print(f"    nie ruszylo w {budget:.0f} s")


def leave_cp(budget: float = 15.0) -> None:
    """Wyjdz z `cp` udokumentowanym objazdem przez inne zrodlo.

    `SetPlaybackControl stop` i `pause` odpowiadaja czysto i nie robia nic
    (WAM_PROTOCOL.md, "Leaving cp takes two commands"). Jedyne, co przenosi
    glosnik, to podroz przez inne zrodlo - `aux`, bo `bt` mowi na glos
    "Bluetooth is ready". Przelaczenie nie jest natychmiastowe, stad odpytywanie.
    """
    call("UIC", "SetFunc", [("function", "aux", "str")])
    time.sleep(2)
    call("UIC", "SetFunc", [("function", "wifi", "str")])
    deadline = time.monotonic() + budget
    while time.monotonic() < deadline:
        func = call("UIC", "GetFunc")
        if field(func, "submode") != "cp":
            print(f"    wyszlo z cp: function={field(func, 'function')} "
                  f"submode={field(func, 'submode')}")
            return
        time.sleep(2)
    print("    NADAL w cp - glosnik zostal grajacy, zatrzymaj recznie")


def main() -> None:
    print(f"glosnik: {SPEAKER}:{PORT}")

    show("GetFunc (przed)", call("UIC", "GetFunc"))
    call("UIC", "SetVolume", [("volume", 3, "dec")])
    time.sleep(PAUSE)

    show("SetSelectRadio", call("CPM", "SetSelectRadio"))
    time.sleep(PAUSE)

    presets = call("CPM", "GetPresetList",
                   [("startindex", 0, "dec"), ("listcount", 30, "dec")])
    show("GetPresetList (surowo)", presets)

    # Czego szukamy: czy wezel <preset> niesie wlasny indeks obok contentid.
    nodes = re.findall(r"<preset\b.*?</preset>", presets, re.S)
    print(f"\n    presetow w odpowiedzi: {len(nodes)}")
    if nodes:
        print("    pierwszy wezel w calosci:")
        print("    " + nodes[0][:500].replace("\n", "\n    "))
    ids = re.findall(r"<contentid>([^<]+)</contentid>", presets)
    kinds = re.findall(r"<kind>([^<]+)</kind>", presets)
    print(f"    contentid: {ids[:6]}")
    print(f"    kind     : {kinds[:6]}")

    if not ids:
        print("\nBrak presetow - dalsze proby nie mialyby czego odtwarzac.")
        return

    kind = (kinds[0] if kinds else "speaker").strip().casefold()
    preset_type = 1 if kind == "speaker" else 0

    # Wariant A: to, co robi dzis tunein.py - contentid jako presetindex.
    time.sleep(PAUSE)
    show(
        f"A) SetPlayPreset presettype={preset_type} presetindex={ids[0]} (contentid, stan obecny)",
        call("CPM", "SetPlayPreset",
             [("presettype", preset_type, "dec"), ("presetindex", int(ids[0]), "dec")]),
    )
    wait_for_play()

    # Wariant B: pozycja na liscie.
    time.sleep(PAUSE)
    show(
        f"B) SetPlayPreset presettype={preset_type} presetindex=0 (pozycja na liscie)",
        call("CPM", "SetPlayPreset",
             [("presettype", preset_type, "dec"), ("presetindex", 0, "dec")]),
    )
    wait_for_play()

    # Wariant C: druga pozycja, zeby odroznic "0 tez nie dziala" od "indeks dziala".
    time.sleep(PAUSE)
    show(
        f"C) SetPlayPreset presettype={preset_type} presetindex=1",
        call("CPM", "SetPlayPreset",
             [("presettype", preset_type, "dec"), ("presetindex", 1, "dec")]),
    )
    wait_for_play()

    # Cisza po sobie - objazdem, bo stop i pause nic tu nie robia.
    print("\n### wyjscie z cp")
    leave_cp()
    show("GetFunc (po)", call("UIC", "GetFunc"))


if __name__ == "__main__":
    main()
