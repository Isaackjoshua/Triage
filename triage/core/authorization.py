"""WriteAuthorization — the only thing a transport will accept on the write path.

`Transport.run_write` does not take a command string. It takes one of these, and reads
the command *out of it*. That is what makes "no code path applies a mutating command
without an approved Remediation" a structural property rather than a rule someone has to
remember: to mutate the machine you need an authorization, and only the approval queue
can mint one, and only after a human said yes.

Each authorization is single-use. `consume()` raises on the second call, so an approved
remediation cannot be replayed.
"""

from __future__ import annotations

import uuid
from typing import Any

from .models import SnapshotRef, utc_now

#: Module-private sentinel. `ApprovalQueue` passes this to construct an authorization;
#: nothing else has a legitimate reason to, and an accidental attempt fails loudly
#: rather than quietly producing a token that unlocks the write path.
_MINT_KEY = object()


class AuthorizationError(PermissionError):
    """Raised when the write path is reached without a valid, unused authorization."""


class WriteAuthorization:
    """Proof that a specific command, from a specific remediation, was approved."""

    __slots__ = (
        "id",
        "remediation_id",
        "command",
        "approved_by",
        "approved_at",
        "reason",
        "snapshot",
        "dry_run",
        "_consumed",
    )

    def __init__(
        self,
        mint_key: Any,
        *,
        remediation_id: str,
        command: str,
        approved_by: str,
        reason: str = "",
        snapshot: SnapshotRef | None = None,
        dry_run: bool = False,
    ) -> None:
        if mint_key is not _MINT_KEY:
            raise AuthorizationError(
                "WriteAuthorization cannot be constructed directly. It is minted only by "
                "ApprovalQueue.approve(), after a human approves a proposed remediation."
            )
        self.id = f"auth_{uuid.uuid4().hex[:12]}"
        self.remediation_id = remediation_id
        self.command = command
        self.approved_by = approved_by
        self.approved_at = utc_now()
        self.reason = reason
        self.snapshot = snapshot
        self.dry_run = dry_run
        self._consumed = False

    @property
    def consumed(self) -> bool:
        return self._consumed

    def consume(self) -> str:
        """Redeem the authorization once and return the command it authorizes."""
        if self._consumed:
            raise AuthorizationError(
                f"Authorization {self.id} for remediation {self.remediation_id} has already "
                "been used. Each approval applies exactly one command, exactly once."
            )
        self._consumed = True
        return self.command

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        state = "consumed" if self._consumed else "unused"
        return f"<WriteAuthorization {self.id} {self.remediation_id} {state}>"


def _mint(
    *,
    remediation_id: str,
    command: str,
    approved_by: str,
    reason: str = "",
    snapshot: SnapshotRef | None = None,
    dry_run: bool = False,
) -> WriteAuthorization:
    """Internal constructor. Call site is `ApprovalQueue.approve()` and nowhere else."""
    return WriteAuthorization(
        _MINT_KEY,
        remediation_id=remediation_id,
        command=command,
        approved_by=approved_by,
        reason=reason,
        snapshot=snapshot,
        dry_run=dry_run,
    )
