from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from triage.core.capability import Capability  # noqa: E402
from triage.core.gate import CommandGate  # noqa: E402
from triage.core.journal import Journal  # noqa: E402
from triage.core.models import AuthorizationRecord, TargetDescriptor  # noqa: E402
from triage.core.session import ApprovalDecision, DiagnosticSession  # noqa: E402
from triage.transports.mock import faulty_workstation  # noqa: E402


@pytest.fixture
def gate() -> CommandGate:
    return CommandGate()


@pytest.fixture
def journal(tmp_path: Path) -> Journal:
    return Journal(tmp_path / "triage.sqlite", jsonl_path=tmp_path / "triage.jsonl")


@pytest.fixture
def authorization() -> AuthorizationRecord:
    return AuthorizationRecord(
        statement="I administer this machine (asset #4412).", asserted_by="operator"
    )


@pytest.fixture
def approve_all():
    """An approval handler that says yes, including to changes with no rollback."""

    async def handler(remediation, plan):
        return ApprovalDecision(
            approved=True,
            approver="operator",
            reason="approved in test",
            acknowledge_no_rollback=True,
        )

    return handler


@pytest.fixture
def reject_all():
    async def handler(remediation, plan):
        return ApprovalDecision(
            approved=False, approver="operator", reason="rejected in test"
        )

    return handler


@pytest.fixture
def make_session(journal, authorization):
    """Build a session against the mock faulty workstation."""

    def factory(
        client=None,
        capability: Capability = Capability.EXECUTE_RW,
        dry_run: bool = False,
        approval_handler=None,
        **transport_kwargs,
    ) -> DiagnosticSession:
        transport = faulty_workstation(
            capability=capability, dry_run=dry_run, **transport_kwargs
        )
        return DiagnosticSession(
            transport,
            journal,
            target=TargetDescriptor(name="ws-14", notes="mock faulty workstation"),
            authorization=authorization,
            capability=capability,
            approval_handler=approval_handler,
            client=client,
            max_turns=20,
        )

    return factory
