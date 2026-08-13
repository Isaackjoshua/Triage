"""SSHTransport — Phase 1's executable transport, against a reachable Linux target.

Credentials are supplied by the operator per session, held only in memory, and never
handed to the journal. The transport itself does no classification: by the time a command
reaches `_execute` the gate has already ruled, and on the write path an approval has
already been consumed.

Commands run non-interactively with a hard timeout. If the target hangs, the session does
not — a machine that is faulty is exactly the kind that stops answering mid-command.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from ..core.capability import Capability
from ..core.models import CommandResult, SnapshotKind, SnapshotRef, TransportInfo, utc_now
from .base import Transport, TransportError

try:  # asyncssh is a hard dependency in practice, but importing lazily keeps the
    import asyncssh  # mock/dry-run path usable in environments without it installed.
except ImportError:  # pragma: no cover
    asyncssh = None  # type: ignore[assignment]


class SSHTransport(Transport):
    name = "ssh"

    def __init__(
        self,
        host: str,
        username: str,
        *,
        port: int = 22,
        password: str | None = None,
        client_keys: list[str] | None = None,
        passphrase: str | None = None,
        known_hosts: str | None = None,
        capability: Capability = Capability.EXECUTE_RO,
        connect_timeout_s: float = 15.0,
        **kwargs: Any,
    ) -> None:
        super().__init__(target=f"{username}@{host}:{port}", capability=capability, **kwargs)
        self.host = host
        self.port = port
        self.username = username
        self.connect_timeout_s = connect_timeout_s
        #: Credentials live here and nowhere else. They are not journaled, not logged,
        #: and not included in `describe()`.
        self._password = password
        self._client_keys = client_keys
        self._passphrase = passphrase
        #: asyncssh verifies host keys by default. `None` means "use the operator's
        #: known_hosts"; explicitly passing "" disables checking, which is the
        #: operator's call to make for a machine that may have been reinstalled.
        self._known_hosts = known_hosts
        self._conn: Any = None
        self._server_banner = ""

    # ------------------------------------------------------------------ lifecycle

    async def _connect(self) -> None:
        if asyncssh is None:  # pragma: no cover
            raise TransportError(
                "asyncssh is not installed. Install it with `pip install asyncssh` to use "
                "the SSH transport, or run against --mock."
            )
        options: dict[str, Any] = {
            "username": self.username,
            "port": self.port,
            "known_hosts": () if self._known_hosts == "" else self._known_hosts,
        }
        if self._password:
            options["password"] = self._password
        if self._client_keys:
            options["client_keys"] = self._client_keys
        if self._passphrase:
            options["passphrase"] = self._passphrase

        try:
            self._conn = await asyncio.wait_for(
                asyncssh.connect(self.host, **options), timeout=self.connect_timeout_s
            )
        except asyncio.TimeoutError as exc:
            raise TransportError(
                f"Timed out after {self.connect_timeout_s:.0f}s connecting to {self.target}. "
                "The target may be down, unreachable, or too degraded to accept a session."
            ) from exc
        except Exception as exc:  # asyncssh raises a family of connection errors
            raise TransportError(f"Could not connect to {self.target}: {exc}") from exc

        # A cheap identifying read, so `describe()` can report something real.
        try:
            result = await self._execute("uname -sr", timeout_s=10.0)
            self._server_banner = result.stdout.strip()
        except Exception:  # pragma: no cover - identification is best-effort
            self._server_banner = ""

    async def _close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            try:
                await self._conn.wait_closed()
            except Exception:  # pragma: no cover - already torn down
                pass
            self._conn = None

    def describe(self) -> TransportInfo:
        return TransportInfo(
            name=self.name,
            capability=self.capability.value,
            reachable=self._conn is not None,
            target=self.target,
            supports_snapshot=True,
            supports_observation=self.observation_provider is not None,
            detail=self._server_banner or "Linux target over SSH",
        )

    # ------------------------------------------------------------------ execution

    async def _execute(self, command: str, timeout_s: float) -> CommandResult:
        if self._conn is None:
            raise TransportError("SSH transport is not connected. Call connect() first.")

        started_at = utc_now()
        started = time.monotonic()
        try:
            completed = await asyncio.wait_for(
                self._conn.run(command, check=False), timeout=timeout_s
            )
        except asyncio.TimeoutError:
            return CommandResult(
                command=command,
                exit_code=124,
                stderr=f"Timed out after {timeout_s:.0f}s with no result.",
                duration_s=time.monotonic() - started,
                timed_out=True,
                started_at=started_at,
            )
        except Exception as exc:
            raise TransportError(f"Command failed on {self.target}: {exc}") from exc

        return CommandResult(
            command=command,
            exit_code=int(completed.exit_status if completed.exit_status is not None else -1),
            stdout=_as_text(completed.stdout),
            stderr=_as_text(completed.stderr),
            duration_s=time.monotonic() - started,
            started_at=started_at,
        )

    # ------------------------------------------------------------------ snapshots

    async def snapshot(self, scope: str) -> SnapshotRef | None:
        """Detect a snapshot-capable filesystem for `scope` and take a rollback point.

        Detection is read-only. Taking the snapshot is itself a mutation, so it is run
        through `_execute` directly — it is part of the approved-write sequence, ordered
        before the change it protects, not a command the agent chose.
        """
        if self.dry_run:
            return await super().snapshot(scope)

        fstype = await self._filesystem_type(scope)

        if fstype == "btrfs":
            ref = SnapshotRef(
                kind=SnapshotKind.BTRFS,
                scope=scope,
                rollback_hint="",
                detail="btrfs read-only subvolume snapshot",
            )
            path = f"/.triage-snapshots/{ref.id}"
            mkdir = await self._execute("mkdir -p /.triage-snapshots", 30.0)
            if not mkdir.ok:
                return None
            taken = await self._execute(f"btrfs subvolume snapshot -r {scope} {path}", 120.0)
            if not taken.ok:
                return None
            ref.rollback_hint = (
                f"Restore with: btrfs subvolume delete {scope} && "
                f"btrfs subvolume snapshot {path} {scope}"
            )
            return ref

        if fstype == "zfs":
            dataset = await self._zfs_dataset(scope)
            if dataset:
                ref = SnapshotRef(
                    kind=SnapshotKind.ZFS,
                    scope=dataset,
                    rollback_hint="",
                    detail="ZFS snapshot",
                )
                name = f"{dataset}@triage-{ref.id}"
                taken = await self._execute(f"zfs snapshot {name}", 120.0)
                if taken.ok:
                    ref.rollback_hint = f"Restore with: zfs rollback {name}"
                    return ref

        return None

    async def backup_files(self, paths: list[str]) -> SnapshotRef | None:
        """Copy specific files aside when a filesystem snapshot is not available.

        This is the second rung of the ladder in the safety invariants: snapshot if you
        can, back up the touched files if you can't, and if neither is possible say so
        plainly rather than implying a rollback exists.
        """
        if not paths:
            return None
        if self.dry_run:
            return SnapshotRef(
                kind=SnapshotKind.SIMULATED,
                scope=", ".join(paths),
                rollback_hint="[dry-run] no backup was taken; nothing was changed.",
            )

        ref = SnapshotRef(
            kind=SnapshotKind.FILE_BACKUP,
            scope=", ".join(paths),
            rollback_hint="",
            detail="Copies of the specific files the command will touch",
        )
        directory = f"/var/backups/triage/{ref.id}"
        made = await self._execute(f"mkdir -p {directory}", 30.0)
        if not made.ok:
            return None

        copied: list[str] = []
        for path in paths:
            result = await self._execute(f"cp -a --parents {path} {directory}", 60.0)
            if result.ok:
                copied.append(path)
        if not copied:
            return None

        ref.scope = ", ".join(copied)
        ref.rollback_hint = f"Restore with: cp -a {directory}/<path> <path> (backups under {directory})"
        return ref

    async def rollback(self, ref: SnapshotRef) -> CommandResult | None:
        """Roll a snapshot back. Returns None when the ref has no automatic rollback."""
        if not ref.is_rollback_possible:
            return None
        if ref.kind is SnapshotKind.ZFS:
            name = ref.rollback_hint.split("zfs rollback ", 1)[-1].strip()
            return await self._execute(f"zfs rollback {name}", 300.0)
        # btrfs and file backups are deliberately not automated: both destroy the
        # current state, and on a machine with no healthy backup that decision belongs
        # to a human with the hint in front of them.
        return None

    # ------------------------------------------------------------------- helpers

    async def _filesystem_type(self, path: str) -> str:
        result = await self._execute(f"findmnt -no FSTYPE --target {path}", 20.0)
        return result.stdout.strip() if result.ok else ""

    async def _zfs_dataset(self, path: str) -> str:
        result = await self._execute(f"findmnt -no SOURCE --target {path}", 20.0)
        return result.stdout.strip() if result.ok else ""


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)
