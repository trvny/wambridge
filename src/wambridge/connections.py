"""Count local TCP sockets still attached to the speaker.

A leaked ``wambridge-pcm`` helper keeps both the persistent control socket and
the speaker's audio pull alive, and the M5 only reaches the idle state its own
power-down needs once everything has let go. Whether releasing is enough on its
own is unmeasured; a session that was killed rather than stopped is only the
leading suspect for the M5 staying lit. The speaker cannot answer this itself either: ``MusicInfo``
was measured returning mixed and stale state, so the local socket table is the
only trustworthy view.

``ESTABLISHED`` alone would miss the case this exists for. A hard-killed helper
has its sockets closed by the kernel at once, so they sit in ``FIN_WAIT`` or
``CLOSE_WAIT`` and never appear as established. Everything from ``SYN_SENT``
through ``LAST_ACK`` therefore counts as attached. ``TIME_WAIT`` does not: it is
the normal lingering state after an orderly close and nothing is held by it.

Two deliberate limits, both visible to the caller only as ``unknown`` or as a
count:

* Every local process counts, not just this component. A speaker held by the
  official Samsung app is still held, so that is the useful answer.
* IPv4 only. A hostname or IPv6 target cannot be matched against the table and
  reports ``None`` rather than a wrong zero.

Windows-only by construction, because that is where the component runs. Every
other platform reports ``None`` (unknown), so callers can tell "nothing is
attached" apart from "could not look".
"""

from __future__ import annotations

import ctypes
import socket
import sys
import time
from ctypes import wintypes

# How long a caller tearing a session down waits for sockets to fall away before
# reporting what it sees. Both the standby action and the PCM helper's teardown
# read the table right after closing their own sockets, and an orderly close is
# not instant.
STANDBY_RELEASE_TIMEOUT = 5.0
STANDBY_RELEASE_POLL = 0.5

_AF_INET = 2
_TCP_TABLE_OWNER_PID_ALL = 5
_ERROR_INSUFFICIENT_BUFFER = 122
_NO_ERROR = 0
# A few growth retries cover the table changing size between the sizing call and
# the read; looping forever on a busy machine would be worse than reporting
# unknown.
_MAX_ATTEMPTS = 4

# MIB_TCP_STATE values. CLOSED, LISTEN, TIME_WAIT and DELETE_TCB are absent on
# purpose: none of them holds the peer. TIME_WAIT in particular is what an
# orderly close leaves behind, and counting it would report a hold after every
# clean stop.
_ATTACHED_STATES = frozenset(
    {
        3,   # SYN_SENT
        4,   # SYN_RCVD
        5,   # ESTABLISHED
        6,   # FIN_WAIT1
        7,   # FIN_WAIT2
        8,   # CLOSE_WAIT
        9,   # CLOSING
        10,  # LAST_ACK
    }
)


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


def attached_connections_to(
    speaker_ip: str,
    *,
    ignore_pid: int | None = None,
) -> int | None:
    """Return how many local sockets are still attached to the speaker.

    Attached means any state from ``SYN_SENT`` through ``LAST_ACK``, so a
    half-closed socket left by a killed helper counts. ``None`` means the socket
    table could not be read, which is not the same as zero and must never be
    reported as "nothing is attached".

    ``ignore_pid`` drops the caller's own sockets. A process that has just
    closed its connections still owns them for a moment: measured against the
    M5 on 2026-08-15, a socket closed locally sat in ``FIN_WAIT`` for a further
    0.5 s to 1.5 s. Counting those made the reading both slow and wrong - the
    question this answers is whether anything *else* is holding the speaker
    after we let go, and the caller's own dying sockets are the one thing that
    is certainly not. Sockets of a killed process still count: their owner is
    gone, so its PID cannot match the caller's.
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
        if row.dwRemoteAddr != target or row.dwState not in _ATTACHED_STATES:
            continue
        if ignore_pid is not None and row.dwOwningPid == ignore_pid:
            continue
        held += 1
    return held


def wait_until_released(
    speaker_ip: str,
    *,
    timeout: float = STANDBY_RELEASE_TIMEOUT,
    poll: float = STANDBY_RELEASE_POLL,
    ignore_pid: int | None = None,
) -> int | None:
    """Wait for local sockets against the speaker to drop, and report the count.

    Returns ``None`` when the socket table could not be read. That is reported
    as unknown rather than as zero: claiming nothing is attached when it could
    not be checked is the failure this exists to prevent.

    ``ignore_pid`` is passed straight through, and a caller tearing its own
    session down wants it: without it this waits out its own closing sockets,
    which is time paid for a reading that was never in question.
    """
    deadline = time.monotonic() + timeout
    held = attached_connections_to(speaker_ip, ignore_pid=ignore_pid)
    while held is not None and held > 0 and time.monotonic() < deadline:
        time.sleep(poll)
        held = attached_connections_to(speaker_ip, ignore_pid=ignore_pid)
    return held
