"""The journal is the trust anchor, so what matters is that it cannot be quietly edited.

These tests attack it: rewrite an entry through the normal path, rewrite it behind the
triggers' back, and try to smuggle a credential in.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from triage.core.journal import Journal, JournalKind


def test_entries_cannot_be_updated_or_deleted(journal: Journal) -> None:
    journal.open_session("s1", "box", "mock", "EXECUTE_RW", "I own it")
    journal.record("s1", JournalKind.COMMAND, command="df -h")

    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        journal._conn.execute("UPDATE entries SET payload = '{}' WHERE seq = 2")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        journal._conn.execute("DELETE FROM entries WHERE seq = 2")

    assert len(journal.entries("s1")) == 2


def test_tampering_that_bypasses_the_triggers_is_still_detected(journal: Journal) -> None:
    """Append-only is enforced by the database; the hash chain survives losing it."""
    journal.open_session("s1", "box", "mock", "EXECUTE_RW", "I own it")
    journal.record("s1", JournalKind.COMMAND, command="df -h")
    journal.record("s1", JournalKind.FINDING, symptom="/var full")
    assert journal.verify()[0]

    path = journal.db_path
    journal.close()

    conn = sqlite3.connect(str(path))
    conn.execute("DROP TRIGGER entries_no_update")
    conn.execute("UPDATE entries SET payload = '{\"command\": \"rm -rf /\"}' WHERE seq = 2")
    conn.commit()
    conn.close()

    intact, bad_seq, note = Journal(path).verify()
    assert not intact
    assert bad_seq == 2
    assert "does not match its hash" in note


def test_removing_an_entry_breaks_the_chain(journal: Journal) -> None:
    journal.open_session("s1", "box", "mock", "EXECUTE_RW", "I own it")
    for i in range(4):
        journal.record("s1", JournalKind.COMMAND, command=f"cmd-{i}")
    path = journal.db_path
    journal.close()

    conn = sqlite3.connect(str(path))
    conn.execute("DROP TRIGGER entries_no_delete")
    conn.execute("DELETE FROM entries WHERE seq = 3")
    conn.commit()
    conn.close()

    intact, bad_seq, note = Journal(path).verify()
    assert not intact
    assert bad_seq == 4
    assert "removed or reordered" in note


def test_registered_secrets_are_scrubbed_everywhere_they_appear(journal: Journal) -> None:
    journal.register_secret("s3cret-passphrase")
    journal.record(
        "s1",
        JournalKind.NOTE,
        free_text="authenticated with s3cret-passphrase",
        nested={"deep": ["s3cret-passphrase", "fine"]},
    )
    payload = journal.entries("s1")[0].payload
    assert "s3cret-passphrase" not in json.dumps(payload)
    assert payload["nested"]["deep"][1] == "fine"


@pytest.mark.parametrize(
    "key", ["password", "passphrase", "api_key", "API-KEY", "token", "private_key", "credential"]
)
def test_secret_shaped_keys_are_redacted_even_when_unregistered(
    journal: Journal, key: str
) -> None:
    """Defence in depth: a credential that was never registered still must not land here."""
    journal.record("s1", JournalKind.NOTE, **{key: "never-seen-before-value"})
    assert journal.entries("s1")[0].payload[key] == "[REDACTED]"


def test_jsonl_mirror_matches_the_database(journal: Journal) -> None:
    journal.open_session("s1", "box", "mock", "EXECUTE_RW", "I own it")
    journal.record("s1", JournalKind.FINDING, symptom="/var full")

    lines = [json.loads(l) for l in journal.jsonl_path.read_text().splitlines()]
    assert [l["kind"] for l in lines] == [e.kind for e in journal.entries("s1")]
    assert [l["entry_hash"] for l in lines] == [e.entry_hash for e in journal.entries("s1")]


def test_sessions_are_listed_with_their_mode(journal: Journal) -> None:
    journal.open_session("s1", "box-a", "ssh", "EXECUTE_RW", "I own it", dry_run=True)
    journal.open_session("s2", "box-b", "mock", "EXECUTE_RO", "I own it too")
    rows = {r["id"]: r for r in journal.sessions()}
    assert rows["s1"]["dry_run"] == 1
    assert rows["s2"]["capability"] == "EXECUTE_RO"
