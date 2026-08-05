"""Stable client identity for the Samsung WAM control protocol.

The speaker ties one UUID to a client across ``mobileUUID`` headers,
``SetIpInfo`` registration, ``device_udn`` and the ``user_identifier`` echoed in
every response. Generating a fresh identity per command breaks the relationship
the firmware expects, so it is stored next to the device profiles.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from pathlib import Path

LOGGER = logging.getLogger(__name__)

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
        raw = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        # First use on this installation; fall through and create one.
        raw = None
    except OSError as error:
        # A permission or I/O error is not first use: regenerating the UUID
        # here would silently break the identity the firmware ties to this
        # client, so make the failure visible.
        LOGGER.warning("Cannot read client identity from %s: %s", target, error)
        raw = None
    if raw is not None:
        try:
            payload = json.loads(raw)
            stored = payload["client_uuid"]
        except (ValueError, KeyError, TypeError) as error:
            LOGGER.warning(
                "Ignoring unreadable client identity in %s and generating a new "
                "one: %s",
                target,
                error,
            )
        else:
            if isinstance(stored, str) and stored and not stored.startswith("uuid:"):
                return stored
            LOGGER.warning(
                "Stored client identity in %s is not usable (%r); generating a "
                "new one",
                target,
                stored,
            )

    created = str(uuid.uuid4())
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps({"version": IDENTITY_VERSION, "client_uuid": created}, indent=2),
        encoding="utf-8",
    )
    return created
