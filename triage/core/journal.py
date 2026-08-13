"""The journal: an append-only, tamper-evident record of everything the session did.

This is the trust anchor. A faulty machine can lie about its own health, and an agent
can be wrong about what it concluded — so the operator needs an independent record that
is complete enough to reconstruct the session *and* to distrust the agent after the fact.

Three properties make it worth trusting:

* **Append-only in the database, not just by convention.** SQLite triggers raise on any
  UPDATE or DELETE against the entries table. Code that tries to rewrite history fails.
* **Hash-chained.** Each entry commits to the one before it, so removing or editing an
  entry breaks every hash after it and ``verify()`` says exactly where.
* **Credential-free.** Secrets registered with the journal are scrubbed from payloads
  before they are written, and value-bearing key names (password, token, ...) are
  redacted structurally. Credentials are supplied per session and never land here.

The optional JSONL mirror is for operators who want the record in something greppable
without opening the database.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .models import serialize, utc_now

GENESIS_HASH = "0" * 64

#: Keys whose values are redacted wholesale, wherever they appear in a payload.
_SECRET_KEY_PATTERN = re.compile(
    r"(password|passphrase|secret|token|api[_-]?key|private[_-]?key|credential)", re.I
)

REDACTED = "[REDACTED]"


class JournalKind(str):
    """Entry kinds. A plain string subclass so unlisted kinds still work."""

    SESSION_CREATED = "session_created"
    AUTHORIZATION_ASSERTED = "authorization_asserted"
    TRANSPORT_BOUND = "transport_bound"
    COMMAND = "command"
    COMMAND_REFUSED = "command_refused"
    OBSERVATION = "observation"
    FINDING = "finding"
    REMEDIATION_PROPOSED = "remediation_proposed"
    APPROVAL_DECISION = "approval_decision"
    SNAPSHOT = "snapshot"
    REMEDIATION_APPLIED = "remediation_applied"
    REMEDIATION_VERIFIED = "remediation_verified"
    ROLLBACK = "rollback"
    MODEL_TURN = "model_turn"
    REPORT = "report"
    ERROR = "error"
    NOTE = "note"


@dataclass(frozen=True)
class JournalEntry:
    seq: int
    session_id: str
    at: str
    kind: str
    payload: dict[str, Any]
    prev_hash: str
    entry_hash: str


_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id            TEXT PRIMARY KEY,
    created_at    TEXT NOT NULL,
    target        TEXT NOT NULL,
    transport     TEXT NOT NULL,
    capability    TEXT NOT NULL,
    dry_run       INTEGER NOT NULL DEFAULT 0,
    authorization TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS entries (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    at         TEXT NOT NULL,
    kind       TEXT NOT NULL,
    payload    TEXT NOT NULL,
    prev_hash  TEXT NOT NULL,
    entry_hash TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS entries_by_session ON entries (session_id, seq);

-- Append-only, enforced by the database rather than by discipline.
CREATE TRIGGER IF NOT EXISTS entries_no_update
BEFORE UPDATE ON entries
BEGIN
    SELECT RAISE(ABORT, 'triage journal is append-only: entries cannot be updated');
END;

CREATE TRIGGER IF NOT EXISTS entries_no_delete
BEFORE DELETE ON entries
BEGIN
    SELECT RAISE(ABORT, 'triage journal is append-only: entries cannot be deleted');
END;
"""


class Journal:
    def __init__(
        self,
        db_path: str | Path = "triage.sqlite",
        jsonl_path: str | Path | None = None,
        secrets: Iterable[str] = (),
    ) -> None:
        self.db_path = Path(db_path)
        self.jsonl_path = Path(jsonl_path) if jsonl_path else None
        self._secrets: set[str] = {s for s in secrets if s}
        self._lock = threading.Lock()

        if self.db_path.parent != Path(""):
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

        if self.jsonl_path:
            self.jsonl_path.parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------- secrets

    def register_secret(self, value: str | None) -> None:
        """Register a credential so it is scrubbed if it ever reaches a payload.

        Belt and braces: the design is that credentials never get passed to the journal
        at all. This makes an accidental pass-through non-fatal.
        """
        if value:
            self._secrets.add(value)

    # ------------------------------------------------------------------ recording

    def open_session(
        self,
        session_id: str,
        target: str,
        transport: str,
        capability: str,
        authorization: str,
        dry_run: bool = False,
    ) -> JournalEntry:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO sessions "
                "(id, created_at, target, transport, capability, dry_run, authorization) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    session_id,
                    utc_now(),
                    target,
                    transport,
                    capability,
                    int(dry_run),
                    self._scrub(authorization),
                ),
            )
            self._conn.commit()
        return self.record(
            session_id,
            JournalKind.SESSION_CREATED,
            target=target,
            transport=transport,
            capability=capability,
            dry_run=dry_run,
            authorization=authorization,
        )

    def record(self, session_id: str, kind: str, **payload: Any) -> JournalEntry:
        """Append one entry. This is the only way anything enters the journal."""
        clean = self._scrub(serialize(payload))
        body = json.dumps(clean, sort_keys=True, separators=(",", ":"), default=str)
        at = utc_now()

        with self._lock:
            prev_hash = self._tip_hash_locked()
            entry_hash = _hash(prev_hash, session_id, at, kind, body)
            cursor = self._conn.execute(
                "INSERT INTO entries (session_id, at, kind, payload, prev_hash, entry_hash) "
                "VALUES (?,?,?,?,?,?)",
                (session_id, at, kind, body, prev_hash, entry_hash),
            )
            self._conn.commit()
            seq = int(cursor.lastrowid or 0)

        entry = JournalEntry(seq, session_id, at, kind, clean, prev_hash, entry_hash)
        self._mirror(entry)
        return entry

    # -------------------------------------------------------------------- reading

    def entries(self, session_id: str | None = None) -> list[JournalEntry]:
        if session_id is None:
            rows = self._conn.execute("SELECT * FROM entries ORDER BY seq").fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM entries WHERE session_id = ? ORDER BY seq", (session_id,)
            ).fetchall()
        return [_row_to_entry(row) for row in rows]

    def sessions(self) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM sessions ORDER BY created_at DESC").fetchall()
        return [dict(row) for row in rows]

    def verify(self) -> tuple[bool, int | None, str]:
        """Recompute the chain. Returns (intact, first_bad_seq, explanation)."""
        prev = GENESIS_HASH
        for row in self._conn.execute("SELECT * FROM entries ORDER BY seq"):
            if row["prev_hash"] != prev:
                return (
                    False,
                    int(row["seq"]),
                    f"entry {row['seq']} does not follow the previous entry — an entry "
                    "before it was removed or reordered",
                )
            expected = _hash(prev, row["session_id"], row["at"], row["kind"], row["payload"])
            if expected != row["entry_hash"]:
                return (False, int(row["seq"]), f"entry {row['seq']} content does not match its hash")
            prev = row["entry_hash"]
        return (True, None, "journal chain intact")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -------------------------------------------------------------------- private

    def _tip_hash_locked(self) -> str:
        row = self._conn.execute("SELECT entry_hash FROM entries ORDER BY seq DESC LIMIT 1").fetchone()
        return row["entry_hash"] if row else GENESIS_HASH

    def _mirror(self, entry: JournalEntry) -> None:
        if not self.jsonl_path:
            return
        line = json.dumps(
            {
                "seq": entry.seq,
                "session_id": entry.session_id,
                "at": entry.at,
                "kind": entry.kind,
                "payload": entry.payload,
                "prev_hash": entry.prev_hash,
                "entry_hash": entry.entry_hash,
            },
            default=str,
        )
        with self.jsonl_path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def _scrub(self, value: Any) -> Any:
        """Remove registered secrets and redact secret-shaped keys, recursively."""
        if isinstance(value, str):
            for secret in self._secrets:
                if secret in value:
                    value = value.replace(secret, REDACTED)
            return value
        if isinstance(value, dict):
            return {
                key: (REDACTED if _SECRET_KEY_PATTERN.search(str(key)) else self._scrub(item))
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [self._scrub(item) for item in value]
        return value


def _hash(prev_hash: str, session_id: str, at: str, kind: str, body: str) -> str:
    digest = hashlib.sha256()
    digest.update(prev_hash.encode())
    digest.update(b"\x00")
    digest.update(session_id.encode())
    digest.update(b"\x00")
    digest.update(at.encode())
    digest.update(b"\x00")
    digest.update(kind.encode())
    digest.update(b"\x00")
    digest.update(body.encode())
    return digest.hexdigest()


def _row_to_entry(row: sqlite3.Row) -> JournalEntry:
    return JournalEntry(
        seq=int(row["seq"]),
        session_id=row["session_id"],
        at=row["at"],
        kind=row["kind"],
        payload=json.loads(row["payload"]),
        prev_hash=row["prev_hash"],
        entry_hash=row["entry_hash"],
    )
