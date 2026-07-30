"""Native TuneIn presets stored by Samsung WAM speakers."""

from __future__ import annotations

from dataclasses import dataclass
from xml.etree import ElementTree

from .samsung import DEFAULT_PORT, WamApiError, WamResponse, request


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
