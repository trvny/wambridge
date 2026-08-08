"""Count local TCP connections still held against the speaker.

A leaked ``wambridge-pcm`` helper keeps both the persistent control socket and
the speaker's audio pull alive, and the speaker will not fall asleep on its own
while anything is attached. The speaker itself cannot answer this: ``MusicInfo``
was measured returning mixed and stale state, so it is not usable as proof that
nothing is streaming. The only trustworthy view is the local socket table.

Windows-only by construction, because that is where the component runs. Every
other platform reports ``None`` (unknown) rather than a wrong zero, so callers
can tell "nothing is attached" apart from "could not look".
"""

from __future__ import annotations

import ctypes
import socket
import sys
from ctypes import wintypes

_AF_INET = 2
_TCP_TABLE_OWNER_PID_ALL = 5
_MIB_TCP_STATE_ESTAB = 5
_ERROR_INSUFFICIENT_BUFFER = 122
_NO_ERROR = 0
# One growth retry covers the table changing size between the sizing call and
# the read; looping forever on a busy machine would be worse than reporting
# unknown.
_MAX_ATTEMPTS = 4


class _MIB_TCPROW_OWNER_PID(ctypes.Structure):
    _fields_ = [
        ("dwState", wintypes.DWORD),
        ("dwLocalAddr", wintypes.DWORD),
        ("dwLocalPort", wintypes.DWORD),
        ("dwRemoteAddr", wintypes.DWORD),
        ("dwRemotePort", wintypes.DWORD),
        ("dwOwningPid", wintypes.DWORD),
    ]


def _packed_address(speaker_ip: str) -> int | None:
    """Return the address in the byte order the socket table stores it in."""
    try:
        return int.from_bytes(socket.inet_aton(speaker_ip), sys.byteorder)
    except OSError:
        return None


def established_connections_to(speaker_ip: str) -> int | None:
    """Return how many local sockets are ESTABLISHED to the speaker.

    ``None`` means the socket table could not be read, which is not the same as
    zero and must never be reported as "nothing is attached".
    """
    if not sys.platform.startswith("win"):
        return None
    target = _packed_address(speaker_ip)
    if target is None:
        return None

    try:
        get_table = ctypes.windll.iphlpapi.GetExtendedTcpTable
    except (AttributeError, OSError):
        return None

    size = wintypes.DWORD(0)
    buffer = ctypes.create_string_buffer(0)
    for _ in range(_MAX_ATTEMPTS):
        result = get_table(
            buffer, ctypes.byref(size), False,
            _AF_INET, _TCP_TABLE_OWNER_PID_ALL, 0,
        )
        if result == _NO_ERROR:
            break
        if result != _ERROR_INSUFFICIENT_BUFFER:
            return None
        buffer = ctypes.create_string_buffer(size.value)
    else:
        return None

    if size.value < ctypes.sizeof(wintypes.DWORD):
        return None
    count = int.from_bytes(buffer.raw[: ctypes.sizeof(wintypes.DWORD)], sys.byteorder)

    row_size = ctypes.sizeof(_MIB_TCPROW_OWNER_PID)
    offset = ctypes.sizeof(wintypes.DWORD)
    held = 0
    for _ in range(count):
        if offset + row_size > size.value:
            break
        row = _MIB_TCPROW_OWNER_PID.from_buffer_copy(buffer.raw[offset : offset + row_size])
        offset += row_size
        if row.dwState == _MIB_TCP_STATE_ESTAB and row.dwRemoteAddr == target:
            held += 1
    return held
