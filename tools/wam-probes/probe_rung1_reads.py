"""Rung 1 z docs/WAM_PROTOCOL.md: caly odczytowy material naraz, zero zapisow.

Powod na jeden przebieg zamiast kilku: wyjscie do sprzetu jest drogie, a pojedynczy
odczyt kosztuje gniazdo. Glowne pytanie to `GetUpperRadioList` / `GetCurrentRadioList`
- czy powierzchnia radia to drzewo, czy plaska lista - ale skoro i tak jedziemy, to
zbieramy przy okazji cala reszte rungu 1.

Firmware, ktore nie zna komendy, po prostu MILCZY - timeout to wynik, nie awaria.
Dlatego kazda komenda ma wlasny timeout i nic nie przerywa przebiegu.

NIE odpalac przy zywym PCM: druga sesja na 55001 potrafila wywalic odtwarzanie.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from wambridge.samsung import build_api_url  # noqa: E402

SPEAKER = os.environ.get("WAM_SPEAKER", "192.168.1.50")
SCRATCH = Path(os.environ.get("WAM_SCRATCH", "_scratch"))
OPENER = build_opener(ProxyHandler({}))

# (api, metoda, argumenty, timeout). Kolejnosc ma znaczenie: radio najpierw,
# bo `SetSelectRadio` musi poprzedzac listy, a reszta jest od niego niezalezna.
LIST_ARGS = [("startindex", 0, "dec"), ("listcount", 30, "dec")]

READS: list[tuple[str, str, list, float]] = [
    # --- pytanie glowne: ksztalt powierzchni radia ---
    ("CPM", "SetSelectRadio", [], 8.0),
    ("CPM", "GetCurrentRadioList", LIST_ARGS, 8.0),
    ("CPM", "GetUpperRadioList", LIST_ARGS, 8.0),
    # --- czy istnieje "prawie czuwanie" i jak sie nazywa ---
    ("UIC", "GetNetworkStandByMode", [], 4.0),
    # --- powierzchnia dzwieku, tylko odczyt ---
    ("UIC", "GetCurrentEQMode", [], 4.0),
    ("UIC", "Get7BandEQList", [], 4.0),
    ("UIC", "GetEQBass", [], 4.0),
    ("UIC", "GetEQTreble", [], 4.0),
    ("UIC", "GetEQBalance", [], 4.0),
    ("UIC", "GetEQDrc", [], 4.0),
    ("UIC", "GetWooferLevel", [], 4.0),
    ("UIC", "GetRearLevel", [], 4.0),
    ("UIC", "GetAudioQuality", [], 4.0),
    # --- stan urzadzenia ---
    ("UIC", "GetSpeakerStatus", [], 4.0),
    ("UIC", "GetPlayStatus", [], 4.0),
    ("UIC", "GetAvSourceAll", [], 6.0),
    ("UIC", "GetLed", [], 4.0),
    ("UIC", "GetBatteryStatus", [], 4.0),
    ("UIC", "GetGroupName", [], 4.0),
    ("UIC", "SpkInGroup", [], 4.0),
    ("UIC", "GetAlarmInfo", [], 6.0),
]


def send(api: str, method: str, args: list, timeout: float) -> tuple[str, str]:
    """Zwroc (status, surowe cialo). Status: ok | timeout | blad."""
    url = build_api_url(SPEAKER, method, args or None, api_type=api)
    try:
        with OPENER.open(url, timeout=timeout) as response:  # nosec B310
            return "ok", response.read(1024 * 1024).decode("utf-8", "replace")
    except URLError as error:
        return "timeout", f"{error}"
    except OSError as error:
        return "blad", f"{error}"


def summarise(body: str) -> str:
    """Jedna linia: metoda z odpowiedzi, kod bledu albo dlugosc ciala."""
    import re

    name = re.search(r"<method>([^<]*)</method>", body)
    err = re.search(r"<errcode>([^<]*)</errcode>", body, re.IGNORECASE)
    msg = re.search(r"<errmessage>([^<]*)</errmessage>", body, re.IGNORECASE)
    label = name.group(1) if name else "(bez <method>)"
    if err:
        return f"{label}  BLAD {err.group(1)} {msg.group(1) if msg else ''}".strip()
    return f"{label}  {len(body)} B"


def main() -> None:
    SCRATCH.mkdir(parents=True, exist_ok=True)
    out = SCRATCH / "rung1_reads.xml"
    print(f"glosnik : {SPEAKER}")
    print(f"surowe  : {out}\n")

    with out.open("w", encoding="utf-8") as sink:
        for api, method, args, timeout in READS:
            status, body = send(api, method, args, timeout)
            print(f"  {api} {method:<22} {status:<8} {summarise(body) if status == 'ok' else body}")
            sink.write(f"\n===== {api} {method} [{status}] =====\n{body}\n")
            time.sleep(0.3)

    print(f"\nGotowe. Cale odpowiedzi w {out}")


if __name__ == "__main__":
    main()
