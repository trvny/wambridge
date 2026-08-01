"""Ostatni znany rozjazd: playertype. Kod wysyla 'allshare', zmierzylismy 'myphone'.

Wszystko inne ustawione na formy juz potwierdzone eksperymentalnie:
surowy device_udn + serwowanie pod /DLNA/<objectid>.
Zmieniamy JEDNO pole.
"""

from __future__ import annotations

import threading
import time

import probe_share as ps


def share_as(playertype: str, sourcename: str) -> str:
    return ps.send(
        '<name>SetSharePlaybackControl</name>'
        '<p type="str" name="playbackcontrol" val="play"/>'
        f'<p type="str" name="playertype" val="{playertype}"/>'
        f'<p type="cdata" name="sourcename" val="empty"><![CDATA[{sourcename}]]></p>'
        '<p type="dec" name="playtime" val="0"/>'
        f'<p type="str" name="device_udn" val="{ps.CLIENT_UUID}"/>'
        f'<p type="str" name="objectid" val="{ps.OBJECT_NAME}"/>'
    )


def main() -> None:
    print(f"klient UUID : {ps.CLIENT_UUID}")
    print(f"DMS         : http://{ps.HOST_IP}:{ps.DMS_PORT}/DLNA/{ps.OBJECT_NAME}\n")

    srv = ps.Server(("0.0.0.0", ps.DMS_PORT), ps.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    phase = {"name": "init"}
    threading.Thread(target=ps.listener, args=(phase,), daemon=True).start()
    time.sleep(1.5)
    ps.send('<name>SetVolume</name><p type="dec" name="volume" val="3"/>')
    print("glosnosc 3/30 (raz)\n")

    results = []
    for playertype, sourcename in (("myphone", "phone"), ("allshare", "WAMBridge")):
        label = f"playertype={playertype} sourcename={sourcename}"
        print(f"\n=== {label} ===")
        phase["name"] = label
        h0, e0 = len(ps.http_hits), len(ps.events)
        ps.register()
        time.sleep(0.5)
        resp = share_as(playertype, sourcename)
        err = ps.field(resp, "errCode") or ps.field(resp, "errcode") or "-"
        print(f"    odpowiedz: {ps.field(resp, 'method') or '(brak)'} err={err}")
        time.sleep(18)
        results.append(ps.phase_report(label, h0, e0))
        ps.send('<name>SetPlaybackControl</name>'
                '<p type="str" name="playbackcontrol" val="pause"/>')
        time.sleep(1)

    ps._stop.set()
    srv.shutdown()

    print("\n" + "=" * 66)
    for r in results:
        print(f"  {r['label']:<44} HTTP={len(r['http']):<3} {r['events'] or '-'}")
    ps.send('<name>SetVolume</name><p type="dec" name="volume" val="4"/>')
    print("\nglosnosc przywrocona do 4/30")


if __name__ == "__main__":
    main()
