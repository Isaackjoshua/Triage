"""Snapshot before mutate — filesystem detection, backups, and rollback.

The patient has no healthy backup. That is the assumption the whole system is built on,
and it is what makes this module load-bearing rather than a nicety: the difference between
a recoverable problem and a dead machine is often whether a rollback point existed before
someone ran the fix.

There is a ladder, and it is walked in order:

1. **Filesystem snapshot** (btrfs / ZFS) — cheap, whole-subvolume, genuinely reversible.
2. **File backup** — copy aside the specific files the command will touch, when the
   filesystem cannot snapshot but the blast radius is determinable.
3. **Nothing** — and say so. A `SnapshotPlan` with `kind == NONE` is what triggers the
   explicit second confirmation in the approval flow. It is never papered over.

`plan()` is read-only: it reports what protection is *available* so the human sees it
before deciding. `protect()` actually takes it, immediately before the write.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field

from ..core.journal import Journal, JournalKind
from ..core.models import Remediation, SnapshotKind, SnapshotRef
from ..transports.base import Transport

#: Commands whose file arguments are the thing being modified. Used to work out what to
#: back up when the filesystem cannot snapshot.
_FILE_MUTATORS = {
    "truncate", "tee", "sed", "cp", "mv", "rm", "chmod", "chown", "chattr", "ln", "touch",
    "dd",
}

#: Paths that are not real files on disk, or are far too broad to copy aside.
_UNBACKABLE = re.compile(r"^(/dev/|/proc/|/sys/|/run/|/tmp/?$|/$)")


@dataclass
class SnapshotPlan:
    """What protection is available for a proposed change, decided before approval."""

    kind: SnapshotKind
    scope: str
    detail: str
    paths: list[str] = field(default_factory=list)

    @property
    def has_rollback(self) -> bool:
        return self.kind not in (SnapshotKind.NONE, SnapshotKind.SIMULATED)

    def describe(self) -> str:
        if self.kind is SnapshotKind.NONE:
            return (
                "NO AUTOMATIC ROLLBACK AVAILABLE. If this command does the wrong thing, "
                "there is nothing to restore from."
            )
        if self.kind is SnapshotKind.SIMULATED:
            return "Dry run — the snapshot and the change are both simulated."
        return f"{self.kind.value}: {self.detail} ({self.scope})"


class SnapshotManager:
    def __init__(self, transport: Transport, journal: Journal, session_id: str) -> None:
        self.transport = transport
        self.journal = journal
        self.session_id = session_id

    # ----------------------------------------------------------------------- plan

    async def plan(self, remediation: Remediation) -> SnapshotPlan:
        """Work out — without changing anything — how this change could be undone."""
        if self.transport.dry_run:
            return SnapshotPlan(
                SnapshotKind.SIMULATED,
                scope="(dry run)",
                detail="nothing will be changed",
            )

        paths = extract_paths(remediation.command)
        scope = _common_scope(paths)

        fstype = await self._filesystem_type(scope)
        if fstype in ("btrfs", "zfs"):
            return SnapshotPlan(
                SnapshotKind.BTRFS if fstype == "btrfs" else SnapshotKind.ZFS,
                scope=scope,
                detail=f"{fstype} snapshot of {scope}",
                paths=paths,
            )

        backable = [p for p in paths if not _UNBACKABLE.match(p)]
        if backable:
            return SnapshotPlan(
                SnapshotKind.FILE_BACKUP,
                scope=", ".join(backable),
                detail="copy the affected files aside before the change",
                paths=backable,
            )

        return SnapshotPlan(
            SnapshotKind.NONE,
            scope=scope,
            detail="no snapshot-capable filesystem, and the affected files are not "
            "determinable from the command",
            paths=paths,
        )

    # -------------------------------------------------------------------- protect

    async def protect(self, remediation: Remediation, plan: SnapshotPlan) -> SnapshotRef | None:
        """Take the rollback point. Runs immediately before the approved write."""
        ref: SnapshotRef | None = None

        if plan.kind is SnapshotKind.SIMULATED or self.transport.dry_run:
            ref = SnapshotRef(
                kind=SnapshotKind.SIMULATED,
                scope=plan.scope,
                rollback_hint="[dry-run] nothing was changed, so nothing needs restoring.",
            )
        elif plan.kind in (SnapshotKind.BTRFS, SnapshotKind.ZFS):
            ref = await self.transport.snapshot(plan.scope)
        elif plan.kind is SnapshotKind.FILE_BACKUP:
            backup = getattr(self.transport, "backup_files", None)
            if backup is not None:
                ref = await backup(plan.paths)

        if ref is None:
            ref = SnapshotRef(
                kind=SnapshotKind.NONE,
                scope=plan.scope,
                rollback_hint="No rollback point exists for this change.",
                detail=plan.detail,
            )

        self.journal.record(
            self.session_id,
            JournalKind.SNAPSHOT,
            remediation_id=remediation.id,
            snapshot=ref,
            planned_kind=plan.kind.value,
        )
        return ref

    # ------------------------------------------------------------------- rollback

    async def rollback(self, ref: SnapshotRef) -> str:
        """Roll back where it can be done safely; otherwise hand the human the hint.

        btrfs restores and file-backup restores both overwrite current state, and on a
        machine with no healthy backup that is a decision for a human holding the
        rollback hint, not something to fire automatically.
        """
        rollback = getattr(self.transport, "rollback", None)
        result = await rollback(ref) if rollback is not None else None
        outcome = (
            f"rolled back automatically ({result.summary()})"
            if result is not None
            else f"not rolled back automatically. {ref.rollback_hint}"
        )
        self.journal.record(
            self.session_id, JournalKind.ROLLBACK, snapshot=ref, outcome=outcome
        )
        return outcome

    # -------------------------------------------------------------------- helpers

    async def _filesystem_type(self, path: str) -> str:
        detect = getattr(self.transport, "_filesystem_type", None)
        if detect is None:
            return ""
        try:
            return await detect(path)
        except Exception:  # pragma: no cover - detection is best-effort by design
            return ""


def extract_paths(command: str) -> list[str]:
    """Best-effort: which absolute paths would this command modify?

    Deliberately conservative and deliberately fallible. When it finds nothing, the plan
    degrades to "no automatic rollback" and the human is asked to acknowledge that — the
    failure mode is an extra confirmation, never a silently unprotected write.
    """
    try:
        tokens = shlex.split(command)
    except ValueError:
        return []

    paths: list[str] = []
    saw_mutator = False
    for token in tokens:
        name = token.rsplit("/", 1)[-1]
        if name in _FILE_MUTATORS:
            saw_mutator = True
            continue
        if token.startswith("/") and not _UNBACKABLE.match(token):
            paths.append(token)

    if not saw_mutator:
        # Redirection is refused by the gate, so a non-mutator command with a path
        # argument is usually a service or device name rather than a file to preserve.
        return paths[:1] if paths else []
    return paths


def _common_scope(paths: list[str]) -> str:
    """The narrowest directory that contains everything the command touches."""
    if not paths:
        return "/"
    directories = [p.rsplit("/", 1)[0] or "/" for p in paths]
    common = directories[0]
    for directory in directories[1:]:
        while not directory.startswith(common):
            common = common.rsplit("/", 1)[0] or "/"
            if common == "/":
                break
    return common or "/"
