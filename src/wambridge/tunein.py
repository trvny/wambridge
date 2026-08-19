"""Native TuneIn presets stored by Samsung WAM speakers."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import urlencode
from xml.etree import ElementTree

from .samsung import (
    DEFAULT_PORT,
    LOCAL_OPENER,
    WamApiError,
    WamResponse,
    request,
)

LOGGER = logging.getLogger("wambridge")

# A Tune.ashx answer is a short playlist. Anything larger is not one.
MAX_TUNE_RESPONSE_BYTES = 64 * 1024


@dataclass(frozen=True, slots=True)
class WamPreset:
    """One TuneIn preset stored by the Samsung speaker."""

    content_id: str
    title: str
    kind: str
    description: str | None = None
    media_id: str | None = None
    thumbnail: str | None = None

    @property
    def preset_type(self) -> int:
        """Return Samsung's numeric preset type."""
        if self.kind == "speaker":
            return 1
        if self.kind == "my":
            return 0
        raise WamApiError(f"Unsupported TuneIn preset kind: {self.kind!r}")

    @property
    def preset_index(self) -> int:
        """Return the numeric content ID required by SetPlayPreset."""
        try:
            return int(self.content_id)
        except ValueError as error:
            raise WamApiError(
                f"Invalid TuneIn preset content ID: {self.content_id!r}"
            ) from error


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _element_value(element: ElementTree.Element, name: str) -> str | None:
    """Return a named child or parameter value from one XML element."""
    for node in element.iter():
        node_name = node.get("name") or _local_name(node.tag)
        if node_name != name:
            continue
        value = node.get("val")
        if value is None and node.text:
            value = node.text.strip()
        if value:
            return value
    return None


# TuneIn resolves a station id to whatever the broadcaster serves right now.
# Asking for `mp3,aac` matters: without it the answer for many stations is an
# HLS playlist over HTTPS, and the speaker plays neither when a URL is handed
# to it directly. No partnerId or serial is needed - measured 2026-08-19, the
# bare id plus formats answers, while the id alone returns `#STATUS: 400`.
TUNEIN_TUNE_URL = "http://opml.radiotime.com/Tune.ashx"
TUNEIN_DIRECT_FORMATS = "mp3,aac"


def _read_text(url: str, timeout: float) -> str:
    with LOCAL_OPENER.open(url, timeout=timeout) as response:  # nosec B310
        return response.read(MAX_TUNE_RESPONSE_BYTES).decode("utf-8", errors="replace")


def _expand_pls(url: str, timeout: float) -> tuple[str, ...]:
    """Return the stream URLs inside a PLS playlist, or the URL unchanged.

    TuneIn answers for some stations with a `.pls` file rather than a stream.
    FFmpeg will not open one, so a caller that passes it straight through loses
    a station that works perfectly once the playlist is read - measured on
    Czwórka, whose `listen.pls` holds a single `File1=` that plays fine.

    A playlist that cannot be fetched or holds nothing usable falls back to the
    original URL, so this can only ever add candidates, never remove them.
    """
    if not url.lower().split("?", 1)[0].endswith(".pls"):
        return (url,)
    try:
        body = _read_text(url, timeout)
    except OSError as error:
        LOGGER.debug("Could not read PLS playlist %s: %s", url, error)
        return (url,)
    entries = [
        value.strip()
        for line in body.splitlines()
        if line.strip().lower().startswith("file")
        for _, _, value in [line.partition("=")]
        if value.strip().startswith(("http://", "https://"))
    ]
    return tuple(dict.fromkeys(entries)) or (url,)


def resolve_tunein_station(
    tunein_id: str,
    *,
    timeout: float = 10.0,
    formats: str = TUNEIN_DIRECT_FORMATS,
) -> tuple[str, ...]:
    """Return the stream URLs TuneIn currently offers for one station id.

    Runs on this machine, not on the speaker. Playlist entries that are HLS are
    dropped rather than returned, because a caller handing them straight to
    ``SetUrlPlayback`` gets silence, and one has previously wedged the control
    port. Returns an empty tuple when TuneIn answers with no usable stream, so
    a caller can fall through to its own static URLs.
    """
    query = urlencode({"id": tunein_id, "formats": formats})
    request_url = f"{TUNEIN_TUNE_URL}?{query}"
    try:
        with LOCAL_OPENER.open(  # nosec B310 - fixed http scheme, built above
            request_url,
            timeout=timeout,
        ) as response:
            body = response.read(MAX_TUNE_RESPONSE_BYTES).decode(
                "utf-8", errors="replace"
            )
    except OSError as error:
        raise WamApiError(
            f"Cannot reach TuneIn for station {tunein_id}: {error}"
        ) from error

    urls: list[str] = []
    for line in body.splitlines():
        candidate = line.strip()
        if not candidate or candidate.startswith("#"):
            continue
        if not candidate.startswith(("http://", "https://")):
            continue
        if ".m3u8" in candidate:
            LOGGER.debug("Skipping HLS variant for %s: %s", tunein_id, candidate)
            continue
        for expanded in _expand_pls(candidate, timeout):
            if ".m3u8" in expanded:
                continue
            if expanded not in urls:
                urls.append(expanded)
    return tuple(urls)


def select_tunein(
    speaker_ip: str,
    *,
    port: int = DEFAULT_PORT,
    timeout: float = 5.0,
) -> WamResponse:
    """Select Samsung's native TuneIn content provider."""
    return request(
        speaker_ip,
        "SetSelectRadio",
        port=port,
        timeout=timeout,
        api_type="CPM",
    )


def parse_tunein_presets(body: str) -> list[WamPreset]:
    """Parse repeated TuneIn preset nodes from a PresetList response."""
    try:
        root = ElementTree.fromstring(body)  # nosec B314 - local speaker XML
    except ElementTree.ParseError as error:
        raise WamApiError(
            f"Samsung WAM returned invalid preset XML: {body[:200]}"
        ) from error

    presets: list[WamPreset] = []
    for node in root.iter():
        if _local_name(node.tag) != "preset":
            continue
        content_id = _element_value(node, "contentid")
        title = _element_value(node, "title")
        kind = _element_value(node, "kind")
        if not content_id or not title or not kind:
            continue
        presets.append(
            WamPreset(
                content_id=content_id,
                title=title,
                kind=kind.casefold(),
                description=_element_value(node, "description"),
                media_id=_element_value(node, "mediaid"),
                thumbnail=_element_value(node, "thumbnail"),
            )
        )
    return presets


def get_tunein_presets(
    speaker_ip: str,
    *,
    start_index: int = 0,
    list_count: int = 100,
    port: int = DEFAULT_PORT,
    timeout: float = 10.0,
) -> list[WamPreset]:
    """Return TuneIn presets stored by the speaker or signed-in account."""
    if start_index < 0 or list_count < 1:
        raise ValueError("TuneIn preset range is invalid")
    select_tunein(speaker_ip, port=port, timeout=timeout)
    response = request(
        speaker_ip,
        "GetPresetList",
        [
            ("startindex", start_index, "dec"),
            ("listcount", list_count, "dec"),
        ],
        port=port,
        timeout=timeout,
        api_type="CPM",
    )
    return parse_tunein_presets(response.body)


def find_tunein_preset(
    presets: list[WamPreset],
    selector: str,
) -> WamPreset:
    """Find a TuneIn preset by content ID or exact title."""
    cleaned = selector.strip()
    for preset in presets:
        if preset.content_id == cleaned:
            return preset
    matches = [
        preset
        for preset in presets
        if preset.title.casefold() == cleaned.casefold()
    ]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise WamApiError(
            f"More than one TuneIn preset is named {selector!r}; use its ID"
        )
    raise WamApiError(f"No TuneIn preset matches {selector!r}")


def play_tunein_preset(
    speaker_ip: str,
    preset: WamPreset,
    *,
    port: int = DEFAULT_PORT,
    timeout: float = 25.0,
) -> WamResponse:
    """Start one native TuneIn preset."""
    select_tunein(speaker_ip, port=port, timeout=min(timeout, 10.0))
    return request(
        speaker_ip,
        "SetPlayPreset",
        [
            ("presettype", preset.preset_type, "dec"),
            ("presetindex", preset.preset_index, "dec"),
        ],
        port=port,
        timeout=timeout,
        api_type="CPM",
    )
