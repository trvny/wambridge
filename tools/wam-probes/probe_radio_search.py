"""Wyszukiwanie stacji po nazwie - ostatni nietkniety kawalek powierzchni radiowej.

Ksztalty komend wyjete z DEX-a oficjalnej apki, nie zgadniete:

    CPM?cmd=<name>SearchQuery</name><p type="str" name="query" val="%s"/>
           <p type="dec" name="startindex" val="%d"/><p type="dec" name="listcount" val="%d"/>
           [<p type="str" name="type" val="fast"/>]
    CPM?cmd=<name>GetGenreStations</name>
    CPM?cmd=<name>GlobalSearch</name><p type="str_arr" name="cpnames">%s</p>...

Wszystko czyta. Kursor przegladania normalizowany na wejsciu, bo zyje w glosniku i przezywa
proces - patrz `probe_radio_browse.py`.
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
QUERY = os.environ.get("WAM_QUERY", "Trojka")
OPENER = build_opener(ProxyHandler({}))
FIELD_RE = re.compile(r"<(\w+)>([^<]*)</\1>")
ITEM_RE = re.compile(r"<menuitem\b(.*?)</menuitem>", re.S)


def cpm(method: str, args: list | None = None, timeout: float = 12.0) -> str:
    try:
        with OPENER.open(  # nosec B310
            build_api_url(SPEAKER, method, args, api_type="CPM"),
            timeout=timeout,
        ) as response:
            return response.read(1024 * 1024).decode("utf-8", "replace")
    except OSError as error:
        return f"<!--{type(error).__name__}-->"


def summarise(label: str, body: str) -> str:
    if body.startswith("<!--"):
        print(f"{label:<30} {body}")
        return body
    method = re.search(r"<method>(\w+)", body)
    total = re.search(r"<totallistcount>(\d+)", body)
    error = re.search(r"<errmessage>([^<]*)", body)
    items = ITEM_RE.findall(body)
    print(
        f"{label:<30} {method.group(1) if method else '?':<14} "
        f"total={total.group(1) if total else '-':<5} items={len(items)} "
        f"{error.group(1) if error else ''}"
    )
    return body


def main() -> None:
    print(f"glosnik: {SPEAKER}   szukane: {QUERY!r}\n")
    cpm("SetSelectRadio")
    time.sleep(1.2)
    for _ in range(3):
        body = cpm("GetUpperRadioList", [("startindex", 0, "dec"), ("listcount", 30, "dec")])
        if re.search(r'<category isroot="1"', body):
            break
        time.sleep(1.0)

    page = [("startindex", 0, "dec"), ("listcount", 10, "dec")]

    time.sleep(1.0)
    summarise("GetGenreStations", cpm("GetGenreStations"))
    time.sleep(1.5)
    body = summarise("SearchQuery", cpm("SearchQuery", [("query", QUERY, "str"), *page]))
    time.sleep(1.5)
    summarise(
        "SearchQuery type=fast",
        cpm("SearchQuery", [("query", QUERY, "str"), *page, ("type", "fast", "str")]),
    )

    # Wyniki sa mieszane: `type="0"` to kategoria bez `mediaid` (np. "Artist: Trojka"),
    # a stacja niesie `mediaid`, ktore idzie prosto do Tune.ashx. To domyka droga
    # "szukaj po nazwie -> grywalny adres" bez przegladania drzewa i bez presetow.
    print("\nwyniki:")
    for block in ITEM_RE.findall(body):
        entry = dict(FIELD_RE.findall(block))
        kind = re.search(r'type="(\d+)"', block.split(">", 1)[0])
        print(
            f"  [{entry.get('contentid', '?'):>3}] typ={kind.group(1) if kind else '?'} "
            f"{entry.get('mediaid', '(kategoria)'):<10} {entry.get('title', '?')[:46]}"
        )


if __name__ == "__main__":
    main()
