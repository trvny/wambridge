"""Stable client identity for the Samsung WAM control protocol.

The speaker ties one UUID to a client across ``mobileUUID`` headers,
``SetIpInfo`` registration, ``device_udn`` and the ``user_identifier`` echoed in
every response. Generating a fresh identity per command breaks the relationship
the firmware expects, so it is stored next to the device profiles.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

IDENTITY_VERSION = 1


def default_identity_path() -> Path:
    """Return the file holding this installation's client UUID."""

    if configured := os.environ.get("WAMBRIDGE_IDENTITY"):
        return Path(configured).expanduser()
    if local_app_data := os.environ.get("LOCALAPPDATA"):
        return Path(local_app_data) / "WAMBridge" / "identity.json"
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return config_home / "wambridge" / "identity.json"


def load_client_uuid(path: Path | None = None) -> str:
    """Return the stored client UUID, creating one on first use."""

    target = Path(path) if path is not None else default_identity_path()
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
        stored = payload["client_uuid"]
        if isinstance(stored, str) and stored and not stored.startswith("uuid:"):
            return stored
    except (OSError, ValueError, KeyError, TypeError):
        pass

    created = str(uuid.uuid4())
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps({"version": IDENTITY_VERSION, "client_uuid": created}, indent=2),
        encoding="utf-8",
    )
    return created
