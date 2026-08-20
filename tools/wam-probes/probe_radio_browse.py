"""Spacer po drzewie radia: zejscie, wyjscie w gore, szczegol stacji.

Ksztalt komend nie jest zgadniety - to doslownie ciagi formatujace wyjete z DEX-a
oficjalnej aplikacji (`Wireless Audio-Multiroom (Tab)` 4164):

    CPM?cmd=<name>GetSelectRadioList</name><p type="dec" name="contentid" val="%s"/>
            <p type="dec" name="startindex" val="0"/><p type="dec" name="listcount" val="%s"/>
    CPM?cmd=<name>GetUpperRadioList</name><p type="dec" name="startindex" val="0"/>
            <p type="dec" name="listcount" val="%s"/>
    CPM?cmd=<name>GetStationData</name><p type="dec" name="selectitemid" val="%s"/>

Wszystko czyta. Zmienia sie tylko kursor przegladania w glosniku, a
`GetUpperRadioList` cofa go z powrotem - zadnego odtwarzania, zadnego presetu.

NIE odpalac przy zywym PCM.

Uwaga zmierzona przy pisaniu tego skryptu: podsystem CPM potrafi sie zaciac przy
szybkiej serii zapytan - najpierw oddaje listy z `totallistcount=0`, potem milknie
calkiem na ~20-30 s, podczas gdy UIC odpowiada normalnie. Wraca sam. Stad pauzy
miedzy komendami i ponowienie przy pustej liscie.
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
SCRATCH = Path(os.environ.get("WAM_SCRATCH", "_scratch"))
OPENER = build_opener(ProxyHandler({}))
LOG: list[str] = []

CATEGORY_RE = re.compile(r'<category isroot="(\d)">([^<]*)</category>')
MENUITEM_RE = re.compile(r"<menuitem\b(.*?)</menuitem>", re.S)
FIELD_RE = re.compile(r"<(\w+)>([^<]*)</\1>")
TYPE_RE = re.compile(r'type="(\d+)"')
TOTAL_RE = re.compile(r"<totallistcount>(\d+)</totallistcount>")


def cpm(method: str, args: list | None = None, timeout: float = 8.0) -> str:
    url = build_api_url(SPEAKER, method, args, api_type="CPM")
    try:
        with OPENER.open(url, timeout=timeout) as response:  # nosec B310
            body = response.read(1024 * 1024).decode("utf-8", "replace")
    except OSError as error:
        body = f"<!-- {type(error).__name__}: {error} -->"
    LOG.append(f"\n===== CPM {method} {args or ''} =====\n{body}\n")
    return body


def fetch_list(method: str, args: list, attempts: int = 3) -> str:
    """Pobierz strone listy, ponawiajac gdy CPM oddaje pusty poziom.

    `totallistcount=0` na poziomie, ktory ma zawartosc, oznacza zwykle podsystem CPM
    w trakcie dochodzenia do siebie, a nie pusta kategorie. Ulubione naprawde bywaja
    puste, wiec po wyczerpaniu prob oddajemy ostatnia odpowiedz taka, jaka jest.
    """
    body = ""
    for attempt in range(attempts):
        body = cpm(method, args)
        total = TOTAL_RE.search(body)
        if total and total.group(1) != "0":
            return body
        if attempt + 1 < attempts:
            print(f"    (pusta lista z {method}, ponawiam)")
            time.sleep(2.0)
    return body


def page(body: str) -> tuple[str, str, list[dict[str, str]]]:
    """Zwroc (kategoria, isroot, pozycje).

    Dwa ksztalty pozycji, oba zmierzone: katalog to `<menuitem type="0">` z samym
    tytulem i `contentid`, stacja to `<menuitem type="2" cat="stations">` z dodatkowym
    `mediaid`, `thumbnail` i `description`. Kolejnosc pol nie jest stala, wiec kazdy
    `menuitem` idzie jako osobny blok, a nie jednym wzorcem na calosc.
    """
    category = CATEGORY_RE.search(body)
    items = []
    for block in MENUITEM_RE.findall(body):
        entry = dict(FIELD_RE.findall(block))
        found = TYPE_RE.search(block.split(">", 1)[0])
        entry["type"] = found.group(1) if found else "?"
        items.append(entry)
    return (
        category.group(2) if category else "?",
        category.group(1) if category else "?",
        items,
    )


def show(label: str, body: str) -> list[dict[str, str]]:
    category, isroot, items = page(body)
    total = TOTAL_RE.search(body)
    print(f"\n{label}")
    print(f"  category={category!r} isroot={isroot} total={total.group(1) if total else '?'}")
    for item in items[:8]:
        kind = "katalog" if item["type"] == "0" else f"typ{item['type']}"
        media = f"  mediaid={item['mediaid']}" if item.get("mediaid") else ""
        print(f"    [{item.get('contentid', '?'):>3}] {kind:<8} {item.get('title', '?')}{media}")
    if len(items) > 8:
        print(f"    ... i jeszcze {len(items) - 8}")
    return items


def normalise_to_root(attempts: int = 5) -> None:
    """Walk up until the speaker's browse cursor is at the catalogue root.

    The cursor lives in the speaker and **survives the client process**, so a
    fresh run does not start at the root - it starts wherever the last one
    stopped. Descending from there lands somewhere unintended and returns an
    empty level, which is indistinguishable from the CPM wedge this file also
    documents. Cost two debugging detours on 2026-08-19 before it was noticed.
    """
    for _ in range(attempts):
        body = cpm("GetUpperRadioList", [("startindex", 0, "dec"), ("listcount", 30, "dec")])
        if re.search(r'<category isroot="1"', body):
            return
        time.sleep(1.0)
    print("  (nie udalo sie dojsc do korzenia - wyniki ponizej moga byc z innego poziomu)")


def main() -> None:
    SCRATCH.mkdir(parents=True, exist_ok=True)
    print(f"glosnik: {SPEAKER}")

    cpm("SetSelectRadio")
    time.sleep(0.4)
    normalise_to_root()
    root = show(
        "KORZEN (GetCurrentRadioList)",
        fetch_list("GetCurrentRadioList", [("startindex", 0, "dec"), ("listcount", 30, "dec")]),
    )

    # Zejscie w pierwszy katalog, ktory nie jest ulubionymi ani historia: te dwa
    # bywaja puste i nic by nie powiedzialy o ksztalcie poziomu nizej.
    target = next(
        (i for i in root if i.get("title") not in {"Favorites", "Recents"}), None
    )
    if target is None:
        print("\nbrak katalogu do zejscia - koniec")
        return

    contentid, title = target["contentid"], target.get("title", "?")
    time.sleep(0.4)
    child = show(
        f"ZEJSCIE contentid={contentid} ({title})",
        fetch_list(
            "GetSelectRadioList",
            [
                ("contentid", int(contentid), "dec"),
                ("startindex", 0, "dec"),
                ("listcount", 30, "dec"),
            ],
        ),
    )

    # Stacja to pozycja o typie innym niz 0 - na niej sprawdzamy GetStationData.
    station = next((i for i in child if i["type"] != "0"), None)
    if station is None:
        print("\nbrak stacji na tym poziomie - GetStationData pominiete")
    else:
        time.sleep(0.4)
        body = cpm("GetStationData", [("selectitemid", int(station["contentid"]), "dec")])
        print(
            f"\nSZCZEGOL STACJI selectitemid={station['contentid']} "
            f"({station.get('title', '?')})"
        )
        for key, value in FIELD_RE.findall(body):
            if key not in {"version", "speakerip", "user_identifier"}:
                print(f"    {key}={value[:90]}")

    time.sleep(0.4)
    show(
        "POWROT W GORE (GetUpperRadioList)",
        fetch_list("GetUpperRadioList", [("startindex", 0, "dec"), ("listcount", 30, "dec")]),
    )


if __name__ == "__main__":
    try:
        main()
    finally:
        # Surowe odpowiedzi sa jedynym trwalym sladem przebiegu, wiec zapis nie moze
        # zalezec od tego, czy main() doszedl do konca.
        out = SCRATCH / "radio_browse.xml"
        out.write_text("".join(LOG), encoding="utf-8")
        print(chr(10) + f"Surowe odpowiedzi: {out}")
