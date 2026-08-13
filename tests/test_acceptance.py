"""The Section 9 acceptance criteria, as the test plan.

Each test here maps to one checkbox in the build spec's definition of done. They drive the
real session loop — real gate, real journal, real approval queue, real snapshot ladder —
against `MockTransport`, with a scripted model standing in for the API.
"""

from __future__ import annotations

import pytest

from fakes import DynamicClient, Response, finalize, finding, propose, read, text

from triage.core.authorization import AuthorizationError, WriteAuthorization
from triage.core.capability import Capability, CapabilityError
from triage.core.journal import JournalKind
from triage.core.models import FindingCategory, RemediationStatus


def _triage_script(session_ref):
    """A model that gathers, finds, proposes the real fix, verifies, and reports.

    Turns are callables because a remediation must reference the finding that justifies
    it, and finding ids only exist once recorded.
    """
    return [
        # 1. Gather.
        lambda s: Response(
            [
                text("Building a picture of the machine's state."),
                read("c1", "df -h", "filesystem usage"),
                read("c2", "systemctl --failed", "failed units"),
                read("c3", "smartctl -a /dev/sdb", "health of the disk /var lives on"),
                read("c4", "dmesg -T", "kernel errors"),
            ]
        ),
        # 2. Record both faults, correctly categorised.
        lambda s: Response(
            [
                finding(
                    "f1",
                    "/var is 100% full",
                    "df: /dev/sdb1 49G 49G 0 100% /var; du: 39G /var/log/ledger",
                    "an unrotated debug.log has consumed the filesystem",
                    "high",
                    "software_fixable",
                ),
                finding(
                    "f2",
                    "/dev/sdb reports 486 reallocated and 24 pending sectors",
                    "smartctl: Reallocated_Sector_Ct 486, Current_Pending_Sector 24; "
                    "dmesg: UNC at LBA 1902847",
                    "the disk is failing; the kernel errors corroborate the SMART counters",
                    "high",
                    "hardware_suspected",
                ),
            ]
        ),
        # 3. Try the fix through the read tool — this must be refused — then propose it.
        lambda s: Response(
            [read("c5", "truncate -s 0 /var/log/ledger/debug.log", "free the filesystem")]
        ),
        lambda s: Response(
            [
                propose(
                    "p1",
                    "truncate -s 0 /var/log/ledger/debug.log",
                    s.findings[0].id,
                    rationale="df and du agree the 39G debug.log is what filled /var",
                    expected_effect="/var drops to ~14% and ledger.service can checkpoint",
                    rollback_plan="the file is discardable debug output; a copy is taken first",
                )
            ]
        ),
        # 4. Verify against the machine rather than assuming.
        lambda s: Response([read("c6", "df -h", "confirm /var actually has space now")]),
        # 5. Report.
        lambda s: Response(
            [
                finalize(
                    "r1",
                    summary="One software fault fixed and verified; one hardware fault reported.",
                    confident_about="the unrotated log filled /var and truncating it freed it",
                    uncertain_about="how long /dev/sdb will survive; pending sectors may grow",
                    human_next_steps="Replace /dev/sdb. Back up /var before it is swapped.",
                )
            ]
        ),
        lambda s: Response([text("Done.")], stop_reason="end_turn"),
    ]


async def _run(make_session, approval_handler=None, **kwargs):
    holder: dict = {}
    client = DynamicClient(_triage_script(None), lambda: holder["session"])
    session = make_session(client=client, approval_handler=approval_handler, **kwargs)
    holder["session"] = session
    await session.start()
    report = await session.run()
    await session.close()
    return session, report


# ---------------------------------------------------------------------------------------
# [ ] Agent runs read diagnostics and produces a structured findings report separating
#     software_fixable / hardware_suspected / needs_human / informational.
# ---------------------------------------------------------------------------------------


async def test_produces_findings_separated_by_category(make_session, approve_all) -> None:
    session, report = await _run(make_session, approve_all)

    assert report is not None
    assert len(report.by_category(FindingCategory.SOFTWARE_FIXABLE)) == 1
    assert len(report.by_category(FindingCategory.HARDWARE_SUSPECTED)) == 1

    hardware = report.by_category(FindingCategory.HARDWARE_SUSPECTED)[0]
    assert "sdb" in hardware.symptom
    assert report.human_next_steps  # a hardware finding must come with a physical step

    # The reads actually reached the target.
    assert any("smartctl" in c for c in session.transport.executed)


# ---------------------------------------------------------------------------------------
# [ ] For a software-fixable issue the agent produces an exact Remediation with rationale,
#     expected effect, and rollback plan; it blocks on human approval; on approval the
#     system snapshots (or warns), applies, then verifies; the outcome is fed back.
# ---------------------------------------------------------------------------------------


async def test_remediation_is_proposed_approved_snapshotted_applied_and_fed_back(
    make_session, approve_all, journal
) -> None:
    session, report = await _run(make_session, approve_all)

    remediation = session.approvals.all()[0]
    assert remediation.command == "truncate -s 0 /var/log/ledger/debug.log"
    assert remediation.rationale and remediation.expected_effect and remediation.rollback_plan
    assert remediation.finding_id == session.findings[0].id
    assert remediation.status is RemediationStatus.APPLIED
    assert remediation.decided_by == "operator"

    kinds = [e.kind for e in journal.entries(session.id)]
    # Snapshot is recorded before the change it protects, and after the decision to make it.
    assert kinds.index(JournalKind.REMEDIATION_PROPOSED) < kinds.index(JournalKind.SNAPSHOT)
    assert kinds.index(JournalKind.SNAPSHOT) < kinds.index(JournalKind.REMEDIATION_APPLIED)
    assert JournalKind.APPROVAL_DECISION in kinds

    # The outcome went back into the conversation so the model could verify it.
    conversation = str(session._messages)
    assert "REMEDIATION" in conversation and "APPLIED" in conversation
    assert "Now VERIFY" in conversation

    # And it did verify: the post-fix read ran, against a machine whose state changed.
    assert session.transport.executed.count("df -h") == 2
    assert "log_truncated" in session.transport.state


async def test_rejection_leaves_the_machine_untouched(make_session, reject_all) -> None:
    session, _ = await _run(make_session, reject_all)

    remediation = session.approvals.all()[0]
    assert remediation.status is RemediationStatus.REJECTED
    assert "log_truncated" not in session.transport.state
    assert not any("truncate" in c for c in session.transport.executed)
    assert "REJECTED" in str(session._messages)


async def test_a_failed_snapshot_sends_the_decision_back_to_the_human(
    make_session,
) -> None:
    """The plan forecasts protection. When the forecast is wrong, ask again with the truth.

    Proceeding would apply an unprotected write the human never agreed to; rejecting
    outright would decide on their behalf.
    """
    asked: list[bool] = []

    async def handler(remediation, plan):
        from triage.core.session import ApprovalDecision

        asked.append(plan.has_rollback)
        # Say yes the first time on the promise of a backup; only acknowledge the
        # absence of a rollback when actually told there is none.
        return ApprovalDecision(
            approved=True,
            approver="operator",
            reason="ok",
            acknowledge_no_rollback=not plan.has_rollback,
        )

    session, _ = await _run(make_session, handler, snapshot_capable=False)

    assert asked == [True, False], "the human was not re-asked once the backup failed"
    assert session.approvals.all()[0].status is RemediationStatus.APPLIED


async def test_a_failed_snapshot_that_is_not_re_approved_changes_nothing(
    make_session,
) -> None:
    async def handler(remediation, plan):
        from triage.core.session import ApprovalDecision

        # Yes to the protected change; no once it turns out to be unprotected.
        return ApprovalDecision(
            approved=plan.has_rollback,
            approver="operator",
            reason="not without a rollback point",
        )

    session, _ = await _run(make_session, handler, snapshot_capable=False)

    assert session.approvals.all()[0].status is RemediationStatus.REJECTED
    assert "log_truncated" not in session.transport.state
    assert not any("truncate" in c for c in session.transport.executed)


async def test_a_session_with_no_approval_channel_cannot_apply_anything(
    make_session,
) -> None:
    """No handler is not the same as 'approve by default'."""
    session, _ = await _run(make_session, approval_handler=None)

    assert session.approvals.all()[0].status is RemediationStatus.REJECTED
    assert "log_truncated" not in session.transport.state


# ---------------------------------------------------------------------------------------
# [ ] A WRITE/UNKNOWN command routed through run_read_command is refused and steered to
#     propose_remediation.
# ---------------------------------------------------------------------------------------


async def test_write_through_the_read_tool_is_refused_and_steered(
    make_session, approve_all, journal
) -> None:
    session, _ = await _run(make_session, approve_all)

    refusals = [
        e for e in journal.entries(session.id) if e.kind == JournalKind.COMMAND_REFUSED
    ]
    assert len(refusals) == 1
    assert refusals[0].payload["command"] == "truncate -s 0 /var/log/ledger/debug.log"
    assert refusals[0].payload["classification"] == "WRITE"

    # It was refused *before* the transport saw it — the machine never ran it as a read.
    executed_before_approval = session.transport.executed.index(
        "truncate -s 0 /var/log/ledger/debug.log"
    )
    assert executed_before_approval > 0  # it ran once, via the approved write path

    # And the model was told where to route it.
    assert "propose_remediation" in str(session._messages)


# ---------------------------------------------------------------------------------------
# [ ] The full session is reconstructable from the append-only journal; credentials never
#     appear in it.
# ---------------------------------------------------------------------------------------


async def test_session_is_reconstructable_from_the_journal(
    make_session, approve_all, journal
) -> None:
    session, report = await _run(make_session, approve_all)
    entries = journal.entries(session.id)
    kinds = [e.kind for e in entries]

    for required in (
        JournalKind.SESSION_CREATED,
        JournalKind.AUTHORIZATION_ASSERTED,
        JournalKind.TRANSPORT_BOUND,
        JournalKind.COMMAND,
        JournalKind.COMMAND_REFUSED,
        JournalKind.FINDING,
        JournalKind.REMEDIATION_PROPOSED,
        JournalKind.SNAPSHOT,
        JournalKind.APPROVAL_DECISION,
        JournalKind.REMEDIATION_APPLIED,
        JournalKind.REPORT,
    ):
        assert required in kinds, f"journal cannot reconstruct the session without {required}"

    # Every command that ran is in the record, with its output.
    commands = [e.payload["command"] for e in entries if e.kind == JournalKind.COMMAND]
    assert "df -h" in commands and "smartctl -a /dev/sdb" in commands

    intact, bad_seq, _ = journal.verify()
    assert intact and bad_seq is None


async def test_authorization_is_asserted_and_journaled(
    make_session, approve_all, journal
) -> None:
    session, _ = await _run(make_session, approve_all)
    entry = next(
        e for e in journal.entries(session.id) if e.kind == JournalKind.AUTHORIZATION_ASSERTED
    )
    assert entry.payload["statement"] == "I administer this machine (asset #4412)."
    assert entry.payload["asserted_by"] == "operator"


async def test_credentials_never_reach_the_journal(make_session, approve_all, journal) -> None:
    journal.register_secret("hunter2-the-ssh-password")
    session, _ = await _run(make_session, approve_all)
    journal.record(
        session.id,
        JournalKind.NOTE,
        password="hunter2-the-ssh-password",
        note="connected using hunter2-the-ssh-password",
        nested={"credential": "hunter2-the-ssh-password"},
    )

    dump = str([e.payload for e in journal.entries(session.id)])
    assert "hunter2-the-ssh-password" not in dump
    assert "[REDACTED]" in dump


# ---------------------------------------------------------------------------------------
# [ ] The entire flow runs under --dry-run and against MockTransport with no real target.
# ---------------------------------------------------------------------------------------


async def test_dry_run_completes_the_whole_loop_without_touching_anything(
    make_session, approve_all
) -> None:
    session, report = await _run(make_session, approve_all, dry_run=True)

    assert report is not None
    remediation = session.approvals.all()[0]
    assert remediation.status is RemediationStatus.APPLIED
    assert remediation.result is not None and remediation.result.simulated

    # The write was proposed, approved, and simulated — but never executed.
    assert not any("truncate" in c for c in session.transport.executed)
    assert "log_truncated" not in session.transport.state
    # Reads still ran for real, so the rehearsal is against actual state.
    assert "df -h" in session.transport.executed


# ---------------------------------------------------------------------------------------
# [ ] No code path applies a mutating command without an approved Remediation.
# ---------------------------------------------------------------------------------------


async def test_the_write_path_cannot_be_reached_without_an_approval(make_session) -> None:
    session = make_session()
    await session.start()
    transport = session.transport

    # A raw command string is not an authorization.
    with pytest.raises(AuthorizationError):
        await transport.run_write("rm -rf /var/log/ledger")  # type: ignore[arg-type]

    # Nor is a hand-built object that merely looks like one.
    class Lookalike:
        remediation_id = "rem_fake"
        command = "rm -rf /var/log/ledger"

        def consume(self) -> str:
            return self.command

    with pytest.raises(AuthorizationError):
        await transport.run_write(Lookalike())  # type: ignore[arg-type]

    # Nor can one be constructed directly, bypassing the queue.
    with pytest.raises(AuthorizationError):
        WriteAuthorization(
            object(),
            remediation_id="rem_fake",
            command="rm -rf /var/log/ledger",
            approved_by="nobody",
        )

    assert not transport.executed
    await session.close()


async def test_an_approval_applies_exactly_one_command_exactly_once(
    make_session, approve_all
) -> None:
    from triage.core.models import Remediation, RiskLevel
    from triage.remediation.snapshot import SnapshotPlan
    from triage.core.models import SnapshotKind

    session = make_session(approval_handler=approve_all)
    await session.start()

    remediation = Remediation(
        command="systemctl restart ledger.service",
        rationale="the unit died on a full filesystem that is now clear",
        expected_effect="the unit comes back up",
        rollback_plan="stop the unit again",
        risk=RiskLevel.LOW,
        finding_id="find_x",
    )
    session.approvals.propose(remediation)
    plan = SnapshotPlan(SnapshotKind.FILE_BACKUP, "/var", "backup", ["/var/lib/ledger"])
    authorization = session.approvals.approve(remediation.id, "operator", plan=plan)

    await session.transport.run_write(authorization)
    with pytest.raises(AuthorizationError):
        await session.transport.run_write(authorization)

    assert session.transport.executed.count("systemctl restart ledger.service") == 1
    await session.close()


async def test_read_only_capability_cannot_accumulate_a_pending_write(make_session) -> None:
    session = make_session(capability=Capability.EXECUTE_RO)
    await session.start()

    from triage.core.models import Remediation, RiskLevel

    with pytest.raises(CapabilityError):
        session.approvals.propose(
            Remediation(
                command="rm -rf /var/log/ledger",
                rationale="",
                expected_effect="",
                rollback_plan="",
                risk=RiskLevel.HIGH,
                finding_id="find_x",
            )
        )
    assert not session.approvals.all()
    await session.close()


async def test_write_tool_is_not_offered_below_execute_rw() -> None:
    from triage.agent.tools import tool_schema

    rw = {t["name"] for t in tool_schema(Capability.EXECUTE_RW)}
    ro = {t["name"] for t in tool_schema(Capability.EXECUTE_RO)}
    advise = {t["name"] for t in tool_schema(Capability.ADVISE_ONLY)}

    assert "propose_remediation" in rw
    assert "propose_remediation" not in ro
    assert "propose_remediation" not in advise
    assert "run_read_command" in ro
    assert "request_observation" in advise
