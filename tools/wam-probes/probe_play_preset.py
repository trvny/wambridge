"""Czemu `SetPlayPreset` oddaje `StopPlaybackEvent` i nic nie gra.

Pytanie otwarte z punktu 14 w `DEVELOPMENT_STATUS.md`: `--tunein-list` zwraca
wszystkie 15 presetow, ale odtworzenie ktoregokolwiek konczy sie zdarzeniem
zatrzymania. Dwie hipotezy warte rozroznienia:

  H1  Usluga TuneIn po stronie Samsunga juz nie dziala i zaden argument nie pomoze.
  H2  Wolamy poprawna komende ze zlym argumentem.

H2 ma konkretny ksztalt. `WamPreset.preset_index` (tunein.py:45-52) zwraca
`int(content_id)`, czyli identyfikator stacji w TuneIn. Ale parametr nazywa sie
`presetindex`, a w oficjalnej aplikacji indeks presetu to zwykle **pozycja na
liscie** (0..N-1). Jesli glosnik dostaje numer, ktorego nie ma wsrod swoich
pozycji, zatrzymanie odtwarzania jest dokladnie tym, czego nalezy sie spodziewac.

Ta proba: czyta liste surowo (zeby zobaczyc, czy wezly niosa wlasny indeks),
potem probuje `SetPlayPreset` obiema interpretacjami i po kazdej pyta glosnik,
co faktycznie gra.

ODTWARZA DZWIEK. Ustawia glosnosc na 3 przed pierwsza proba (regula z AGENTS.md)
i zatrzymuje odtwarzanie na koniec. Nie odpalac przy zywym PCM - port 55001 ma
miec jednego wlasciciela.

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


def now_playing() -> None:
    show("GetRadioInfo", call("CPM", "GetRadioInfo"))


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
    time.sleep(PAUSE)
    now_playing()

    # Wariant B: pozycja na liscie.
    time.sleep(PAUSE)
    show(
        f"B) SetPlayPreset presettype={preset_type} presetindex=0 (pozycja na liscie)",
        call("CPM", "SetPlayPreset",
             [("presettype", preset_type, "dec"), ("presetindex", 0, "dec")]),
    )
    time.sleep(PAUSE)
    now_playing()

    # Wariant C: druga pozycja, zeby odroznic "0 tez nie dziala" od "indeks dziala".
    time.sleep(PAUSE)
    show(
        f"C) SetPlayPreset presettype={preset_type} presetindex=1",
        call("CPM", "SetPlayPreset",
             [("presettype", preset_type, "dec"), ("presetindex", 1, "dec")]),
    )
    time.sleep(PAUSE)
    now_playing()

    # Cisza po sobie.
    time.sleep(PAUSE)
    show("SetPlaybackControl stop",
         call("UIC", "SetPlaybackControl", [("playbackcontrol", "stop", "str")]))
    show("GetFunc (po)", call("UIC", "GetFunc"))


if __name__ == "__main__":
    main()
