"""Browse-aware Samsung DMS facade for one local MP3."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlsplit
from xml.etree import ElementTree

from .dlna import DIDL_LITE, DUBLIN_CORE, MP3_PROTOCOL_INFO, UPNP_METADATA
from .dlna_server import CONTENT_DIRECTORY
from .dms_probe import DEFAULT_DMS_PORT, SamsungDmsServer

LOGGER = logging.getLogger(__name__)


class SamsungBrowseServer(SamsungDmsServer):
    """Present a standards-correct root folder and one numeric audio item."""

    def __init__(
        self,
        source: str | Path,
        *,
        bind: str = "0.0.0.0",  # nosec B104 - speaker must reach the LAN server
        port: int = DEFAULT_DMS_PORT,
    ) -> None:
        super().__init__(source, bind=bind, port=port)
        # Samsung's own Android DMS uses numeric MediaStore IDs for audio items.
        self.object_id = "1"
        self.last_browse_fields: dict[str, str] = {}

    @staticmethod
    def _register_didl_namespaces() -> None:
        ElementTree.register_namespace("", DIDL_LITE)
        ElementTree.register_namespace("dc", DUBLIN_CORE)
        ElementTree.register_namespace("upnp", UPNP_METADATA)

    def _empty_didl(self) -> str:
        self._register_didl_namespaces()
        root = ElementTree.Element(f"{{{DIDL_LITE}}}DIDL-Lite")
        return ElementTree.tostring(root, encoding="unicode")

    def _root_didl(self) -> str:
        self._register_didl_namespaces()
        root = ElementTree.Element(f"{{{DIDL_LITE}}}DIDL-Lite")
        container = ElementTree.SubElement(
            root,
            f"{{{DIDL_LITE}}}container",
            {
                "id": "0",
                "parentID": "-1",
                "restricted": "1",
                "searchable": "0",
                "childCount": "1",
            },
        )
        ElementTree.SubElement(container, f"{{{DUBLIN_CORE}}}title").text = (
            "WAM Bridge"
        )
        ElementTree.SubElement(container, f"{{{UPNP_METADATA}}}class").text = (
            "object.container.storageFolder"
        )
        return ElementTree.tostring(root, encoding="unicode")

    def _item_didl(self, host: str) -> str:
        self._register_didl_namespaces()
        root = ElementTree.Element(f"{{{DIDL_LITE}}}DIDL-Lite")
        item = ElementTree.SubElement(
            root,
            f"{{{DIDL_LITE}}}item",
            {"id": self.object_id, "parentID": "0", "restricted": "1"},
        )
        ElementTree.SubElement(item, f"{{{DUBLIN_CORE}}}title").text = (
            self.source.stem
        )
        modified = datetime.fromtimestamp(
            self.source.stat().st_mtime,
            tz=timezone.utc,
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        ElementTree.SubElement(item, f"{{{DUBLIN_CORE}}}date").text = modified
        ElementTree.SubElement(item, f"{{{UPNP_METADATA}}}artist").text = (
            "Unknown Artist"
        )
        ElementTree.SubElement(item, f"{{{UPNP_METADATA}}}album").text = (
            "WAM Bridge"
        )
        ElementTree.SubElement(item, f"{{{UPNP_METADATA}}}genre").text = "Unknown"
        ElementTree.SubElement(
            item,
            f"{{{UPNP_METADATA}}}originalTrackNumber",
        ).text = "1"
        ElementTree.SubElement(item, f"{{{UPNP_METADATA}}}class").text = (
            "object.item.audioItem.musicTrack"
        )
        attributes = {
            "protocolInfo": MP3_PROTOCOL_INFO,
            "size": str(self.size),
        }
        if self.duration:
            attributes["duration"] = self.duration
        ElementTree.SubElement(item, f"{{{DIDL_LITE}}}res", attributes).text = (
            self.url(host)
        )
        return ElementTree.tostring(root, encoding="unicode")

    @staticmethod
    def _soap_value(body: bytes, name: str) -> str:
        text = body.decode("utf-8", errors="ignore")
        match = re.search(
            rf"<(?:[A-Za-z_][\w.-]*:)?{re.escape(name)}\b[^>]*>"
            rf"(.*?)</(?:[A-Za-z_][\w.-]*:)?{re.escape(name)}>",
            text,
            flags=re.DOTALL,
        )
        return match.group(1).strip() if match else ""

    def _browse_result(
        self,
        host: str,
        body: bytes,
    ) -> tuple[str, int, int, dict[str, str]]:
        fields = {
            name: self._soap_value(body, name)
            for name in (
                "ObjectID",
                "BrowseFlag",
                "Filter",
                "StartingIndex",
                "RequestedCount",
                "SortCriteria",
            )
        }
        self.last_browse_fields = fields
        object_id = fields["ObjectID"] or "0"
        browse_flag = fields["BrowseFlag"] or "BrowseDirectChildren"
        try:
            starting_index = max(0, int(fields["StartingIndex"] or "0"))
        except ValueError:
            starting_index = 0
        try:
            requested_count = max(0, int(fields["RequestedCount"] or "0"))
        except ValueError:
            requested_count = 0

        if browse_flag == "BrowseMetadata" and object_id == "0":
            didl = self._root_didl()
            total = 1
        elif browse_flag == "BrowseMetadata" and object_id:
            # The object ID in the command and the server database can differ
            # across Samsung firmware generations. This server has one item.
            didl = self._item_didl(host)
            total = 1
        elif browse_flag == "BrowseDirectChildren" and object_id == "0":
            didl = self._item_didl(host)
            total = 1
        else:
            didl = self._empty_didl()
            total = 0

        returned = total
        if browse_flag == "BrowseDirectChildren" and starting_index > 0:
            returned = 0
        if requested_count and returned > requested_count:
            returned = requested_count
        if not returned:
            didl = self._empty_didl()
        return didl, returned, total, fields

    def _make_handler(self) -> type[BaseHTTPRequestHandler]:
        base_handler = super()._make_handler()
        owner = self

        class Handler(base_handler):
            def do_POST(self) -> None:  # noqa: N802
                path = urlsplit(self.path).path
                if path != "/DLNA/cdscontrol":
                    super().do_POST()
                    return

                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    length = 0
                body = self.rfile.read(max(0, length))
                action = owner._action_name(self, body)
                host = self.headers.get(
                    "Host",
                    f"127.0.0.1:{owner.port}",
                ).split(":", 1)[0]
                LOGGER.debug("Samsung DMS SOAP %s %s", path, action)

                if action == "Browse":
                    owner.browse_requested.set()
                    didl, returned, total, fields = owner._browse_result(host, body)
                    LOGGER.debug(
                        "Samsung DMS Browse: ObjectID=%r BrowseFlag=%r "
                        "Filter=%r StartingIndex=%r RequestedCount=%r "
                        "SortCriteria=%r -> returned=%s total=%s",
                        fields["ObjectID"],
                        fields["BrowseFlag"],
                        fields["Filter"],
                        fields["StartingIndex"],
                        fields["RequestedCount"],
                        fields["SortCriteria"],
                        returned,
                        total,
                    )
                    values = {
                        "Result": didl,
                        "NumberReturned": str(returned),
                        "TotalMatches": str(total),
                        "UpdateID": "1",
                    }
                elif action == "GetSystemUpdateID":
                    values = {"Id": "1"}
                elif action == "GetSearchCapabilities":
                    values = {"SearchCaps": ""}
                elif action == "GetSortCapabilities":
                    values = {"SortCaps": ""}
                else:
                    self.send_error(500, "Unsupported ContentDirectory action")
                    return

                payload = owner._soap_response(CONTENT_DIRECTORY, action, values)
                self.send_response(200)
                self.send_header("Content-Type", 'text/xml; charset="utf-8"')
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Connection", "close")
                self.send_header("EXT", "")
                self.end_headers()
                self.wfile.write(payload)

        return Handler
