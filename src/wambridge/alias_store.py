"""Alias-keyed JSON configuration shared by saved devices and radio stations."""

from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from collections.abc import Iterable
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, ClassVar, Protocol

CONFIG_VERSION = 1


def default_config_path(env_var: str, filename: str) -> Path:
    """Return the per-user path of one WAM Bridge configuration file."""
    if configured := os.environ.get(env_var):
        return Path(configured).expanduser()
    if local_app_data := os.environ.get("LOCALAPPDATA"):
        return Path(local_app_data) / "WAMBridge" / filename
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return config_home / "wambridge" / filename


class AliasedEntry(Protocol):
    """One stored item addressed by a case-insensitive alias."""

    @property
    def alias(self) -> str: ...

    def to_dict(self) -> dict[str, Any]: ...


class AliasStore[EntryT: AliasedEntry](ABC):
    """Read and atomically update one alias-keyed JSON configuration file.

    Subclasses supply the exception type, the JSON collection name and the
    wording used in messages. Everything else - alias keying, version checking
    and the temporary-file replace - is identical for every stored kind.
    """

    error: ClassVar[type[RuntimeError]]
    collection: ClassVar[str]
    entry_label: ClassVar[str]
    """Singular wording for the file and its entries, e.g. ``WAM profile``."""
    plural_label: ClassVar[str]
    """Wording for whole-file failures, e.g. ``WAM profiles``."""
    subject_label: ClassVar[str]
    """Wording for a missing item, e.g. ``WAM device``."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or self.default_path()

    @classmethod
    @abstractmethod
    def default_path(cls) -> Path:
        """Return the per-user file used when no override is given."""

    @classmethod
    @abstractmethod
    def parse(cls, value: dict[str, Any]) -> EntryT:
        """Validate and construct one entry loaded from JSON."""

    @classmethod
    def validated(cls, entry: EntryT) -> EntryT:
        """Return the entry as it should be stored."""
        return entry

    @classmethod
    def alias_key(cls, alias: str) -> str:
        """Return the case-insensitive key of an alias."""
        key = alias.strip().casefold()
        if not key:
            subject = cls.subject_label
            raise cls.error(f"{subject[:1].upper()}{subject[1:]} alias cannot be empty")
        return key

    def load(self) -> dict[str, EntryT]:
        """Load entries indexed by a case-insensitive alias."""
        if not self.path.exists():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise self.error(
                f"Cannot read {self.plural_label} from {self.path}: {error}"
            ) from error
        if not isinstance(payload, dict):
            raise self.error(f"Unsupported {self.entry_label} file: {self.path}")
        if payload.get("version") != CONFIG_VERSION or not isinstance(
            payload.get(self.collection), list
        ):
            raise self.error(f"Unsupported {self.entry_label} file: {self.path}")

        entries: dict[str, EntryT] = {}
        for raw_entry in payload[self.collection]:
            if not isinstance(raw_entry, dict):
                raise self.error(f"Invalid {self.entry_label} entry in {self.path}")
            entry = self.parse(raw_entry)
            entries[self.alias_key(entry.alias)] = entry
        return entries

    def all(self) -> list[EntryT]:
        """Return saved entries ordered by alias."""
        return sorted(self.load().values(), key=lambda entry: entry.alias.casefold())

    def get(self, alias: str) -> EntryT:
        """Return one saved entry."""
        entry = self.load().get(self.alias_key(alias))
        if entry is None:
            raise self.error(f"No saved {self.subject_label} named {alias!r}")
        return entry

    def put(self, entry: EntryT) -> None:
        """Create or replace one saved entry."""
        self.put_many([entry])

    def put_many(self, new_entries: Iterable[EntryT]) -> list[EntryT]:
        """Create or replace several entries in one atomic update."""
        validated = [self.validated(entry) for entry in new_entries]
        entries = self.load()
        for entry in validated:
            entries[self.alias_key(entry.alias)] = entry
        self._save(entries.values())
        return validated

    def remove(self, alias: str) -> EntryT:
        """Delete and return one saved entry."""
        entries = self.load()
        try:
            removed = entries.pop(self.alias_key(alias))
        except KeyError as error:
            raise self.error(f"No saved {self.subject_label} named {alias!r}") from error
        self._save(entries.values())
        return removed

    def _save(self, entries: Iterable[EntryT]) -> None:
        payload = {
            "version": CONFIG_VERSION,
            self.collection: [
                entry.to_dict()
                for entry in sorted(entries, key=lambda item: item.alias.casefold())
            ],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with NamedTemporaryFile(
                "w",
                encoding="utf-8",
                newline="\n",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                json.dump(payload, temporary, ensure_ascii=False, indent=2)
                temporary.write("\n")
                temporary_path = Path(temporary.name)
            os.replace(temporary_path, self.path)
        except OSError as error:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise self.error(
                f"Cannot save {self.plural_label} to {self.path}: {error}"
            ) from error
