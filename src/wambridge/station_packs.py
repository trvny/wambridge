"""Bundled station packs for quick local setup."""

from __future__ import annotations

from .stations import RadioStation, StationError

TOP3 = (
    RadioStation(
        alias="bbc1",
        url=(
            "https://as-hls-ww-live.akamaized.net/pool_01505109/live/ww/"
            "bbc_radio_one/bbc_radio_one.isml/"
            "bbc_radio_one-audio=320000.norewind.m3u8"
        ),
        fallback_urls=(
            "https://a.files.bbci.co.uk/ms6/live/"
            "3441A116-B12E-4D2F-ACA8-C1984642FA4B/audio/simulcast/hls/"
            "nonuk/audio_syndication_low_sbr_v1/aks/bbc_radio_one.m3u8",
        ),
    ),
    RadioStation(
        alias="trojka",
        url="http://41.dktr.pl:8000/trojka.ogg",
        fallback_urls=("http://41.dktr.pl:8000/trojka2.ogg",),
    ),
    RadioStation(
        alias="czworka",
        url="http://stream3.polskieradio.pl:8906/;stream",
        fallback_urls=("http://mp3.polskieradio.pl:8956/;",),
    ),
)

FAVORITES = TOP3 + (
    RadioStation(
        alias="radioparadise",
        url="http://stream.radioparadise.com/ogg-192m",
    ),
    RadioStation(
        alias="electroswing",
        url="https://streamer.radio.co/s2c3cc784b/listen",
        fallback_urls=("https://streamer.radio.co:80/s2c3cc784b/listen",),
    ),
    RadioStation(
        alias="bbc6",
        url=(
            "https://as-hls-ww-live.akamaized.net/pool_81827798/live/ww/"
            "bbc_6music/bbc_6music.isml/"
            "bbc_6music-audio=320000.norewind.m3u8"
        ),
    ),
    RadioStation(
        alias="radioplus",
        url="https://pl05.cdn.eurozet.pl/plu-gdn.mp3",
        fallback_urls=("https://ic2.smcdn.pl/4070-1.mp3",),
    ),
    RadioStation(
        alias="minimalmix",
        url="http://orion.shoutca.st:8750/",
    ),
    RadioStation(
        alias="radiozet",
        url="http://zet-net-01.cdn.eurozet.pl:8400/",
        fallback_urls=(
            "https://r.dcs.redcdn.pl/sc/o2/Eurozet/live/audio.livx",
        ),
    ),
    RadioStation(
        alias="rmfmaxxx",
        url="https://rs201-krk.rmfstream.pl/rmf_maxxx",
        fallback_urls=("http://195.150.20.7/rmf_maxxx",),
    ),
    RadioStation(
        alias="kaszebe",
        url="http://x.radiokaszebe.pl:9000/;",
        fallback_urls=("https://stream4.nadaje.com:10125/kaszebe128",),
    ),
    RadioStation(
        alias="cinemix",
        url="https://kathy.torontocast.com:1190/stream",
    ),
    RadioStation(
        alias="soundtrack",
        url="https://quincy.torontocast.com:2410/stream",
    ),
    RadioStation(
        alias="thedotradio",
        url="http://c16.radioboss.fm:8026/autodj",
    ),
    RadioStation(
        alias="promodj",
        url="http://radio.promodj.com/top100-192",
    ),
    RadioStation(
        alias="falloutfm5",
        url="http://fallout.fm:8000/falloutfm5.ogg",
    ),
    RadioStation(
        alias="streamingsoundtracks",
        url="http://lo4.streamingsoundtracks.com/;",
    ),
)

STATION_PACKS: dict[str, tuple[RadioStation, ...]] = {
    "favorites": FAVORITES,
    "top3": TOP3,
}


def station_pack_names() -> tuple[str, ...]:
    """Return bundled station-pack names."""
    return tuple(sorted(STATION_PACKS))


def get_station_pack(name: str) -> tuple[RadioStation, ...]:
    """Return a bundled station pack by case-insensitive name."""
    key = name.strip().casefold()
    try:
        return STATION_PACKS[key]
    except KeyError as error:
        available = ", ".join(station_pack_names())
        raise StationError(
            f"Unknown radio station pack {name!r}; available: {available}"
        ) from error
