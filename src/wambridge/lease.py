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
import sys
import time
from dataclasses import dataclass
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
    """

    target_dir = Path(directory) if directory is not None else default_lease_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    pid = os.getpid()
    path = target_dir / f"{pid}.json"
    path.write_text(
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
    return Lease(path=path, pid=pid, speaker_ip=speaker_ip, speaker_port=speaker_port)


def remove_lease(lease: Lease) -> None:
    """Best-effort cleanup on a clean exit.

    A file that fails to delete here just means the next sweep finds a lease
    for a pid that has already exited - indistinguishable from the crash this
    module exists for, and handled the same way.
    """

    try:
        lease.path.unlink()
    except OSError as error:
        LOGGER.warning("Could not remove lease %s: %s", lease.path, error)


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
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = kernel32.OpenProcess
    open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    open_process.restype = wintypes.HANDLE
    handle = open_process(process_query_limited_information, False, pid)
    if not handle:
        return False
    kernel32.CloseHandle(handle)
    return True


def find_stale_leases(*, directory: Path | None = None) -> list[Lease]:
    """Return leases whose owning process is no longer running.

    A lease file that cannot be parsed is removed rather than reported: it
    names no speaker to recover, and leaving it behind would only fail the
    same way on every future sweep.
    """

    target_dir = Path(directory) if directory is not None else default_lease_dir()
    stale: list[Lease] = []
    try:
        entries = sorted(target_dir.glob("*.json"))
    except OSError:
        return stale
    for entry in entries:
        try:
            payload = json.loads(entry.read_text(encoding="utf-8"))
            pid = int(payload["pid"])
            speaker_ip = str(payload["speaker_ip"])
            speaker_port = int(payload["speaker_port"])
        except (OSError, ValueError, KeyError, TypeError) as error:
            LOGGER.warning("Removing unreadable lease %s: %s", entry, error)
            with contextlib.suppress(OSError):
                entry.unlink()
            continue
        if not is_pid_alive(pid):
            stale.append(
                Lease(path=entry, pid=pid, speaker_ip=speaker_ip, speaker_port=speaker_port)
            )
    return stale
