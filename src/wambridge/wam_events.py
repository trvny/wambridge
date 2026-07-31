"""Persistent Samsung WAM response and event stream diagnostics."""

from __future__ import annotations

import re
import socket
from dataclasses import dataclass, field
from time import monotonic
from urllib.parse import urlsplit

from .samsung import DEFAULT_PORT, build_api_url

_HEADER_END = b"\r\n\r\n"
_STATUS_RE = re.compile(rb"^HTTP/1\.[01]\s+(\d{3})\b", re.IGNORECASE)
_CONTENT_LENGTH_RE = re.compile(
    rb"^Content-Length\s*:\s*(\d+)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_METHOD_RE = re.compile(r"<method>\s*([^<]+?)\s*</method>", re.IGNORECASE | re.DOTALL)
_RESPONSE_RE = re.compile(r"<response\b([^>]*)>", re.IGNORECASE | re.DOTALL)
_ATTRIBUTE_RE = re.compile(r"([A-Za-z_][\w:.-]*)\s*=\s*(['\"])(.*?)\2", re.DOTALL)
_LEAF_RE = re.compile(
    r"<([A-Za-z_][\w.-]*)\b[^>]*>\s*"
    r"(?:<!\[CDATA\[)?([^<]*?)(?:\]\]>)?\s*</\1>",
    re.IGNORECASE | re.DOTALL,
)
_PARAMETER_RE = re.compile(
    r"<p\b[^>]*\bname=(['\"])(.*?)\1[^>]*\bval=(['\"])(.*?)\3[^>]*/>",
    re.IGNORECASE | re.DOTALL,
)


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
            message_end = header_end + len(_HEADER_END) + content_length
            if len(self._buffer) < message_end:
                break

            body_start = header_end + len(_HEADER_END)
            body = bytes(self._buffer[body_start:message_end])
            del self._buffer[:message_end]
            if status_match.group(1) == b"200" and body:
                bodies.append(body.decode("utf-8", errors="replace"))
        return bodies


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
    for name, value in _LEAF_RE.findall(body):
        normalized = value.strip()
        if normalized and name.casefold() not in {"method", "response"}:
            values[name] = normalized
    for _, name, _, value in _PARAMETER_RE.findall(body):
        if name and value and value != "empty":
            values[name] = value

    user_identifier = None
    for key, value in values.items():
        if key.casefold() == "user_identifier":
            user_identifier = value
            break

    error_code = None
    for key, value in response_attributes.items():
        if key.casefold() == "errcode":
            error_code = value
            break

    return WamEvent(
        method=method_match.group(1).strip() if method_match else None,
        result=response_attributes.get("result"),
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
    port: int = DEFAULT_PORT,
    api_type: str = "UIC",
) -> bytes:
    """Build the raw request shape used by Samsung Multiroom clients."""
    parsed = urlsplit(
        build_api_url(
            speaker_ip,
            method,
            port=port,
            api_type=api_type,
        )
    )
    target = parsed.path
    if parsed.query:
        target = f"{target}?{parsed.query}"
    return "\r\n".join(
        (
            f"GET {target} HTTP/1.1",
            f"Host: {speaker_ip}:{port}",
            f"mobileUUID: {client_uuid}",
            "mobileName: Wireless Audio",
            "mobileVersion: 1.0",
            "Connection: close",
            "",
            "",
        )
    ).encode("utf-8")


def send_probe(
    speaker_ip: str,
    client_uuid: str,
    *,
    port: int = DEFAULT_PORT,
    timeout: float = 5.0,
) -> None:
    """Send a harmless GetFunc command through a separate writer socket."""
    request = build_mobile_request(speaker_ip, client_uuid, port=port)
    with socket.create_connection((speaker_ip, port), timeout=timeout) as writer:
        writer.sendall(request)


def listen_events(
    speaker_ip: str,
    client_uuid: str,
    *,
    port: int = DEFAULT_PORT,
    timeout: float = 5.0,
    duration: float = 0.0,
):
    """Yield speaker responses and unsolicited events from a persistent socket."""
    parser = WamHttpStreamParser()
    deadline = monotonic() + duration if duration > 0 else None
    with socket.create_connection((speaker_ip, port), timeout=timeout) as listener:
        listener.settimeout(1.0)
        send_probe(
            speaker_ip,
            client_uuid,
            port=port,
            timeout=timeout,
        )
        while deadline is None or monotonic() < deadline:
            try:
                data = listener.recv(65536)
            except TimeoutError:
                continue
            if not data:
                raise WamEventError("Samsung WAM closed the persistent event connection")
            for body in parser.feed(data):
                yield parse_event(body)
