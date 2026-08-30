"""Crash-safe record of a speaker a PCM session currently holds.

The speaker only reaches its own idle power-down once every program talking
to it has let go (measured 2026-08-16: 33 minutes still lit after a session
that sent no release, against 17 minutes to dark after one that did). The
helper's normal teardown sends that release - but a process that loses its
PC's power, or is killed outright, runs none of its own cleanup code, so
nothing sends it. Recovery is one command, ``standby``; nothing sends that
either, because the side that would is gone.

This module gives some *other* process, running later, a way to notice.
Each PCM session writes a small file recording which speaker it holds while
it holds it, named by its own pid so a second session never collides with
the first. A clean exit removes its own file. A file whose pid is no longer
running is what abandonment looks like from the outside - the process that
would have removed it did not get the chance.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import secrets
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path

LOGGER = logging.getLogger(__name__)

LEASE_VERSION = 1


def default_lease_dir() -> Path:
    """Return the directory holding one file per live PCM session."""

    if configured := os.environ.get("WAMBRIDGE_LEASES"):
        return Path(configured).expanduser()
    if local_app_data := os.environ.get("LOCALAPPDATA"):
        return Path(local_app_data) / "WAMBridge" / "leases"
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return config_home / "wambridge" / "leases"


@dataclass(frozen=True, slots=True)
class Lease:
    """One PCM session's claim on a speaker."""

    path: Path
    pid: int
    speaker_ip: str
    speaker_port: int


def write_lease(
    speaker_ip: str,
    speaker_port: int,
    *,
    directory: Path | None = None,
) -> Lease:
    """Record that this process holds a playback session with this speaker.

    Named by pid: a crashed process's file survives it for the sweep to find,
    and a fresh process that happens to reuse the same pid overwrites its
    predecessor's file rather than piling up beside it.

    Written to a temporary file and published with an atomic rename, so a
    concurrent sweep's ``find_stale_leases`` never observes a half-written
    file - it either sees the complete previous lease or the complete new
    one, never a torn read it would otherwise delete as unreadable.
    """

    target_dir = Path(directory) if directory is not None else default_lease_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    pid = os.getpid()
    path = target_dir / f"{pid}.json"
    tmp_path = target_dir / f".tmp-{pid}-{secrets.token_hex(4)}"
    tmp_path.write_text(
        json.dumps(
            {
                "version": LEASE_VERSION,
                "pid": pid,
                "speaker_ip": speaker_ip,
                "speaker_port": speaker_port,
                "started_at": time.time(),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    os.replace(tmp_path, path)
    return Lease(path=path, pid=pid, speaker_ip=speaker_ip, speaker_port=speaker_port)


def remove_lease(lease: Lease) -> None:
    """Best-effort cleanup on a clean exit, or once recovery is confirmed.

    A file that fails to delete here just means the next sweep finds a lease
    for a pid that has already exited - indistinguishable from the crash this
    module exists for, and handled the same way.
    """

    try:
        lease.path.unlink()
    except OSError as error:
        LOGGER.warning("Could not remove lease %s: %s", lease.path, error)


RECOVERY_CLAIM_TIMEOUT_S = 60
"""How long a claimed lease may sit unresolved before another sweep retries it.

Generous headroom over ``standby(require_stop_confirmed=True)``'s bounded
worst case (three retries of a 3 s stop, then a 3 s mute, then a 2 s
verification - well under 30 s), so this only reclaims a claim whose own
recovering process has genuinely stopped attempting it, whether that is a
failed attempt (deliberately left claimed - see ``claim_lease``) or that
process dying mid-recovery, the same class of event this whole module exists
to survive.
"""


def claim_lease(lease: Lease) -> Lease | None:
    """Mark a lease as being recovered, so a concurrent sweep leaves it alone.

    Renames the file to a fresh ``<pid>.json.recovering-<random>`` name and
    stamps the current time on it. The destination name is always new - even
    when reclaiming an already-``.recovering`` lease past
    ``RECOVERY_CLAIM_TIMEOUT_S`` - so the rename's source path is never one
    two callers could both still see: whichever one no longer finds
    ``lease.path`` there loses the race here instead of running ``standby``
    against the same speaker at the same time. Renaming an existing
    ``.recovering`` file in place would not have that property, since
    "already claimed" and "reclaim me" would otherwise share one name.

    A failed recovery attempt does not call ``unclaim`` - there isn't one.
    It simply leaves the file claimed, which doubles as backoff: nothing
    reclaims it until ``RECOVERY_CLAIM_TIMEOUT_S`` has passed.

    Returns ``None`` if the file is already gone (removed by its own process
    after all, or claimed by another sweep between the scan and this call).
    """

    claimed_path = lease.path.with_name(f"{lease.pid}.json.recovering-{secrets.token_hex(4)}")
    try:
        os.replace(lease.path, claimed_path)
        os.utime(claimed_path, None)
    except OSError:
        return None
    return replace(lease, path=claimed_path)


def is_pid_alive(pid: int) -> bool:
    """Return whether a process with this pid is still running."""

    if sys.platform == "win32":
        return _is_pid_alive_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists and is owned by someone else - still alive.
        return True
    return True


def _is_pid_alive_windows(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    error_access_denied = 5
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = kernel32.OpenProcess
    open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    open_process.restype = wintypes.HANDLE
    handle = open_process(process_query_limited_information, False, pid)
    if not handle:
        # A denied handle still means a process exists to deny it for - the
        # POSIX branch above treats its own permission error the same way.
        # Foobar and its helper can run as different users on a shared
        # machine, and reading that case as "dead" would send standby to a
        # speaker another session is still legitimately using.
        return ctypes.get_last_error() == error_access_denied
    kernel32.CloseHandle(handle)
    return True


def _parse_lease(entry: Path) -> Lease | None:
    """Read one lease file, deleting and returning ``None`` if it is unreadable.

    Unreadable here means garbage, not absent: it names no speaker to
    recover, and leaving it behind would only fail the same way on every
    future sweep.
    """

    try:
        payload = json.loads(entry.read_text(encoding="utf-8"))
        pid = int(payload["pid"])
        speaker_ip = str(payload["speaker_ip"])
        speaker_port = int(payload["speaker_port"])
    except (OSError, ValueError, KeyError, TypeError) as error:
        LOGGER.warning("Removing unreadable lease %s: %s", entry, error)
        with contextlib.suppress(OSError):
            entry.unlink()
        return None
    return Lease(path=entry, pid=pid, speaker_ip=speaker_ip, speaker_port=speaker_port)


def find_stale_leases(*, directory: Path | None = None) -> list[Lease]:
    """Return leases whose owning process is no longer running.

    Also returns ``.recovering`` leases claimed longer ago than
    ``RECOVERY_CLAIM_TIMEOUT_S`` - the embedded pid there is always dead
    (a stale lease's original owner, by construction), so age since the
    claim is what actually distinguishes an abandoned claim from one still
    in progress.
    """

    target_dir = Path(directory) if directory is not None else default_lease_dir()
    stale: list[Lease] = []
    try:
        entries = sorted(target_dir.glob("*.json"))
    except OSError:
        return stale
    for entry in entries:
        lease = _parse_lease(entry)
        if lease is not None and not is_pid_alive(lease.pid):
            stale.append(lease)
    now = time.time()
    for entry in sorted(target_dir.glob("*.json.recovering-*")):
        try:
            age = now - entry.stat().st_mtime
        except OSError:
            continue
        if age < RECOVERY_CLAIM_TIMEOUT_S:
            continue
        lease = _parse_lease(entry)
        if lease is not None:
            stale.append(lease)
    return stale
