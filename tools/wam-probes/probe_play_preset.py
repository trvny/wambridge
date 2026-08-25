"""Why `SetPlayPreset` answers `StopPlaybackEvent` and nothing seems to play.

The open question from item 14 in `DEVELOPMENT_STATUS.md`: `--tunein-list`
returns all 15 presets, yet playing any of them ends in a stop event. Two
hypotheses were worth separating:

  H1  Samsung's TuneIn service is dead and no argument helps.
  H2  We send the right command with the wrong argument.

**Answer, measured 2026-08-25: neither.** The command works; the observation
method was wrong. `StopPlaybackEvent` comes back from *every* `SetPlayPreset`,
including the calls that then play - it reports that the previous playback was
torn down, not that this one failed. On top of that, a start takes 2-5 s and
varies for the same preset, so a single sample at a fixed delay says `play` one
run and `stop` the next. Two earlier runs of this probe named *different*
working presets for exactly that reason, before the latency itself was measured.

Retired along the way: `contentid` in `GetPresetList` **is** the list position
(0..N-1), so `WamPreset.preset_index` returning `int(content_id)` is right, and
the `kind`-to-`presettype` mapping matches what the speaker accepts.

The probe is kept as a tool: it reads the list raw, tries three variants, and
after each one **polls** until the speaker answers rather than sampling once.

PLAYS AUDIO. Sets volume to 3 before the first attempt (the rule in AGENTS.md)
and leaves `cp` through `aux` at the end - `SetPlaybackControl stop` answers
cleanly and stops nothing. Do not run it against live PCM: port 55001 must have
a single owner.

The CPM subsystem wedges under a rapid series of queries (see
probe_radio_browse), which is what the pauses are for.
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
ERROR_PREFIX = "<<ERROR "


def call(api: str, command: str, args=None, timeout: float = 12.0) -> str:
    """Send one command and return the raw response body."""
    url = build_api_url(SPEAKER, command, args, port=PORT, api_type=api)
    try:
        with OPENER.open(url, timeout=timeout) as response:
            return response.read().decode("utf-8", "replace")
    except Exception as error:  # noqa: BLE001 - a probe reports, it does not abort
        return f"{ERROR_PREFIX}{type(error).__name__}: {error}>>"


def field(body: str, name: str) -> str:
    match = re.search(rf"<{name}>([^<]*)</{name}>", body)
    return match.group(1) if match else "-"


def show(label: str, body: str) -> None:
    method = re.search(r"<method>([^<]+)</method>", body)
    result = re.search(r'result="([^"]+)"', body)
    print(f"\n### {label}")
    print(f"    method={method.group(1) if method else '?'} "
          f"result={result.group(1) if result else '?'}")
    print("    " + body.strip()[:600].replace("\n", "\n    "))


def wait_for_play(budget: float = 25.0) -> None:
    """Poll until it plays - one sample at a fixed delay lies.

    A start takes 2-5 s and varies for the same preset, so a single read at four
    seconds reports `play` one run and `stop` the next. That is precisely the
    mistake that had the whole command written off as broken.
    """
    deadline = time.monotonic() + budget
    while time.monotonic() < deadline:
        info = call("CPM", "GetRadioInfo")
        elapsed = budget - (deadline - time.monotonic())
        if field(info, "playstatus") == "play":
            print(f"    +{elapsed:4.1f}s PLAYING idx={field(info, 'presetindex')} "
                  f"title={field(info, 'title')[:40]}")
            return
        time.sleep(2)
    print(f"    did not start within {budget:.0f} s")


def read_presets(attempts: int = 4) -> str:
    """Fetch the preset list, retrying - an empty list is not evidence.

    The CPM subsystem wedges under a series of queries and answers
    `totallistcount=0`, and under a longer wedge it goes silent until the
    timeout (`call` then returns an error string). WAM_PROTOCOL.md says plainly
    that an empty list must not be taken as an answer without a retry.
    """
    body = ""
    for attempt in range(1, attempts + 1):
        body = call("CPM", "GetPresetList",
                    [("startindex", 0, "dec"), ("listcount", 30, "dec")])
        total = field(body, "totallistcount")
        if not body.startswith(ERROR_PREFIX) and total not in ("", "-", "0"):
            return body
        print(f"    empty list or no answer (attempt {attempt}/{attempts}), waiting...")
        time.sleep(PAUSE * 2)
    return body


def leave_cp(budget: float = 15.0) -> None:
    """Leave `cp` through the documented detour via another source.

    `SetPlaybackControl stop` and `pause` answer cleanly and do nothing
    (WAM_PROTOCOL.md, "Leaving cp takes two commands"). Only a round trip
    through another source moves the speaker - `aux`, because `bt` says
    "Bluetooth is ready" out loud. The switch is not instant, hence the polling.
    """
    call("UIC", "SetFunc", [("function", "aux", "str")])
    time.sleep(2)
    call("UIC", "SetFunc", [("function", "wifi", "str")])

    # Wait for the return to `wifi`, not merely for leaving `cp`. The detour goes
    # through `aux`, so "submode != cp" is already true halfway and would report
    # success with the speaker parked on line-in.
    deadline = time.monotonic() + budget
    while time.monotonic() < deadline:
        func = call("UIC", "GetFunc")
        function, submode = field(func, "function"), field(func, "submode")
        if function == "wifi" and submode != "cp":
            print(f"    back: function={function} submode={submode}")
            return
        time.sleep(2)
    print("    DID NOT return to wifi - check the speaker by hand")


def main() -> None:
    print(f"speaker: {SPEAKER}:{PORT}")

    show("GetFunc (before)", call("UIC", "GetFunc"))
    call("UIC", "SetVolume", [("volume", 3, "dec")])
    time.sleep(PAUSE)

    show("SetSelectRadio", call("CPM", "SetSelectRadio"))
    time.sleep(PAUSE)

    presets = read_presets()
    show("GetPresetList (raw)", presets)

    # What we are after: whether a <preset> node carries its own index next to contentid.
    nodes = re.findall(r"<preset\b.*?</preset>", presets, re.S)
    print(f"\n    presets in response: {len(nodes)}")
    if nodes:
        print("    first node in full:")
        print("    " + nodes[0][:500].replace("\n", "\n    "))
    ids = re.findall(r"<contentid>([^<]+)</contentid>", presets)
    kinds = re.findall(r"<kind>([^<]+)</kind>", presets)
    print(f"    contentid: {ids[:6]}")
    print(f"    kind     : {kinds[:6]}")

    if not ids:
        print("\nNo presets - the variants below would have nothing to play.")
        return

    kind = (kinds[0] if kinds else "speaker").strip().casefold()
    preset_type = 1 if kind == "speaker" else 0

    # From here the speaker may start playing, so cleanup goes in `finally`.
    # A Ctrl-C during the 25-second poll would otherwise leave the station
    # playing for good - `SetPlaybackControl stop` will not stop it, see leave_cp.
    try:
        # Variant A: what tunein.py does today - contentid as presetindex.
        time.sleep(PAUSE)
        show(
            f"A) SetPlayPreset presettype={preset_type} presetindex={ids[0]} (contentid)",
            call("CPM", "SetPlayPreset",
                 [("presettype", preset_type, "dec"), ("presetindex", int(ids[0]), "dec")]),
        )
        wait_for_play()

        # Variant B: position in the list.
        time.sleep(PAUSE)
        show(
            f"B) SetPlayPreset presettype={preset_type} presetindex=0 (list position)",
            call("CPM", "SetPlayPreset",
                 [("presettype", preset_type, "dec"), ("presetindex", 0, "dec")]),
        )
        wait_for_play()

        # Variant C: the second position, to tell "0 fails too" from "the index works".
        time.sleep(PAUSE)
        show(
            f"C) SetPlayPreset presettype={preset_type} presetindex=1",
            call("CPM", "SetPlayPreset",
                 [("presettype", preset_type, "dec"), ("presetindex", 1, "dec")]),
        )
        wait_for_play()
    finally:
        print("\n### leaving cp")
        leave_cp()
        show("GetFunc (after)", call("UIC", "GetFunc"))


if __name__ == "__main__":
    main()
