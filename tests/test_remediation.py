"""The approval queue and the snapshot ladder — the gate between proposal and change."""

from __future__ import annotations

import pytest

from triage.core.capability import Capability, CapabilityError
from triage.core.journal import Journal
from triage.core.models import Remediation, RemediationStatus, RiskLevel, SnapshotKind
from triage.remediation.approval import (
    ApprovalError,
    ApprovalQueue,
    NoRollbackAcknowledgementRequired,
)
from triage.remediation.snapshot import SnapshotManager, SnapshotPlan, extract_paths
from triage.transports.mock import faulty_workstation


def make_remediation(command: str = "truncate -s 0 /var/log/ledger/debug.log") -> Remediation:
    return Remediation(
        command=command,
        rationale="df and du agree this file filled the filesystem",
        expected_effect="/var drops below 20%",
        rollback_plan="the file is discardable debug output",
        risk=RiskLevel.MEDIUM,
        finding_id="find_1",
    )


@pytest.fixture
def queue(journal: Journal) -> ApprovalQueue:
    return ApprovalQueue(journal, "s1", Capability.EXECUTE_RW)


@pytest.fixture
def backed_plan() -> SnapshotPlan:
    return SnapshotPlan(SnapshotKind.FILE_BACKUP, "/var/log", "copy aside", ["/var/log/x"])


@pytest.fixture
def bare_plan() -> SnapshotPlan:
    return SnapshotPlan(SnapshotKind.NONE, "/", "nothing can be restored")


# ------------------------------------------------------------------------------ proposal


def test_no_fix_without_a_finding(queue: ApprovalQueue) -> None:
    orphan = make_remediation()
    orphan.finding_id = None
    with pytest.raises(ApprovalError, match="No fix without a finding"):
        queue.propose(orphan)


def test_read_only_sessions_cannot_propose(journal: Journal) -> None:
    queue = ApprovalQueue(journal, "s1", Capability.EXECUTE_RO)
    with pytest.raises(CapabilityError):
        queue.propose(make_remediation())


def test_advise_only_sessions_cannot_propose(journal: Journal) -> None:
    queue = ApprovalQueue(journal, "s1", Capability.ADVISE_ONLY)
    with pytest.raises(CapabilityError):
        queue.propose(make_remediation())


# ------------------------------------------------------------------------------ decision


def test_approval_mints_a_usable_authorization(queue: ApprovalQueue, backed_plan) -> None:
    remediation = queue.propose(make_remediation())
    authorization = queue.approve(remediation.id, "operator", plan=backed_plan)

    assert authorization.remediation_id == remediation.id
    assert authorization.command == remediation.command
    assert not authorization.consumed
    assert authorization.consume() == remediation.command
    assert remediation.status is RemediationStatus.APPROVED


def test_a_change_with_no_rollback_needs_a_second_acknowledgement(
    queue: ApprovalQueue, bare_plan
) -> None:
    remediation = queue.propose(make_remediation("dd if=/dev/zero of=/dev/sdb1 bs=1M count=1"))

    with pytest.raises(NoRollbackAcknowledgementRequired) as exc:
        queue.approve(remediation.id, "operator", plan=bare_plan)
    assert "nothing can be restored" in str(exc.value)
    assert remediation.status is RemediationStatus.PROPOSED  # still undecided

    authorization = queue.approve(
        remediation.id, "operator", plan=bare_plan, acknowledge_no_rollback=True
    )
    assert authorization.command.startswith("dd ")


def test_a_snapshot_that_failed_to_materialise_is_not_approved_on_the_plans_promise(
    queue: ApprovalQueue, backed_plan
) -> None:
    """The plan promised a backup; `protect()` came back empty. That is a no-rollback write."""
    from triage.core.models import SnapshotRef

    remediation = queue.propose(make_remediation())
    failed = SnapshotRef(
        kind=SnapshotKind.NONE, scope="/var/log", rollback_hint="none", detail="mkdir failed"
    )
    with pytest.raises(NoRollbackAcknowledgementRequired):
        queue.approve(remediation.id, "operator", plan=backed_plan, snapshot=failed)


def test_a_dry_run_needs_no_acknowledgement(queue: ApprovalQueue) -> None:
    """Nothing is changed, so there is nothing to restore and nothing to acknowledge."""
    remediation = queue.propose(make_remediation())
    plan = SnapshotPlan(SnapshotKind.SIMULATED, "(dry run)", "nothing will change")
    authorization = queue.approve(remediation.id, "operator", plan=plan, dry_run=True)
    assert authorization.dry_run


def test_each_proposal_is_decided_exactly_once(queue: ApprovalQueue, backed_plan) -> None:
    remediation = queue.propose(make_remediation())
    queue.approve(remediation.id, "operator", plan=backed_plan)
    with pytest.raises(ApprovalError, match="already approved"):
        queue.approve(remediation.id, "operator", plan=backed_plan)
    with pytest.raises(ApprovalError, match="already approved"):
        queue.reject(remediation.id, "operator", "changed my mind")


def test_rejection_is_journaled_with_its_reason(
    queue: ApprovalQueue, journal: Journal, backed_plan
) -> None:
    remediation = queue.propose(make_remediation())
    queue.reject(remediation.id, "operator", "the log may still be needed for the postmortem")

    decision = next(e for e in journal.entries("s1") if e.kind == "approval_decision")
    assert decision.payload["decision"] == "rejected"
    assert "postmortem" in decision.payload["reason"]
    assert not queue.has_pending()


# ------------------------------------------------------------------------------ snapshots


@pytest.mark.parametrize(
    "command,expected",
    [
        ("truncate -s 0 /var/log/ledger/debug.log", ["/var/log/ledger/debug.log"]),
        ("rm /etc/systemd/system/broken.service", ["/etc/systemd/system/broken.service"]),
        ("cp /etc/fstab /etc/fstab.bak", ["/etc/fstab", "/etc/fstab.bak"]),
        ("systemctl restart ledger.service", []),  # a unit name is not a file
        ("dd if=/dev/zero of=/dev/sda", []),  # device nodes are not backed up
    ],
)
def test_affected_paths_are_extracted_conservatively(command: str, expected: list[str]) -> None:
    assert extract_paths(command) == expected


async def test_the_ladder_prefers_a_filesystem_snapshot(journal: Journal) -> None:
    transport = faulty_workstation(filesystem_type="btrfs")
    await transport.connect()
    manager = SnapshotManager(transport, journal, "s1")

    plan = await manager.plan(make_remediation())
    assert plan.kind is SnapshotKind.BTRFS

    ref = await manager.protect(make_remediation(), plan)
    assert ref is not None and ref.is_rollback_possible
    await transport.close()


async def test_the_ladder_falls_back_to_copying_the_affected_files(journal: Journal) -> None:
    transport = faulty_workstation(filesystem_type="ext4")
    await transport.connect()
    manager = SnapshotManager(transport, journal, "s1")

    plan = await manager.plan(make_remediation())
    assert plan.kind is SnapshotKind.FILE_BACKUP
    assert plan.paths == ["/var/log/ledger/debug.log"]
    await transport.close()


async def test_the_ladder_admits_when_it_has_nothing(journal: Journal) -> None:
    transport = faulty_workstation(filesystem_type="ext4", snapshot_capable=False)
    await transport.connect()
    manager = SnapshotManager(transport, journal, "s1")

    remediation = make_remediation("dd if=/dev/zero of=/dev/sdb1 bs=1M count=1")
    plan = await manager.plan(remediation)
    assert plan.kind is SnapshotKind.NONE
    assert not plan.has_rollback
    assert "NO AUTOMATIC ROLLBACK" in plan.describe()

    ref = await manager.protect(remediation, plan)
    assert ref is not None and not ref.is_rollback_possible
    await transport.close()


async def test_a_dry_run_snapshot_is_marked_simulated(journal: Journal) -> None:
    transport = faulty_workstation(dry_run=True)
    await transport.connect()
    manager = SnapshotManager(transport, journal, "s1")

    plan = await manager.plan(make_remediation())
    ref = await manager.protect(make_remediation(), plan)
    assert plan.kind is SnapshotKind.SIMULATED
    assert ref is not None and ref.kind is SnapshotKind.SIMULATED
    await transport.close()
