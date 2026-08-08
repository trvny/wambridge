"""Persistent Samsung WAM response and event stream diagnostics."""

from __future__ import annotations

import re
import socket
import threading
from dataclasses import dataclass, field
from time import monotonic
from urllib.parse import urlsplit

from .samsung import DEFAULT_PORT, build_api_url, validate_speaker_address

MAX_MESSAGE_BYTES = 1024 * 1024
_HEADER_END = b"\r\n\r\n"
_STATUS_RE = re.compile(rb"^HTTP/1\.[01]\s+(\d{3})\b", re.IGNORECASE)
_CONTENT_LENGTH_RE = re.compile(
    rb"^Content-Length\s*:\s*(\d+)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_METHOD_RE = re.compile(r"<method>\s*([^<]+?)\s*</method>", re.IGNORECASE | re.DOTALL)
_RESPONSE_RE = re.compile(r"<response\b([^>]*)>", re.IGNORECASE | re.DOTALL)
_ATTRIBUTE_RE = re.compile(r"([A-Za-z_][\w:.-]*)\s*=\s*(['\"])(.*?)\2", re.DOTALL)
_CDATA_LEAF_RE = re.compile(
    r"<([A-Za-z_][\w.-]*)\b[^>]*>\s*<!\[CDATA\[(.*?)\]\]>\s*</\1>",
    re.IGNORECASE | re.DOTALL,
)
_TEXT_LEAF_RE = re.compile(
    r"<([A-Za-z_][\w.-]*)\b[^>]*>\s*([^<]*?)\s*</\1>",
    re.IGNORECASE | re.DOTALL,
)
_PARAMETER_RE = re.compile(
    r"<p\b[^>]*\bname=(['\"])(.*?)\1[^>]*\bval=(['\"])(.*?)\3[^>]*/>",
    re.IGNORECASE | re.DOTALL,
)
_HEADER_VALUE_RE = re.compile(r"\A[\x21-\x7e]+\Z")


class WamEventError(RuntimeError):
    """Raised when the persistent WAM event stream is malformed or closes."""


@dataclass(frozen=True, slots=True)
class WamEvent:
    """One decoded response or unsolicited event broadcast by the speaker."""

    method: str | None
    result: str | None
    user_identifier: str | None
    error_code: str | None
    values: dict[str, str] = field(default_factory=dict)
    body: str = ""

    @property
    def reported_error_code(self) -> str:
        """Return the error code in whichever spelling the firmware used.

        Measured M5 firmware reports the code as an attribute, as a nested
        ``errCode`` element and as a lowercase ``errcode`` element.
        """
        return (
            self.error_code
            or self.values.get("errCode")
            or self.values.get("errcode", "")
        )


class WamHttpStreamParser:
    """Incrementally split Samsung's adjacent HTTP responses by Content-Length."""

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, data: bytes) -> list[str]:
        """Append bytes and return every complete HTTP 200 response body."""
        self._buffer.extend(data)
        bodies: list[str] = []
        while self._buffer:
            status_start = self._buffer.find(b"HTTP/")
            if status_start < 0:
                if len(self._buffer) > 16:
                    del self._buffer[:-16]
                break
            if status_start:
                del self._buffer[:status_start]

            header_end = self._buffer.find(_HEADER_END)
            if header_end < 0:
                break
            header = bytes(self._buffer[:header_end])
            status_match = _STATUS_RE.search(header)
            length_match = _CONTENT_LENGTH_RE.search(header)
            if status_match is None or length_match is None:
                raise WamEventError("Samsung WAM sent HTTP without status or Content-Length")

            content_length = int(length_match.group(1))
            if content_length > MAX_MESSAGE_BYTES:
                raise WamEventError(
                    f"Samsung WAM announced {content_length} bytes, above the "
                    f"{MAX_MESSAGE_BYTES} byte limit"
                )
            message_end = header_end + len(_HEADER_END) + content_length
            if len(self._buffer) < message_end:
                break

            body_start = header_end + len(_HEADER_END)
            body = bytes(self._buffer[body_start:message_end])
            del self._buffer[:message_end]
            if status_match.group(1) == b"200" and body:
                bodies.append(body.decode("utf-8", errors="replace"))
        return bodies


def _store_leaf(values: dict[str, str], name: str, value: str) -> None:
    normalized = value.strip()
    if normalized and name.casefold() not in {"method", "response"}:
        values[name] = normalized


def parse_event(body: str) -> WamEvent:
    """Extract useful fields without trusting network XML entity expansion."""
    method_match = _METHOD_RE.search(body)
    response_match = _RESPONSE_RE.search(body)
    response_attributes = {
        name: value
        for name, _, value in _ATTRIBUTE_RE.findall(
            response_match.group(1) if response_match else ""
        )
    }

    values: dict[str, str] = {}
    for name, value in _CDATA_LEAF_RE.findall(body):
        _store_leaf(values, name, value)
    for name, value in _TEXT_LEAF_RE.findall(body):
        if name not in values:
            _store_leaf(values, name, value)
    for _, name, _, value in _PARAMETER_RE.findall(body):
        if name and value and value != "empty":
            values[name] = value

    user_identifier = None
    for key, value in values.items():
        if key.casefold() == "user_identifier":
            user_identifier = value
            break

    result = None
    error_code = None
    for key, value in response_attributes.items():
        normalized_key = key.casefold()
        if normalized_key == "result":
            result = value
        elif normalized_key == "errcode":
            error_code = value

    return WamEvent(
        method=method_match.group(1).strip() if method_match else None,
        result=result,
        user_identifier=user_identifier,
        error_code=error_code,
        values=values,
        body=body,
    )


def build_mobile_request(
    speaker_ip: str,
    client_uuid: str,
    *,
    method: str = "GetFunc",
    arguments: list[tuple[str, str | int, str]] | None = None,
    port: int = DEFAULT_PORT,
    api_type: str = "UIC",
    power_on: bool = False,
    keep_alive: bool = False,
) -> bytes:
    """Build the raw request shape used by Samsung Multiroom clients.

    The address and the client UUID become request-line and header values, so
    both are checked first: a line break in either would append headers or a
    second request to the speaker's control connection.
    """
    validate_speaker_address(speaker_ip, port)
    if not client_uuid or not _HEADER_VALUE_RE.match(client_uuid):
        raise ValueError(f"Invalid Samsung WAM client UUID: {client_uuid!r}")
    parsed = urlsplit(
        build_api_url(
            speaker_ip,
            method,
            arguments,
            port=port,
            api_type=api_type,
            power_on=power_on,
        )
    )
    target = parsed.path
    if parsed.query:
        target = f"{target}?{parsed.query}"
    connection = "keep-alive" if keep_alive else "close"
    return "\r\n".join(
        (
            f"GET {target} HTTP/1.1",
            f"Host: {speaker_ip}:{port}",
            f"mobileUUID: {client_uuid}",
            "mobileName: Wireless Audio",
            "mobileVersion: 1.0",
            f"Connection: {connection}",
            "",
            "",
        )
    ).encode("utf-8")


class WamEventConnection:
    """One persistent control connection used for commands and events."""

    def __init__(
        self,
        speaker_ip: str,
        client_uuid: str,
        *,
        port: int = DEFAULT_PORT,
        timeout: float = 5.0,
    ) -> None:
        self._speaker_ip = speaker_ip
        self._client_uuid = client_uuid
        self._port = port
        self._timeout = timeout
        self._socket: socket.socket | None = None
        self._parser = WamHttpStreamParser()
        self._send_lock = threading.Lock()

    def __enter__(self) -> WamEventConnection:
        self._socket = socket.create_connection(
            (self._speaker_ip, self._port),
            timeout=self._timeout,
        )
        self._socket.settimeout(1.0)
        self.send(method="GetFunc")
        return self

    def __exit__(self, *_exc: object) -> None:
        if self._socket is not None:
            self._socket.close()
            self._socket = None

    def send(
        self,
        *,
        method: str,
        arguments: list[tuple[str, str | int, str]] | None = None,
        api_type: str = "UIC",
        power_on: bool = False,
    ) -> None:
        """Send one command without opening a competing control socket."""
        request = build_mobile_request(
            self._speaker_ip,
            self._client_uuid,
            method=method,
            arguments=arguments,
            port=self._port,
            api_type=api_type,
            power_on=power_on,
            keep_alive=True,
        )
        with self._send_lock:
            if self._socket is None:
                raise WamEventError("Samsung WAM event connection is not open")
            self._socket.sendall(request)

    def events(
        self,
        *,
        duration: float = 0.0,
        stop: threading.Event | None = None,
    ):
        """Yield decoded responses and broadcasts from this connection."""
        deadline = monotonic() + duration if duration > 0 else None
        while deadline is None or monotonic() < deadline:
            if stop is not None and stop.is_set():
                return
            if self._socket is None:
                raise WamEventError("Samsung WAM event connection is not open")
            try:
                data = self._socket.recv(65536)
            except TimeoutError:
                continue
            if not data:
                raise WamEventError("Samsung WAM closed the persistent event connection")
            for body in self._parser.feed(data):
                yield parse_event(body)


def send_mobile_command(
    speaker_ip: str,
    client_uuid: str,
    *,
    method: str,
    arguments: list[tuple[str, str | int, str]] | None = None,
    port: int = DEFAULT_PORT,
    timeout: float = 5.0,
    api_type: str = "UIC",
    power_on: bool = False,
) -> None:
    """Send one identity-bearing command without waiting for a reply."""
    request = build_mobile_request(
        speaker_ip,
        client_uuid,
        method=method,
        arguments=arguments,
        port=port,
        api_type=api_type,
        power_on=power_on,
    )
    with socket.create_connection((speaker_ip, port), timeout=timeout) as writer:
        writer.sendall(request)


def send_probe(
    speaker_ip: str,
    client_uuid: str,
    *,
    port: int = DEFAULT_PORT,
    timeout: float = 5.0,
) -> None:
    """Send a harmless GetFunc command through a separate writer socket."""
    send_mobile_command(
        speaker_ip,
        client_uuid,
        method="GetFunc",
        port=port,
        timeout=timeout,
    )


def listen_events(
    speaker_ip: str,
    client_uuid: str,
    *,
    port: int = DEFAULT_PORT,
    timeout: float = 5.0,
    duration: float = 0.0,
    stop: threading.Event | None = None,
    ready: threading.Event | None = None,
):
    """Yield events while keeping commands and reads on one control socket."""
    with WamEventConnection(
        speaker_ip,
        client_uuid,
        port=port,
        timeout=timeout,
    ) as connection:
        if ready is not None:
            ready.set()
        yield from connection.events(duration=duration, stop=stop)
