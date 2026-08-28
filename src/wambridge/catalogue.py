"""Browsing and searching the speaker's own TuneIn catalogue.

This is the read-only half of the radio surface. It reaches stations the
speaker has never had saved as a preset, so nothing here needs the preset write
commands, which remain untried.

**It does not end in a URL the speaker can play, and this docstring said it did
until 2026-08-28.** ``station_url`` from ``GetStationData`` is a ``Tune.ashx``
playlist and ``SetUrlPlayback`` refuses it with ``ErrorEvent`` ``ng``. What a
caller wants from a listed station is its ``media_id``: that is the TuneIn id,
which this side resolves to a stream and then relays, the way every other radio
in this project reaches the speaker.

Three properties of the firmware shape the whole module and are easy to get
wrong:

* **The browse cursor lives in the speaker, not in this process.** There is no
  path in any request; every call is relative to wherever the speaker's cursor
  happens to be, and it survives the client that moved it. A fresh run that
  descends without normalising first lands somewhere unintended and gets back a
  level that looks empty. Call :func:`open_catalogue` before anything else.
* **``contentid`` is the index within the current page**, restarting at 0 on
  every level, so it is only meaningful against the page it came from. The
  stable identifier is ``mediaid``, the TuneIn station id.
* **The CPM subsystem wedges under a fast series of requests.** It first answers
  with ``totallistcount=0`` for levels that do have content, then goes silent
  for twenty to thirty seconds while UIC keeps answering normally. It recovers
  by itself, so an empty page is retried rather than believed.

Measured against the physical M5 on 2026-08-19 and again on 2026-08-26.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from xml.etree import ElementTree

from .stations import is_tunein_station_id
from .samsung import (
    DEFAULT_PORT,
    WamApiError,
    request,
)

LOGGER = logging.getLogger("wambridge")

DEFAULT_LIST_COUNT = 30

# Folders and stations are told apart by the `type` attribute on `menuitem`.
# Search adds a third: a heading such as "Artist: Trojka", which carries no
# `mediaid` and cannot be descended into.
ITEM_FOLDER = "0"
ITEM_STATION = "2"

# `<root>` names which of the two trees the cursor is in. They are separate
# roots and both report `isroot="1"`, so `isroot` alone cannot tell them apart.
BROWSE_ROOT = "Browse"
SEARCH_ROOT = "Search"


@dataclass(frozen=True, slots=True)
class RadioEntry:
    """One row of a catalogue page."""

    content_id: str
    """Index within *this page*. Not stable across levels or pages."""

    title: str
    item_type: str
    media_id: str | None = None
    description: str | None = None
    thumbnail: str | None = None

    @property
    def is_folder(self) -> bool:
        """Return whether descending into this entry is meaningful."""
        return self.item_type == ITEM_FOLDER

    @property
    def is_station(self) -> bool:
        """Return whether this entry names a station whose id can be resolved.

        It cannot yield a playable URL by itself; see the module docstring.

        ``item_type`` alone does not identify a station. Measured on the M5,
        2026-08-28: descending into a podcast programme reached through a search
        lists 50 episodes that are ``type="2"`` exactly like stations, carrying
        ``media_id`` such as ``t573501779``. Nothing here can resolve those, so
        the station id shape is part of the test.
        """
        return (
            self.item_type == ITEM_STATION
            and bool(self.media_id)
            and is_tunein_station_id(self.media_id)
        )

    @property
    def index(self) -> int:
        """Return ``content_id`` as the number the commands expect."""
        try:
            return int(self.content_id)
        except ValueError as error:
            raise WamApiError(
                f"Invalid catalogue content ID: {self.content_id!r}"
            ) from error


@dataclass(frozen=True, slots=True)
class RadioPage:
    """One page of one catalogue level."""

    category: str | None
    is_root: bool
    root: str | None = None
    """Which tree this level belongs to: ``Browse`` or ``Search``."""

    total: int | None = None
    start_index: int = 0
    entries: tuple[RadioEntry, ...] = ()

    @property
    def has_more(self) -> bool:
        """Return whether the level continues past this page."""
        if self.total is None:
            return False
        return self.start_index + len(self.entries) < self.total


@dataclass(frozen=True, slots=True)
class StationDetail:
    """What ``GetStationData`` returns for one station."""

    title: str | None
    station_url: str | None
    media_id: str | None = None
    description: str | None = None
    thumbnail: str | None = None


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _text(node: ElementTree.Element, name: str) -> str | None:
    child = node.find(name)
    if child is None or child.text is None:
        return None
    value = child.text.strip()
    return value or None


def _parse(body: str, what: str) -> ElementTree.Element:
    try:
        return ElementTree.fromstring(body)  # nosec B314 - local speaker XML
    except ElementTree.ParseError as error:
        raise WamApiError(
            f"Samsung WAM returned invalid {what} XML: {body[:200]}"
        ) from error


def parse_radio_page(body: str) -> RadioPage:
    """Parse one ``RadioList`` response into entries plus paging state."""
    root = _parse(body, "catalogue")

    category_node = None
    for node in root.iter():
        if _local_name(node.tag) == "category":
            category_node = node
            break

    category = None
    is_root = False
    if category_node is not None:
        category = (category_node.text or "").strip() or None
        is_root = category_node.get("isroot") == "1"

    total: int | None = None
    start_index = 0
    root_name: str | None = None
    for node in root.iter():
        name = _local_name(node.tag)
        text = (node.text or "").strip()
        if name == "totallistcount" and text.isdigit():
            total = int(text)
        elif name == "startindex" and text.isdigit():
            start_index = int(text)
        elif name == "root" and text:
            root_name = text

    entries: list[RadioEntry] = []
    for node in root.iter():
        if _local_name(node.tag) != "menuitem":
            continue
        title = _text(node, "title")
        content_id = _text(node, "contentid")
        if title is None or content_id is None:
            # A row without either cannot be shown or acted on. Skipping it
            # keeps one malformed entry from losing the whole page.
            LOGGER.debug("Skipping catalogue entry without title or contentid")
            continue
        entries.append(
            RadioEntry(
                content_id=content_id,
                title=title,
                item_type=node.get("type") or "?",
                media_id=_text(node, "mediaid"),
                description=_text(node, "description"),
                thumbnail=_text(node, "thumbnail"),
            )
        )

    return RadioPage(
        category=category,
        is_root=is_root,
        root=root_name,
        total=total,
        start_index=start_index,
        entries=tuple(entries),
    )


def parse_station_detail(body: str) -> StationDetail:
    """Parse a ``StationData`` response.

    ``stationurl`` carries the speaker's own TuneIn partner id and serial. It is
    a credential: log it and it ends up in a bug report.
    """
    root = _parse(body, "station")
    values: dict[str, str] = {}
    for node in root.iter():
        name = _local_name(node.tag)
        text = (node.text or "").strip()
        if text and name not in values:
            values[name] = text
    return StationDetail(
        title=values.get("title"),
        station_url=values.get("stationurl"),
        media_id=values.get("mediaid"),
        description=values.get("description"),
        thumbnail=values.get("thumbnail"),
    )


def _cpm(
    speaker_ip: str,
    method: str,
    arguments: list[tuple[str, str | int, str]] | None = None,
    *,
    port: int,
    timeout: float,
) -> str:
    return request(
        speaker_ip,
        method,
        arguments,
        port=port,
        timeout=timeout,
        api_type="CPM",
    ).body


def _paged(start_index: int, list_count: int) -> list[tuple[str, str | int, str]]:
    return [
        ("startindex", start_index, "dec"),
        ("listcount", list_count, "dec"),
    ]


def _fetch_page(
    speaker_ip: str,
    method: str,
    arguments: list[tuple[str, str | int, str]],
    *,
    port: int,
    timeout: float,
    attempts: int,
    settle: float,
) -> RadioPage:
    """Fetch one page, retrying the empty answer that means CPM is recovering.

    A level that really is empty - Favorites and Recents on a speaker nobody has
    used that way - looks identical to a wedged subsystem, so the last answer is
    returned as it stands once the attempts run out rather than raising.
    """
    page = RadioPage(category=None, is_root=False)
    for attempt in range(attempts):
        page = parse_radio_page(
            _cpm(speaker_ip, method, arguments, port=port, timeout=timeout)
        )
        if page.entries or page.total:
            return page
        if attempt + 1 < attempts:
            LOGGER.debug("Empty page from %s, retrying", method)
            time.sleep(settle)
    return page


def open_catalogue(
    speaker_ip: str,
    *,
    port: int = DEFAULT_PORT,
    timeout: float = 8.0,
    list_count: int = DEFAULT_LIST_COUNT,
    attempts: int = 5,
    settle: float = 1.0,
) -> RadioPage:
    """Select TuneIn and walk the speaker's cursor back to the catalogue root.

    Returns the root page. Raises when the cursor cannot be normalised, because
    every later call would then be relative to an unknown level and would report
    the wrong thing rather than fail.

    **A search leaves the cursor in a second tree, measured on the M5 on
    2026-08-26/27.** After one ``SearchQuery`` the root is ``Search`` rather
    than ``Browse``, and it also answers ``isroot="1"`` - so walking up lands on
    a root that is not the catalogue, and returning that page would label search
    results as the catalogue. Only ``BrowseMain`` crosses back. ``SetSelectRadio``,
    repeated ``GetUpperRadioList``, a descend-and-ascend round trip and
    ``SetCpService`` were each tried and each left the cursor in ``Search``;
    ``GetCpSubmenu`` is refused outright by this firmware (error 73).
    """
    request(
        speaker_ip,
        "SetSelectRadio",
        port=port,
        timeout=timeout,
        api_type="CPM",
    )
    time.sleep(settle)

    page = RadioPage(category=None, is_root=False)
    for attempt in range(attempts):
        try:
            page = parse_radio_page(
                _cpm(
                    speaker_ip,
                    "GetUpperRadioList",
                    _paged(0, list_count),
                    port=port,
                    timeout=timeout,
                )
            )
        except WamApiError:
            # A refusal here is the same recovering CPM the empty page below is,
            # and it deserves the same budget rather than ending the whole open.
            # Measured 2026-08-28: `RadioList (error 60)` came back three times
            # in a day, always shortly after another CPM state change, and never
            # reproduced on demand - the very next command answered normally.
            # Twice that afternoon it was mistaken for a stuck browse cursor.
            if attempt + 1 >= attempts:
                raise
            time.sleep(settle)
            continue
        # ``is_root`` alone is not enough to accept the answer. A recovering CPM
        # reports ``totallistcount=0`` for a level that does have content - the
        # trap ``_fetch_page`` already retries around - and the Browse root is
        # never genuinely empty, so an empty one there means ask again.
        #
        # A *foreign* root is accepted empty, and must be: a search that matched
        # nothing leaves the cursor on a legitimately empty ``Search`` root, and
        # only ``BrowseMain`` below crosses back out of it. Retrying that one
        # instead would strand the cursor in the search tree for good, failing
        # every later browse the same way.
        if page.is_root and (page.entries or _is_foreign_root(page)):
            break
        if attempt + 1 < attempts:
            time.sleep(settle)
    else:
        raise WamApiError(
            "Could not read the speaker's browse root; results from any "
            "other level would be misleading"
        )

    if _is_foreign_root(page):
        page = _leave_foreign_root(
            page,
            speaker_ip,
            port=port,
            timeout=timeout,
            list_count=list_count,
            settle=settle,
            attempts=attempts,
        )
    if not page.entries:
        raise WamApiError(
            "The speaker's browse root came back empty; results from any "
            "other level would be misleading"
        )
    return page


def _is_foreign_root(page: RadioPage) -> bool:
    """Return whether the cursor sits on a root that is not the catalogue."""
    return bool(page.root) and page.root != BROWSE_ROOT


def _leave_foreign_root(
    page: RadioPage,
    speaker_ip: str,
    *,
    port: int,
    timeout: float,
    list_count: int,
    settle: float,
    attempts: int,
) -> RadioPage:
    """Cross from the search tree back into the catalogue with ``BrowseMain``.

    The read after the crossing is retried like any other, because it is the
    first one aimed at the Browse root and can meet the transient empty answer a
    recovering CPM gives. Raising on that would make a healthy speaker look
    broken for the one path that has just spent its settle time crossing.
    """
    was = page.root
    _cpm(
        speaker_ip,
        "BrowseMain",
        _paged(0, list_count),
        port=port,
        timeout=timeout,
    )
    for _attempt in range(attempts):
        time.sleep(settle)
        page = parse_radio_page(
            _cpm(
                speaker_ip,
                "GetCurrentRadioList",
                _paged(0, list_count),
                port=port,
                timeout=timeout,
            )
        )
        if page.entries or page.root != BROWSE_ROOT:
            break
    if page.root != BROWSE_ROOT:
        raise WamApiError(
            f"The speaker's radio cursor is in the {was} tree and BrowseMain "
            f"did not return it to {BROWSE_ROOT} (it is now {page.root!r}); "
            "search again, or clear it from the Samsung app."
        )
    return page


def current_page(
    speaker_ip: str,
    *,
    start_index: int = 0,
    port: int = DEFAULT_PORT,
    timeout: float = 8.0,
    list_count: int = DEFAULT_LIST_COUNT,
    attempts: int = 3,
    settle: float = 2.0,
) -> RadioPage:
    """Return the level the speaker's cursor is on."""
    return _fetch_page(
        speaker_ip,
        "GetCurrentRadioList",
        _paged(start_index, list_count),
        port=port,
        timeout=timeout,
        attempts=attempts,
        settle=settle,
    )


def descend(
    speaker_ip: str,
    content_id: int,
    *,
    port: int = DEFAULT_PORT,
    timeout: float = 8.0,
    list_count: int = DEFAULT_LIST_COUNT,
    attempts: int = 3,
    settle: float = 2.0,
) -> RadioPage:
    """Move the cursor into one entry of the current page.

    Named ``Get`` by the firmware, which hides that it moves the cursor.
    """
    return _fetch_page(
        speaker_ip,
        "GetSelectRadioList",
        [("contentid", content_id, "dec"), *_paged(0, list_count)],
        port=port,
        timeout=timeout,
        attempts=attempts,
        settle=settle,
    )


def ascend(
    speaker_ip: str,
    *,
    port: int = DEFAULT_PORT,
    timeout: float = 8.0,
    list_count: int = DEFAULT_LIST_COUNT,
    attempts: int = 3,
    settle: float = 2.0,
) -> RadioPage:
    """Move the cursor back up one level."""
    return _fetch_page(
        speaker_ip,
        "GetUpperRadioList",
        _paged(0, list_count),
        port=port,
        timeout=timeout,
        attempts=attempts,
        settle=settle,
    )


def station_detail(
    speaker_ip: str,
    content_id: int,
    *,
    port: int = DEFAULT_PORT,
    timeout: float = 8.0,
) -> StationDetail:
    """Return the detail of one station on the current page.

    ``content_id`` is the entry's index on the page it was read from, so this
    only means anything while the cursor is still on that level.
    """
    return parse_station_detail(
        _cpm(
            speaker_ip,
            "GetStationData",
            [("selectitemid", content_id, "dec")],
            port=port,
            timeout=timeout,
        )
    )


def search(
    speaker_ip: str,
    query: str,
    *,
    start_index: int = 0,
    port: int = DEFAULT_PORT,
    timeout: float = 12.0,
    list_count: int = DEFAULT_LIST_COUNT,
) -> RadioPage:
    """Search the catalogue by name.

    Results are mixed: stations carry a ``mediaid`` that goes straight to
    ``Tune.ashx``, while headings such as "Artist: Trojka" do not and are not
    descendable. ``GetGenreStations`` is not implemented for this service and
    ``GlobalSearch`` searches signed-in providers, of which this speaker has
    none, so this is the whole of search.
    """
    if not query.strip():
        raise WamApiError("A catalogue search needs a non-empty query")
    return parse_radio_page(
        _cpm(
            speaker_ip,
            "SearchQuery",
            [("query", query, "str"), *_paged(start_index, list_count)],
            port=port,
            timeout=timeout,
        )
    )
