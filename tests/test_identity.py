"""Tests for the stable client identity file."""

from __future__ import annotations

import json
import unittest
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from wambridge.identity import IDENTITY_VERSION, default_identity_path, load_client_uuid

HOME = "/tmp/home"


def cleared_environment(**values: str) -> dict[str, str]:
    """Return an environment holding only these values and a home directory.

    ``clear=True`` also drops the variables a home is resolved from. POSIX
    falls back to the password database, Windows has none and raises
    ``RuntimeError`` from ``Path.home()`` instead, so the tests keep one.
    """

    return {"HOME": HOME, "USERPROFILE": HOME, **values}


class DefaultIdentityPathTests(unittest.TestCase):
    def test_explicit_override_wins(self) -> None:
        with patch.dict(
            "os.environ",
            cleared_environment(
                WAMBRIDGE_IDENTITY="~/custom.json",
                LOCALAPPDATA=r"C:\Users\x\AppData\Local",
            ),
            clear=True,
        ):
            self.assertEqual(default_identity_path(), Path("~/custom.json").expanduser())

    def test_windows_uses_local_app_data(self) -> None:
        with patch.dict(
            "os.environ",
            cleared_environment(LOCALAPPDATA="/tmp/LocalAppData"),
            clear=True,
        ):
            self.assertEqual(
                default_identity_path(),
                Path("/tmp/LocalAppData") / "WAMBridge" / "identity.json",
            )

    def test_falls_back_to_xdg_config_home(self) -> None:
        with patch.dict(
            "os.environ",
            cleared_environment(XDG_CONFIG_HOME="/tmp/config"),
            clear=True,
        ):
            self.assertEqual(
                default_identity_path(),
                Path("/tmp/config") / "wambridge" / "identity.json",
            )

    def test_falls_back_to_home_config(self) -> None:
        with patch.dict("os.environ", cleared_environment(), clear=True):
            self.assertEqual(
                default_identity_path(),
                Path.home() / ".config" / "wambridge" / "identity.json",
            )


class LoadClientUuidTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.path = Path(self._tmp.name) / "nested" / "identity.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_creates_and_persists_a_uuid(self) -> None:
        created = load_client_uuid(self.path)

        payload = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(payload["version"], IDENTITY_VERSION)
        self.assertEqual(payload["client_uuid"], created)
        self.assertEqual(str(uuid.UUID(created)), created)

    def test_reuses_the_stored_uuid(self) -> None:
        # The firmware ties one UUID to a client; a new one per run breaks it.
        first = load_client_uuid(self.path)
        self.assertEqual(load_client_uuid(self.path), first)

    def test_prefixed_uuid_is_replaced(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_text(
            json.dumps({"version": 1, "client_uuid": "uuid:abc"}), encoding="utf-8"
        )

        loaded = load_client_uuid(self.path)

        self.assertFalse(loaded.startswith("uuid:"))
        self.assertEqual(
            json.loads(self.path.read_text(encoding="utf-8"))["client_uuid"], loaded
        )

    def test_unreadable_payloads_are_replaced(self) -> None:
        self.path.parent.mkdir(parents=True)
        for broken in ("not json", "{}", json.dumps({"client_uuid": ""})):
            with self.subTest(broken=broken):
                self.path.write_text(broken, encoding="utf-8")
                loaded = load_client_uuid(self.path)
                self.assertEqual(str(uuid.UUID(loaded)), loaded)

    def test_default_path_is_used_when_omitted(self) -> None:
        with patch.dict("os.environ", {"WAMBRIDGE_IDENTITY": str(self.path)}, clear=True):
            created = load_client_uuid()

        self.assertTrue(self.path.exists())
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["client_uuid"], created)


if __name__ == "__main__":
    unittest.main()
