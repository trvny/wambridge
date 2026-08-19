"""Co wpycha M5 w `submode=cp`, czy to boli, i jak z tego wyjsc.

Rozstrzygniete 19.08.2026 na fizycznym M5. Trzy tezy, ktore ta sonda obala albo potwierdza:

1. "Przegladanie radia wpycha glosnik w cp" - **nieprawda**. Caly spacer po drzewie zostawia
   `dlna`. Ten wniosek zdazyl trafic do dokumentacji, zanim ktokolwiek go sprawdzil.
2. "W cp nic nie zagra" - **nieprawda dla sciezki URL**. `cp` to wlasnie tryb, w ktorym
   `SetUrlPlayback` gra, ze slyszalnym dzwiekiem wlacznie.
3. "Z cp wychodzi sie tylko wyjeciem z pradu" - **nieprawda**. Objazd `SetFunc` przez inne
   zrodlo i z powrotem na `wifi` wraca do `dlna`.

Sonda gra przez chwile na glos. Ustawia glosnosc na `WAM_TEST_VOLUME` (domyslnie 3) i oddaje
poprzednia wartosc na koncu, takze gdy przebieg sie wywroci.
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

SPEAKER = os.environ.get("WAM_SPEAKER", "192.168.1.50")
# Zwykly shoutcast z paczki stacji projektu - nie TuneIn, zeby nie mieszac w wynik
# stanu konta i partnerId glosnika.
TEST_STREAM = os.environ.get("WAM_TEST_STREAM", "http://stream3.polskieradio.pl:8906/;stream")
TEST_VOLUME = int(os.environ.get("WAM_TEST_VOLUME", "3"))
OPENER = build_opener(ProxyHandler({}))
FIELD_RE = re.compile(r"<(\w+)>([^<]*)</\1>")
LIST_ARGS = [("startindex", 0, "dec"), ("listcount", 30, "dec")]


def call(api: str, method: str, args: list | None = None, timeout: float = 12.0) -> dict[str, str]:
    url = build_api_url(SPEAKER, method, args, api_type=api)
    try:
        with OPENER.open(url, timeout=timeout) as response:  # nosec B310
            return dict(FIELD_RE.findall(response.read(1024 * 1024).decode("utf-8", "replace")))
    except OSError as error:
        return {"_error": type(error).__name__}


def submode(label: str) -> str:
    got = call("UIC", "GetFunc")
    value = got.get("submode", "")
    print(f"  {label:<36} function={got.get('function', '?'):<6} submode={value or '(puste)'}")
    return value


def play(url: str) -> dict[str, str]:
    return call(
        "UIC",
        "SetUrlPlayback",
        [
            ("url", url, "cdata"),
            ("buffersize", 0, "dec"),
            ("seektime", 0, "dec"),
            ("resume", 1, "dec"),
        ],
    )


def main() -> None:
    print(f"glosnik: {SPEAKER}\n")
    baseline = call("UIC", "GetVolume").get("volume", "")
    call("UIC", "SetVolume", [("volume", TEST_VOLUME, "dec")])

    try:
        print("1. Czy przegladanie radia rusza submode")
        submode("przed")
        call("CPM", "SetSelectRadio")
        time.sleep(1.0)
        submode("SetSelectRadio")
        call("CPM", "GetCurrentRadioList", LIST_ARGS)
        time.sleep(1.0)
        submode("GetCurrentRadioList")
        call("CPM", "GetSelectRadioList", [("contentid", 1, "dec"), *LIST_ARGS])
        time.sleep(1.0)
        submode("GetSelectRadioList (zejscie)")
        call("CPM", "GetStationData", [("selectitemid", 0, "dec")])
        time.sleep(1.0)
        submode("GetStationData")
        call("CPM", "GetUpperRadioList", LIST_ARGS)
        time.sleep(1.0)
        after_browse = submode("GetUpperRadioList")
        print(f"   -> przegladanie {'RUSZA' if after_browse == 'cp' else 'NIE rusza'} submode")

        print("\n2. Co naprawde wpycha w cp: SetUrlPlayback")
        answer = play(TEST_STREAM)
        print(f"   odpowiedz: {answer.get('method', '?')} {answer.get('errCode', '')}")
        time.sleep(6.0)
        playing = submode("po SetUrlPlayback")
        # W cp `GetPlayStatus` nie oddaje w ogole pola `playstatus`, a `GetMusicInfo`
        # zwraca blad - zadne z nich nie jest dowodem, ze nie gra. Dlatego tylko drukujemy.
        status = call("UIC", "GetPlayStatus")
        music = call("UIC", "GetMusicInfo")
        print(f"   GetPlayStatus  playstatus={status.get('playstatus', '(brak pola)')!r}")
        print(f"   GetMusicInfo   {music.get('errCode', music.get('method', '?'))}")
        print("   -> posluchaj: jesli gra, to `cp` nie jest usterka, tylko trybem odtwarzania")

        if playing != "cp":
            print("\n   Glosnik nie wszedl w cp - dalsza czesc nie ma czego rozstrzygac.")
            return

        print("\n3. Czy w cp da sie przelaczyc strumien")
        second = play("http://41.dktr.pl:8000/trojka.ogg")
        print(f"   odpowiedz: {second.get('method', '?')} {second.get('errCode', '')}")
        time.sleep(5.0)
        submode("po przelaczeniu")

        print("\n4. Wyjscie z cp bez wyjmowania z pradu")
        for source in ("bt", "aux", "soundshare"):
            call("UIC", "SetFunc", [("function", source, "str")])
            time.sleep(2.5)
            submode(f"SetFunc {source}")
            call("UIC", "SetFunc", [("function", "wifi", "str")])
            time.sleep(2.5)
            back = submode("SetFunc wifi")
            if back != "cp":
                print(f"\n   ROZSTRZYGNIETE: objazd przez '{source}' wraca do '{back}'.")
                return
        print("\n   Zaden objazd nie pomogl - wtedy dopiero zostaje wyjecie z pradu.")
    finally:
        if baseline.isdigit():
            call("UIC", "SetVolume", [("volume", int(baseline), "dec")])
            print(f"\nglosnosc przywrocona do {baseline}")


if __name__ == "__main__":
    main()
