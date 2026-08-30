"""Tests for the crash-safe speaker lease used to recover an abandoned M5."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from wambridge.lease import (
    RECOVERY_CLAIM_TIMEOUT_S,
    Lease,
    claim_lease,
    default_lease_dir,
    find_stale_leases,
    is_pid_alive,
    remove_lease,
    write_lease,
)

HOME = "/tmp/home"


def _finished_pid() -> int:
    """Spawn a trivial subprocess, wait for it to exit, and return its pid."""

    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait()
    return process.pid


def cleared_environment(**values: str) -> dict[str, str]:
    """Return an environment holding only these values and a home directory.

    ``clear=True`` also drops the variables a home is resolved from. POSIX
    falls back to the password database, Windows has none and raises
    ``RuntimeError`` from ``Path.home()`` instead, so the tests keep one.
    """

    return {"HOME": HOME, "USERPROFILE": HOME, **values}


class DefaultLeaseDirTests(unittest.TestCase):
    def test_explicit_override_wins(self) -> None:
        with patch.dict(
            "os.environ",
            cleared_environment(
                WAMBRIDGE_LEASES="~/custom-leases",
                LOCALAPPDATA=r"C:\Users\x\AppData\Local",
            ),
            clear=True,
        ):
            self.assertEqual(default_lease_dir(), Path("~/custom-leases").expanduser())

    def test_windows_uses_local_app_data(self) -> None:
        with patch.dict(
            "os.environ",
            cleared_environment(LOCALAPPDATA="/tmp/LocalAppData"),
            clear=True,
        ):
            self.assertEqual(
                default_lease_dir(),
                Path("/tmp/LocalAppData") / "WAMBridge" / "leases",
            )

    def test_falls_back_to_xdg_config_home(self) -> None:
        with patch.dict(
            "os.environ",
            cleared_environment(XDG_CONFIG_HOME="/tmp/config"),
            clear=True,
        ):
            self.assertEqual(
                default_lease_dir(),
                Path("/tmp/config") / "wambridge" / "leases",
            )

    def test_falls_back_to_home_config(self) -> None:
        with patch.dict("os.environ", cleared_environment(), clear=True):
            self.assertEqual(
                default_lease_dir(),
                Path.home() / ".config" / "wambridge" / "leases",
            )


class WriteAndRemoveLeaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.directory = Path(self._tmp.name)

    def test_write_lease_records_speaker_and_pid(self) -> None:
        lease = write_lease("10.0.0.118", 55001, directory=self.directory)

        self.assertEqual(lease.speaker_ip, "10.0.0.118")
        self.assertEqual(lease.speaker_port, 55001)
        self.assertTrue(is_pid_alive(lease.pid))  # this test process itself
        payload = json.loads(lease.path.read_text(encoding="utf-8"))
        self.assertEqual(payload["speaker_ip"], "10.0.0.118")
        self.assertEqual(payload["speaker_port"], 55001)
        self.assertEqual(payload["pid"], lease.pid)

    def test_write_lease_creates_the_directory(self) -> None:
        nested = self.directory / "nested" / "leases"

        lease = write_lease("10.0.0.118", 55001, directory=nested)

        self.assertTrue(lease.path.exists())

    def test_remove_lease_deletes_the_file(self) -> None:
        lease = write_lease("10.0.0.118", 55001, directory=self.directory)

        remove_lease(lease)

        self.assertFalse(lease.path.exists())

    def test_remove_lease_is_safe_when_already_gone(self) -> None:
        lease = write_lease("10.0.0.118", 55001, directory=self.directory)
        lease.path.unlink()

        remove_lease(lease)  # must not raise

    def test_write_lease_leaves_no_temporary_file_behind(self) -> None:
        # The atomic publish (write to a temp name, then os.replace) is what
        # keeps a concurrent find_stale_leases from ever reading a half
        # written file - confirm the temp name doesn't linger afterward.
        write_lease("10.0.0.118", 55001, directory=self.directory)

        names = [p.name for p in self.directory.iterdir()]
        self.assertEqual(len(names), 1)
        self.assertFalse(names[0].startswith(".tmp-"))


class IsPidAliveTests(unittest.TestCase):
    def test_current_process_is_alive(self) -> None:
        self.assertTrue(is_pid_alive(os.getpid()))

    def test_a_finished_process_is_not_alive(self) -> None:
        # A real process rather than an assumed-free pid number: spawning and
        # waiting on it is what actually proves is_pid_alive tells the two
        # states apart, on whichever platform this runs on.
        self.assertFalse(is_pid_alive(_finished_pid()))


class ClaimLeaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.directory = Path(self._tmp.name)

    def test_claim_renames_to_a_fresh_recovering_name(self) -> None:
        lease = write_lease("10.0.0.118", 55001, directory=self.directory)

        claimed = claim_lease(lease)

        self.assertIsNotNone(claimed)
        assert claimed is not None  # narrow for the type checker
        self.assertTrue(claimed.path.name.startswith(f"{lease.pid}.json.recovering-"))
        self.assertFalse(lease.path.exists())
        self.assertTrue(claimed.path.exists())

    def test_reclaiming_an_expired_claim_moves_it_to_a_new_name(self) -> None:
        # A fresh name every claim, never a rename-in-place, is what makes two
        # concurrent reclaims of the same expired .recovering file mutually
        # exclusive: only one of them still finds the source path to rename
        # from (found in review - renaming in place let both callers succeed).
        lease = write_lease("10.0.0.118", 55001, directory=self.directory)
        claimed = claim_lease(lease)
        assert claimed is not None
        old_mtime = claimed.path.stat().st_mtime
        os.utime(claimed.path, (old_mtime - 120, old_mtime - 120))

        reclaimed = claim_lease(claimed)

        self.assertIsNotNone(reclaimed)
        assert reclaimed is not None
        self.assertNotEqual(reclaimed.path, claimed.path)
        self.assertFalse(claimed.path.exists())
        self.assertGreater(reclaimed.path.stat().st_mtime, old_mtime - 120)
        # The already-claimed source is gone by the time a second caller
        # would try the same rename - that FileNotFoundError is the mutex.
        self.assertIsNone(claim_lease(claimed))

    def test_claiming_a_vanished_lease_returns_none(self) -> None:
        lease = write_lease("10.0.0.118", 55001, directory=self.directory)
        lease.path.unlink()

        self.assertIsNone(claim_lease(lease))


class FindStaleLeasesTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.directory = Path(self._tmp.name)

    def test_missing_directory_is_not_an_error(self) -> None:
        self.assertEqual(find_stale_leases(directory=self.directory / "missing"), [])

    def test_a_live_process_lease_is_not_stale(self) -> None:
        write_lease("10.0.0.118", 55001, directory=self.directory)

        self.assertEqual(find_stale_leases(directory=self.directory), [])

    def test_a_dead_process_lease_is_stale(self) -> None:
        dead_pid = _finished_pid()
        (self.directory / f"{dead_pid}.json").write_text(
            json.dumps(
                {"version": 1, "pid": dead_pid, "speaker_ip": "10.0.0.118", "speaker_port": 55001}
            ),
            encoding="utf-8",
        )

        stale = find_stale_leases(directory=self.directory)

        self.assertEqual(
            stale,
            [
                Lease(
                    path=self.directory / f"{dead_pid}.json",
                    pid=dead_pid,
                    speaker_ip="10.0.0.118",
                    speaker_port=55001,
                )
            ],
        )

    def test_an_unreadable_lease_is_removed_and_excluded(self) -> None:
        broken = self.directory / "not-json.json"
        broken.write_text("not json", encoding="utf-8")

        stale = find_stale_leases(directory=self.directory)

        self.assertEqual(stale, [])
        self.assertFalse(broken.exists())

    def _recovering_lease(self, *, age_seconds: float) -> Path:
        dead_pid = _finished_pid()
        path = self.directory / f"{dead_pid}.json.recovering-cafef00d"
        path.write_text(
            json.dumps(
                {"version": 1, "pid": dead_pid, "speaker_ip": "10.0.0.118", "speaker_port": 55001}
            ),
            encoding="utf-8",
        )
        stamp = time.time() - age_seconds
        os.utime(path, (stamp, stamp))
        return path

    def test_a_freshly_claimed_lease_is_not_returned(self) -> None:
        self._recovering_lease(age_seconds=1)

        self.assertEqual(find_stale_leases(directory=self.directory), [])

    def test_a_claim_older_than_the_timeout_is_returned(self) -> None:
        path = self._recovering_lease(age_seconds=RECOVERY_CLAIM_TIMEOUT_S + 1)

        stale = find_stale_leases(directory=self.directory)

        self.assertEqual([lease.path for lease in stale], [path])


if __name__ == "__main__":
    unittest.main()
