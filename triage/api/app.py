"""The FastAPI service: session API, event stream, and out-of-band approvals.

Every piece of transport, session, gate, and journal logic lives behind this — the service
creates sessions and relays decisions, and nothing else. That is deliberate: a web UI is a
later add-on, and it should be able to appear without any of the core changing.

The approval flow is the one part that differs from the CLI. There, a human is at the
terminal and the handler blocks on `input()`. Here the session's approval handler parks on
an `asyncio.Future`, the pending proposal shows up on the event stream and in
`GET /sessions/{id}/remediations`, and the future is resolved by a POST. The session is
paused the whole time, exactly as it is at the terminal prompt — no change is applied while
anyone is deciding.

Credentials are supplied per session in the create request, held only in the transport, and
never journaled. They are not echoed back by any endpoint.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..core.capability import Capability
from ..core.journal import Journal
from ..core.models import (
    AuthorizationRecord,
    Observation,
    ObservationKind,
    Remediation,
    TargetDescriptor,
    serialize,
)
from ..core.session import ApprovalDecision, DiagnosticSession
from ..remediation.snapshot import SnapshotPlan
from ..transports.base import Transport
from ..transports.mock import faulty_workstation

app = FastAPI(
    title="Triage",
    description="A diagnostic agent that looks freely and touches only through approval.",
    version="0.1.0",
)


# --------------------------------------------------------------------------- registry


@dataclass
class PendingApproval:
    remediation: Remediation
    plan: SnapshotPlan
    future: "asyncio.Future[ApprovalDecision]"


@dataclass
class PendingObservation:
    instruction: str
    expects: ObservationKind
    future: "asyncio.Future[Observation]"


@dataclass
class SessionHandle:
    session: DiagnosticSession
    journal: Journal
    task: asyncio.Task[Any] | None = None
    approvals: dict[str, PendingApproval] = field(default_factory=dict)
    observations: dict[str, PendingObservation] = field(default_factory=dict)


SESSIONS: dict[str, SessionHandle] = {}


def _handle(session_id: str) -> SessionHandle:
    handle = SESSIONS.get(session_id)
    if handle is None:
        raise HTTPException(status_code=404, detail=f"No session {session_id}")
    return handle


# ---------------------------------------------------------------------------- schemas


class SSHTarget(BaseModel):
    host: str
    user: str
    port: int = 22
    #: Supplied per session, held in memory by the transport, never journaled or returned.
    password: str | None = None
    key_path: str | None = None
    insecure_host_key: bool = False


class CreateSession(BaseModel):
    authorization: str = Field(
        ..., description="Your assertion that you are permitted to service this target."
    )
    asserted_by: str = Field(..., description="Who is asserting it.")
    capability: Literal["EXECUTE_RW", "EXECUTE_RO", "ADVISE_ONLY"] = "EXECUTE_RW"
    dry_run: bool = False
    mock: bool = False
    ssh: SSHTarget | None = None
    target_name: str | None = None
    notes: str = ""
    instruction: str | None = None
    journal_path: str = "triage.sqlite"
    max_turns: int = 40


class Decision(BaseModel):
    approved: bool
    approver: str = "operator"
    reason: str = ""
    acknowledge_no_rollback: bool = Field(
        False,
        description=(
            "Required to approve a change with no rollback point. Separate from `approved` "
            "on purpose: accepting that nothing can be restored is its own decision."
        ),
    )


class ObservationAnswer(BaseModel):
    value: str = ""
    media_path: str | None = None


# --------------------------------------------------------------------------- handlers


def _make_approval_handler(handle_id: str) -> Any:
    async def handler(remediation: Remediation, plan: SnapshotPlan) -> ApprovalDecision:
        handle = SESSIONS[handle_id]
        loop = asyncio.get_running_loop()
        future: asyncio.Future[ApprovalDecision] = loop.create_future()
        handle.approvals[remediation.id] = PendingApproval(remediation, plan, future)
        try:
            return await future
        finally:
            handle.approvals.pop(remediation.id, None)

    return handler


def _make_observation_provider(handle_id: str) -> Any:
    async def provider(instruction: str, expects: ObservationKind) -> Observation:
        handle = SESSIONS[handle_id]
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Observation] = loop.create_future()
        pending = PendingObservation(instruction, expects, future)
        key = f"obsreq_{len(handle.observations) + 1}"
        handle.observations[key] = pending
        try:
            return await future
        finally:
            handle.observations.pop(key, None)

    return provider


def _build_transport(body: CreateSession, capability: Capability) -> Transport:
    if body.mock:
        return faulty_workstation(capability=capability, dry_run=body.dry_run)
    if body.ssh is None:
        raise HTTPException(
            status_code=400, detail="Provide `ssh` connection details, or set `mock: true`."
        )
    from ..transports.ssh import SSHTransport

    return SSHTransport(
        host=body.ssh.host,
        username=body.ssh.user,
        port=body.ssh.port,
        password=body.ssh.password,
        client_keys=[body.ssh.key_path] if body.ssh.key_path else None,
        known_hosts="" if body.ssh.insecure_host_key else None,
        capability=capability,
        dry_run=body.dry_run,
    )


# -------------------------------------------------------------------------- endpoints


@app.post("/sessions", status_code=201)
async def create_session(body: CreateSession) -> dict[str, Any]:
    """Create a session and start the agent loop in the background."""
    if not body.authorization.strip():
        raise HTTPException(
            status_code=400,
            detail="An authorization assertion is required. Triage operates only on "
            "machines the operator is authorized to service.",
        )

    capability = Capability(body.capability)
    journal = Journal(body.journal_path)
    if body.ssh and body.ssh.password:
        journal.register_secret(body.ssh.password)

    transport = _build_transport(body, capability)
    session = DiagnosticSession(
        transport,
        journal,
        target=TargetDescriptor(
            name=body.target_name or transport.target, notes=body.notes
        ),
        authorization=AuthorizationRecord(
            statement=body.authorization, asserted_by=body.asserted_by
        ),
        capability=capability,
        max_turns=body.max_turns,
    )
    handle = SessionHandle(session=session, journal=journal)
    SESSIONS[session.id] = handle

    session.approval_handler = _make_approval_handler(session.id)
    transport.observation_provider = _make_observation_provider(session.id)

    await session.start()
    handle.task = asyncio.create_task(session.run(body.instruction))

    return {
        "session_id": session.id,
        "target": session.target.describe(),
        "capability": capability.value,
        "dry_run": transport.dry_run,
        "events": f"/sessions/{session.id}/events",
    }


@app.get("/sessions")
async def list_sessions() -> list[dict[str, Any]]:
    return [
        {
            "session_id": sid,
            "target": h.session.target.describe(),
            "phase": h.session.phase.value,
            "capability": h.session.capability.value,
            "running": bool(h.task and not h.task.done()),
            "pending_approvals": len(h.approvals),
            "findings": len(h.session.findings),
        }
        for sid, h in SESSIONS.items()
    ]


@app.get("/sessions/{session_id}")
async def get_session(session_id: str) -> dict[str, Any]:
    handle = _handle(session_id)
    session = handle.session
    return {
        "session_id": session.id,
        "target": session.target.describe(),
        "transport": session.transport.describe().__dict__,
        "capability": session.capability.value,
        "phase": session.phase.value,
        "dry_run": session.transport.dry_run,
        "running": bool(handle.task and not handle.task.done()),
        "findings": serialize(session.findings),
        "remediations": serialize(session.approvals.all()),
        "report": serialize(session.report) if session.report else None,
    }


@app.get("/sessions/{session_id}/events")
async def stream_events(session_id: str) -> StreamingResponse:
    """Server-sent events for everything the session does, as it happens."""
    handle = _handle(session_id)
    queue = handle.session.events.subscribe()

    async def generator() -> Any:
        try:
            while True:
                event = await queue.get()
                if event is None:
                    yield "event: close\ndata: {}\n\n"
                    return
                yield f"event: {event.kind}\ndata: {json.dumps(event.to_dict())}\n\n"
        finally:
            handle.session.events.unsubscribe(queue)

    return StreamingResponse(generator(), media_type="text/event-stream")


@app.get("/sessions/{session_id}/remediations")
async def list_remediations(session_id: str) -> list[dict[str, Any]]:
    """Every remediation, with the snapshot plan for those awaiting a decision."""
    handle = _handle(session_id)
    out = []
    for remediation in handle.session.approvals.all():
        item = serialize(remediation)
        pending = handle.approvals.get(remediation.id)
        if pending is not None:
            item["awaiting_decision"] = True
            item["snapshot_plan"] = pending.plan.describe()
            item["rollback_available"] = pending.plan.has_rollback
        out.append(item)
    return out


@app.post("/sessions/{session_id}/remediations/{remediation_id}/decision")
async def decide(session_id: str, remediation_id: str, body: Decision) -> dict[str, Any]:
    """Approve or reject a pending change. The session is parked until this arrives."""
    handle = _handle(session_id)
    pending = handle.approvals.get(remediation_id)
    if pending is None:
        raise HTTPException(
            status_code=404,
            detail=f"Remediation {remediation_id} is not awaiting a decision in this session.",
        )
    if pending.future.done():  # pragma: no cover - double submit
        raise HTTPException(status_code=409, detail="This remediation was already decided.")

    if body.approved and not pending.plan.has_rollback and not body.acknowledge_no_rollback:
        raise HTTPException(
            status_code=409,
            detail=(
                f"{pending.plan.describe()} Approving it requires "
                "`acknowledge_no_rollback: true`."
            ),
        )

    pending.future.set_result(
        ApprovalDecision(
            approved=body.approved,
            approver=body.approver,
            reason=body.reason,
            acknowledge_no_rollback=body.acknowledge_no_rollback,
        )
    )
    return {"remediation_id": remediation_id, "decision": "approved" if body.approved else "rejected"}


@app.get("/sessions/{session_id}/observations")
async def list_observations(session_id: str) -> list[dict[str, Any]]:
    handle = _handle(session_id)
    return [
        {"id": key, "instruction": p.instruction, "expects": p.expects.value}
        for key, p in handle.observations.items()
    ]


@app.post("/sessions/{session_id}/observations/{observation_id}")
async def answer_observation(
    session_id: str, observation_id: str, body: ObservationAnswer
) -> dict[str, Any]:
    handle = _handle(session_id)
    pending = handle.observations.get(observation_id)
    if pending is None:
        raise HTTPException(status_code=404, detail=f"No pending observation {observation_id}")
    pending.future.set_result(
        Observation(
            instruction=pending.instruction,
            expects=pending.expects,
            value=body.value,
            media_path=body.media_path,
        )
    )
    return {"observation_id": observation_id, "accepted": True}


@app.get("/sessions/{session_id}/report")
async def get_report(session_id: str) -> dict[str, Any]:
    handle = _handle(session_id)
    if handle.session.report is None:
        raise HTTPException(status_code=404, detail="This session has not produced a report yet.")
    return serialize(handle.session.report)


@app.get("/sessions/{session_id}/journal")
async def get_journal(session_id: str) -> dict[str, Any]:
    """The append-only record, plus its integrity verdict."""
    handle = _handle(session_id)
    intact, bad_seq, note = handle.journal.verify()
    return {
        "intact": intact,
        "first_bad_entry": bad_seq,
        "note": note,
        "entries": [
            {
                "seq": e.seq,
                "at": e.at,
                "kind": e.kind,
                "payload": e.payload,
                "entry_hash": e.entry_hash,
            }
            for e in handle.journal.entries(session_id)
        ],
    }


@app.delete("/sessions/{session_id}", status_code=200)
async def close_session(session_id: str) -> dict[str, Any]:
    handle = _handle(session_id)
    if handle.task and not handle.task.done():
        handle.task.cancel()
    await handle.session.close()
    SESSIONS.pop(session_id, None)
    return {"session_id": session_id, "closed": True}
