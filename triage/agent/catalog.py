"""Loader for the classified command catalog.

The catalog itself is `catalog.json` — data, not logic, so an operator can audit the
read surface and extend it without touching code. This module parses it, and defines
the resolution order the gate applies to a single command entry.

Extend it at runtime by pointing ``TRIAGE_CATALOG`` at another JSON file with the same
shape; entries there override built-ins of the same name and add new ones.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.models import Classification

BUILTIN_CATALOG = Path(__file__).with_name("catalog.json")

#: Where a binary given by absolute path is allowed to live. A command like
#: /tmp/smartctl is not the smartctl in the catalog, so it is not treated as one.
ALLOWED_BIN_DIRS = frozenset(
    {"/bin", "/sbin", "/usr/bin", "/usr/sbin", "/usr/local/bin", "/usr/local/sbin"}
)


@dataclass(frozen=True)
class CommandEntry:
    """One classified binary.

    The gate resolves an invocation against this in a fixed order, most-restrictive
    first, so that adding a permissive field can never widen an existing entry past a
    write flag:

    1. any ``write_flags`` present            -> WRITE
    2. ``write_if_assignment`` and a NAME=VAL -> WRITE
    3. any ``blocked_flags`` present          -> UNKNOWN (refused, not mutating)
    4. any of the first two positionals in ``write_subcommands`` -> WRITE
    5. ``read_subcommands`` set and first positional is in it    -> READ
    6. no positionals, and a ``read_flags`` entry is present     -> READ
    7. no arguments at all and ``bare_classification`` is set    -> that
    8. otherwise                                                 -> ``classification``
    """

    name: str
    classification: Classification
    summary: str = ""
    note: str = ""
    requires_sudo: bool = False
    bare_classification: Classification | None = None
    read_subcommands: frozenset[str] = field(default_factory=frozenset)
    write_subcommands: frozenset[str] = field(default_factory=frozenset)
    read_flags: frozenset[str] = field(default_factory=frozenset)
    write_flags: frozenset[str] = field(default_factory=frozenset)
    blocked_flags: frozenset[str] = field(default_factory=frozenset)
    write_if_assignment: bool = False

    @property
    def is_read_capable(self) -> bool:
        """True if any invocation of this binary can be classified READ."""
        return (
            self.classification is Classification.READ
            or self.bare_classification is Classification.READ
            or bool(self.read_subcommands)
            or bool(self.read_flags)
        )

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> CommandEntry:
        name = raw.get("name")
        if not name:
            raise ValueError(f"catalog entry is missing 'name': {raw!r}")
        bare = raw.get("bare_classification")
        return cls(
            name=name,
            classification=Classification(raw.get("classification", "WRITE")),
            summary=raw.get("summary", ""),
            note=raw.get("note", ""),
            requires_sudo=bool(raw.get("requires_sudo", False)),
            bare_classification=Classification(bare) if bare else None,
            read_subcommands=frozenset(raw.get("read_subcommands", ())),
            write_subcommands=frozenset(raw.get("write_subcommands", ())),
            read_flags=frozenset(raw.get("read_flags", ())),
            write_flags=frozenset(raw.get("write_flags", ())),
            blocked_flags=frozenset(raw.get("blocked_flags", ())),
            write_if_assignment=bool(raw.get("write_if_assignment", False)),
        )


class Catalog:
    def __init__(self, entries: dict[str, CommandEntry], version: int = 1) -> None:
        self._entries = entries
        self.version = version

    @classmethod
    def load(cls, *paths: str | os.PathLike[str]) -> Catalog:
        """Load the built-in catalog, then layer on any override files given.

        With no arguments, honours ``TRIAGE_CATALOG`` from the environment.
        """
        sources: list[Path] = [BUILTIN_CATALOG]
        if paths:
            sources.extend(Path(p) for p in paths)
        elif override := os.environ.get("TRIAGE_CATALOG"):
            sources.append(Path(override))

        entries: dict[str, CommandEntry] = {}
        version = 1
        for source in sources:
            raw = json.loads(Path(source).read_text(encoding="utf-8"))
            version = int(raw.get("version", version))
            for item in raw.get("commands", []):
                entry = CommandEntry.from_dict(item)
                entries[entry.name] = entry
        return cls(entries, version=version)

    def lookup(self, name: str) -> CommandEntry | None:
        return self._entries.get(name)

    def __contains__(self, name: object) -> bool:
        return name in self._entries

    def __len__(self) -> int:
        return len(self._entries)

    def entries(self) -> list[CommandEntry]:
        return sorted(self._entries.values(), key=lambda e: e.name)

    def read_capable(self) -> list[CommandEntry]:
        """The diagnostics surface — every binary the agent can lean on for looking."""
        return [e for e in self.entries() if e.is_read_capable]

    def describe_read_surface(self) -> str:
        """A compact listing for the system prompt, so the model knows what it has."""
        lines = []
        for entry in self.read_capable():
            sudo = " [sudo]" if entry.requires_sudo else ""
            lines.append(f"- {entry.name}{sudo}: {entry.summary}")
        return "\n".join(lines)


_default: Catalog | None = None


def default_catalog() -> Catalog:
    """Process-wide catalog, loaded once."""
    global _default
    if _default is None:
        _default = Catalog.load()
    return _default
