"""The approval queue — where the model's proposal meets a human's decision.

Nothing is auto-applied. Not the obvious fixes, not the low-risk ones, not the ones the
model is confident about. The queue holds proposals; a human approves or rejects each one;
approval mints a single-use `WriteAuthorization` and nothing else can.

Two refusals are worth calling out because they are the ones a hurried caller would want
to skip:

* Proposing a write at all requires `EXECUTE_RW`. At `EXECUTE_RO` the queue will not
  register the proposal, so a read-only session cannot accumulate a pending write.
* Approving a change with no rollback point requires an explicit second acknowledgement.
  The caller has to say "I understand nothing can be restored" as a separate act from
  "yes, run it".
"""

from __future__ import annotations

from ..core.authorization import WriteAuthorization, _mint
from ..core.capability import Capability, require_write
from ..core.journal import Journal, JournalKind
from ..core.models import Remediation, RemediationStatus, SnapshotKind, SnapshotRef, utc_now
from .snapshot import SnapshotPlan


class ApprovalError(RuntimeError):
    """Raised when the approval flow is used in a way that would bypass the gate."""


class NoRollbackAcknowledgementRequired(ApprovalError):
    """Approval was attempted for a change that cannot be undone.

    Not a failure — a stop. The caller should show the human `plan.describe()` and
    re-approve with `acknowledge_no_rollback=True` if they still want to proceed.
    """

    def __init__(self, remediation: Remediation, plan: SnapshotPlan) -> None:
        self.remediation = remediation
        self.plan = plan
        super().__init__(
            f"'{remediation.command}' has no automatic rollback ({plan.detail}). "
            "Approving it requires explicitly acknowledging that nothing can be restored "
            "if it goes wrong."
        )


class ApprovalQueue:
    """Pending remediations for one session, plus the decisions made about them."""

    def __init__(self, journal: Journal, session_id: str, capability: Capability) -> None:
        self.journal = journal
        self.session_id = session_id
        self.capability = capability
        self._remediations: dict[str, Remediation] = {}

    # -------------------------------------------------------------------- propose

    def propose(self, remediation: Remediation) -> Remediation:
        """Register a proposed change. This never executes anything."""
        require_write(self.capability, "Proposing a remediation")
        if not remediation.finding_id:
            raise ApprovalError(
                "No fix without a finding: every remediation must reference the recorded "
                "finding whose evidence justifies it."
            )
        remediation.status = RemediationStatus.PROPOSED
        self._remediations[remediation.id] = remediation
        self.journal.record(
            self.session_id, JournalKind.REMEDIATION_PROPOSED, remediation=remediation
        )
        return remediation

    # --------------------------------------------------------------------- decide

    def approve(
        self,
        remediation_id: str,
        approver: str,
        *,
        plan: SnapshotPlan,
        snapshot: SnapshotRef | None = None,
        reason: str = "",
        acknowledge_no_rollback: bool = False,
        dry_run: bool = False,
    ) -> WriteAuthorization:
        """Approve a proposal and mint the single-use authorization that applies it."""
        require_write(self.capability, "Approving a remediation")
        remediation = self._pending(remediation_id)

        # A dry run needs no rollback point: the write is simulated, so there is nothing
        # to restore and nothing to acknowledge.
        simulated = plan.kind is SnapshotKind.SIMULATED or (
            snapshot is not None and snapshot.kind is SnapshotKind.SIMULATED
        )

        # Otherwise, judge the rollback point that actually exists rather than the one
        # that was planned. A plan can promise a btrfs snapshot and `protect()` can still
        # come back empty-handed; trusting the plan there approves an unprotected write.
        if not simulated and snapshot is not None and not snapshot.is_rollback_possible:
            plan = SnapshotPlan(
                snapshot.kind,
                scope=snapshot.scope,
                detail=f"the planned {plan.kind.value} rollback point could not be created",
                paths=plan.paths,
            )

        if not simulated and not plan.has_rollback and not acknowledge_no_rollback:
            raise NoRollbackAcknowledgementRequired(remediation, plan)

        remediation.status = RemediationStatus.APPROVED
        remediation.decided_at = utc_now()
        remediation.decided_by = approver
        remediation.decision_reason = reason
        remediation.snapshot = snapshot

        self.journal.record(
            self.session_id,
            JournalKind.APPROVAL_DECISION,
            remediation_id=remediation.id,
            command=remediation.command,
            decision="approved",
            approver=approver,
            reason=reason,
            rollback_available=plan.has_rollback,
            no_rollback_acknowledged=bool(acknowledge_no_rollback and not plan.has_rollback),
            snapshot=snapshot,
        )

        return _mint(
            remediation_id=remediation.id,
            command=remediation.command,
            approved_by=approver,
            reason=reason,
            snapshot=snapshot,
            dry_run=dry_run,
        )

    def reject(self, remediation_id: str, approver: str, reason: str = "") -> Remediation:
        remediation = self._pending(remediation_id)
        remediation.status = RemediationStatus.REJECTED
        remediation.decided_at = utc_now()
        remediation.decided_by = approver
        remediation.decision_reason = reason
        self.journal.record(
            self.session_id,
            JournalKind.APPROVAL_DECISION,
            remediation_id=remediation.id,
            command=remediation.command,
            decision="rejected",
            approver=approver,
            reason=reason,
        )
        return remediation

    # ------------------------------------------------------------------ inspection

    def get(self, remediation_id: str) -> Remediation | None:
        return self._remediations.get(remediation_id)

    def pending(self) -> list[Remediation]:
        return [
            r for r in self._remediations.values() if r.status is RemediationStatus.PROPOSED
        ]

    def all(self) -> list[Remediation]:
        return list(self._remediations.values())

    def has_pending(self) -> bool:
        return bool(self.pending())

    # -------------------------------------------------------------------- private

    def _pending(self, remediation_id: str) -> Remediation:
        remediation = self._remediations.get(remediation_id)
        if remediation is None:
            raise ApprovalError(f"No remediation {remediation_id} in this session.")
        if remediation.status is not RemediationStatus.PROPOSED:
            raise ApprovalError(
                f"Remediation {remediation_id} is already {remediation.status.value}; "
                "each proposal is decided once."
            )
        return remediation
