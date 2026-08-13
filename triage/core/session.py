"""DiagnosticSession — one engagement against one target, and the loop that drives it.

The session owns the transport, the gate, the journal, the approval queue, and the
conversation with the model. Its job is to keep the loop honest:

* Read tool calls go through the gate before the transport sees them, and a refusal is
  handed back to the model as information rather than raised as an error.
* Write tool calls never execute. They register a proposal, the loop pauses, a human
  decides, and only then does the session snapshot → apply → feed the result back for
  verification.
* Everything — command, classification, result, finding, proposal, decision, snapshot,
  outcome — is journaled as it happens, so the session is reconstructable afterwards
  from a record the agent could not have edited.

The loop itself is transport-agnostic. It only ever talks to tools, and the tools talk to
whatever transport is bound.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from ..agent.catalog import Catalog, default_catalog
from ..agent.prompts import build_system_prompt
from ..agent.tools import ToolDispatcher, tool_schema
from ..remediation.approval import ApprovalQueue, NoRollbackAcknowledgementRequired
from ..remediation.snapshot import SnapshotManager, SnapshotPlan
from ..transports.base import Transport
from .authorization import WriteAuthorization
from .capability import Capability, require_execute
from .events import EventBus
from .gate import CommandGate, GateDecision
from .journal import Journal, JournalKind
from .models import (
    AuthorizationRecord,
    CommandResult,
    Finding,
    Observation,
    ObservationKind,
    Remediation,
    RemediationStatus,
    SessionPhase,
    TargetDescriptor,
    TriageReport,
    new_id,
    serialize,
)

DEFAULT_MODEL = os.environ.get("TRIAGE_MODEL", "claude-opus-5")
DEFAULT_EFFORT = os.environ.get("TRIAGE_EFFORT", "high")
DEFAULT_MAX_TOKENS = int(os.environ.get("TRIAGE_MAX_TOKENS", "16000"))
DEFAULT_MAX_TURNS = int(os.environ.get("TRIAGE_MAX_TURNS", "40"))


@dataclass
class ApprovalDecision:
    """What a human decided about one proposed change."""

    approved: bool
    approver: str = "operator"
    reason: str = ""
    acknowledge_no_rollback: bool = False


#: Supplied by the client. Shown the proposal and the snapshot plan; returns the decision.
ApprovalHandler = Callable[[Remediation, SnapshotPlan], Awaitable[ApprovalDecision]]


class DiagnosticSession:
    def __init__(
        self,
        transport: Transport,
        journal: Journal,
        *,
        target: TargetDescriptor,
        authorization: AuthorizationRecord,
        capability: Capability | None = None,
        approval_handler: ApprovalHandler | None = None,
        observation_provider: Any = None,
        catalog: Catalog | None = None,
        model: str = DEFAULT_MODEL,
        effort: str = DEFAULT_EFFORT,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        max_turns: int = DEFAULT_MAX_TURNS,
        client: Any = None,
        session_id: str | None = None,
    ) -> None:
        self.id = session_id or new_id("sess")
        self.transport = transport
        self.journal = journal
        self.target = target
        self.authorization = authorization
        self.capability = capability or transport.capability
        self.catalog = catalog or default_catalog()
        self.gate = CommandGate(self.catalog)
        self.events = EventBus()
        self.phase = SessionPhase.DIAGNOSE

        self.findings: list[Finding] = []
        self.observations: list[Observation] = []
        self.report: TriageReport | None = None

        self.approvals = ApprovalQueue(journal, self.id, self.capability)
        self.snapshots = SnapshotManager(transport, journal, self.id)
        self.dispatcher = ToolDispatcher(self)
        self.approval_handler = approval_handler

        if observation_provider is not None:
            transport.observation_provider = observation_provider

        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens
        self.max_turns = max_turns
        self._client = client
        self._messages: list[dict[str, Any]] = []

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        """Connect the transport and open the journal record for this session."""
        info = await self.transport.connect()
        self.journal.open_session(
            self.id,
            target=self.target.describe(),
            transport=f"{info.name} -> {info.target}",
            capability=self.capability.value,
            authorization=self.authorization.describe(),
            dry_run=self.transport.dry_run,
        )
        self.journal.record(
            self.id,
            JournalKind.AUTHORIZATION_ASSERTED,
            statement=self.authorization.statement,
            asserted_by=self.authorization.asserted_by,
            asserted_at=self.authorization.asserted_at,
        )
        self.journal.record(self.id, JournalKind.TRANSPORT_BOUND, transport=info)
        self.events.emit(
            "session_started",
            session_id=self.id,
            target=self.target.describe(),
            transport=info.name,
            capability=self.capability.value,
            dry_run=self.transport.dry_run,
        )

    async def close(self) -> None:
        await self.transport.close()
        self.events.close()

    # --------------------------------------------------------------- tool backing

    async def run_read(
        self, command: str, purpose: str = ""
    ) -> tuple[CommandResult | None, GateDecision]:
        """Classify, then execute only if the verdict is READ.

        Returning `(None, decision)` is the refusal path — the caller turns the reason
        into something the model can act on. Nothing reached the transport.
        """
        require_execute(self.capability, "Running a command")
        decision = self.gate.classify(command)

        if decision.needs_approval:
            self.journal.record(
                self.id,
                JournalKind.COMMAND_REFUSED,
                command=command,
                purpose=purpose,
                classification=decision.classification.value,
                reason=decision.reason,
            )
            self.events.emit(
                "command_refused",
                command=command,
                classification=decision.classification.value,
                reason=decision.reason,
            )
            return (None, decision)

        self.events.emit("command_started", command=command, purpose=purpose)
        result = await self.transport.run_read(command)
        self.journal.record(
            self.id,
            JournalKind.COMMAND,
            command=command,
            purpose=purpose,
            classification=decision.classification.value,
            exit_code=result.exit_code,
            duration_s=result.duration_s,
            timed_out=result.timed_out,
            truncated=result.truncated,
            stdout=result.stdout,
            stderr=result.stderr,
        )
        self.events.emit("command_finished", command=command, summary=result.summary())
        return (result, decision)

    def record_finding(self, finding: Finding) -> Finding:
        self.findings.append(finding)
        self.journal.record(self.id, JournalKind.FINDING, finding=finding)
        self.events.emit("finding", finding=serialize(finding))
        return finding

    def propose_remediation(self, remediation: Remediation) -> Remediation:
        registered = self.approvals.propose(remediation)
        self.events.emit("remediation_proposed", remediation=serialize(registered))
        return registered

    async def request_observation(
        self, instruction: str, expects: ObservationKind = ObservationKind.TEXT
    ) -> Observation:
        self.events.emit("observation_requested", instruction=instruction, expects=expects.value)
        observation = await self.transport.capture_observation(instruction, expects)
        self.observations.append(observation)
        self.journal.record(self.id, JournalKind.OBSERVATION, observation=observation)
        self.events.emit("observation_received", observation=serialize(observation))
        return observation

    def finalize_report(
        self,
        summary: str,
        confident_about: str = "",
        uncertain_about: str = "",
        human_next_steps: str = "",
    ) -> TriageReport:
        report = TriageReport(
            summary=summary,
            findings=list(self.findings),
            remediations=self.approvals.all(),
            confident_about=confident_about,
            uncertain_about=uncertain_about,
            human_next_steps=human_next_steps,
        )
        self.report = report
        self.phase = SessionPhase.COMPLETE
        self.journal.record(self.id, JournalKind.REPORT, report=report)
        self.events.emit("report", report=serialize(report))
        return report

    def journal_error(self, message: str) -> None:
        self.journal.record(self.id, JournalKind.ERROR, message=message)
        self.events.emit("error", message=message)

    # ------------------------------------------------------------- the write path

    async def resolve_pending(self) -> list[str]:
        """Put every pending proposal to a human, then snapshot/apply/verify the approved.

        Returns human-readable outcome blocks to feed back into the conversation, so the
        model can verify its own fix rather than assume it worked.
        """
        outcomes: list[str] = []
        for remediation in self.approvals.pending():
            outcomes.append(await self._decide_and_apply(remediation))
        return outcomes

    async def _decide_and_apply(self, remediation: Remediation) -> str:
        if self.approval_handler is None:
            self.approvals.reject(
                remediation.id,
                "system",
                "No approval channel is attached to this session, so no change can be applied.",
            )
            return (
                f"{remediation.id} was NOT applied: this session has no approval channel, so "
                "no change can be made. Report the command for a human to run themselves."
            )

        self.phase = SessionPhase.REMEDIATE
        plan = await self.snapshots.plan(remediation)
        self.events.emit(
            "approval_requested",
            remediation=serialize(remediation),
            snapshot_plan=plan.describe(),
            rollback_available=plan.has_rollback,
        )

        decision = await self.approval_handler(remediation, plan)
        if not decision.approved:
            self.approvals.reject(remediation.id, decision.approver, decision.reason)
            self.events.emit(
                "remediation_rejected", remediation_id=remediation.id, reason=decision.reason
            )
            return (
                f"REMEDIATION {remediation.id} REJECTED by {decision.approver}.\n"
                f"  command: {remediation.command}\n"
                f"  reason: {decision.reason or '(none given)'}\n"
                "It was not applied and the machine is unchanged. Take the reason into "
                "account — do not simply re-propose the same change."
            )

        # Snapshot before mutate. This runs after approval and immediately before the write.
        snapshot = await self.snapshots.protect(remediation, plan)

        try:
            authorization: WriteAuthorization = self.approvals.approve(
                remediation.id,
                decision.approver,
                plan=plan,
                snapshot=snapshot,
                reason=decision.reason,
                acknowledge_no_rollback=decision.acknowledge_no_rollback,
                dry_run=self.transport.dry_run,
            )
        except NoRollbackAcknowledgementRequired as exc:
            self.approvals.reject(
                remediation.id,
                decision.approver,
                "Approved, but the required no-rollback acknowledgement was not given.",
            )
            self.events.emit("remediation_rejected", remediation_id=remediation.id, reason=str(exc))
            return (
                f"REMEDIATION {remediation.id} was NOT applied: {exc}\n"
                "The machine is unchanged."
            )

        result = await self.transport.run_write(authorization)
        remediation.result = result
        remediation.status = (
            RemediationStatus.APPLIED if result.ok else RemediationStatus.FAILED
        )
        self.journal.record(
            self.id,
            JournalKind.REMEDIATION_APPLIED,
            remediation_id=remediation.id,
            command=result.command,
            exit_code=result.exit_code,
            stdout=result.stdout,
            stderr=result.stderr,
            simulated=result.simulated,
            snapshot=snapshot,
        )
        self.events.emit(
            "remediation_applied",
            remediation_id=remediation.id,
            summary=result.summary(),
            ok=result.ok,
        )

        rollback_line = (
            snapshot.rollback_hint
            if snapshot is not None and snapshot.rollback_hint
            else "no rollback point exists for this change"
        )
        status_line = (
            "The command exited cleanly."
            if result.ok
            else "The command FAILED — treat the change as not made, and re-plan."
        )
        return (
            f"REMEDIATION {remediation.id} APPLIED by {decision.approver}.\n"
            f"  command: {result.command}\n"
            f"  result: {result.summary()}\n"
            f"  stdout: {result.stdout.strip() or '(none)'}\n"
            f"  stderr: {result.stderr.strip() or '(none)'}\n"
            f"  rollback: {rollback_line}\n"
            f"{status_line}\n"
            "Now VERIFY: re-run the relevant read commands and confirm the symptom is "
            "actually gone and nothing new broke. Do not report success you have not checked."
        )

    def mark_verified(self, remediation_id: str, verification: str) -> None:
        remediation = self.approvals.get(remediation_id)
        if remediation is None:
            return
        remediation.status = RemediationStatus.VERIFIED
        remediation.verification = verification
        self.journal.record(
            self.id,
            JournalKind.REMEDIATION_VERIFIED,
            remediation_id=remediation_id,
            verification=verification,
        )

    # ------------------------------------------------------------------ the loop

    async def run(self, instruction: str | None = None) -> TriageReport | None:
        """Drive the agent loop until the model finalizes, or the turn budget runs out."""
        client = self._ensure_client()
        system = build_system_prompt(
            target_descriptor=self.target.describe(),
            transport=f"{self.transport.name} ({self.transport.target})",
            capability=self.capability,
            authorization_record=self.authorization.describe(),
            catalog=self.catalog,
        )
        tools = tool_schema(self.capability)

        self._messages = [{"role": "user", "content": instruction or _default_instruction(self)}]
        idle_turns = 0

        for turn in range(self.max_turns):
            response = await client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=system,
                tools=tools,
                thinking={"type": "adaptive"},
                output_config={"effort": self.effort},
                messages=self._messages,
            )

            self.journal.record(
                self.id,
                JournalKind.MODEL_TURN,
                turn=turn,
                stop_reason=response.stop_reason,
                usage=getattr(response, "usage", None) and serialize_usage(response.usage),
            )

            # Preserve the assistant turn verbatim — thinking blocks included — so the
            # model's own reasoning carries forward correctly across tool calls.
            self._messages.append({"role": "assistant", "content": response.content})

            for block in response.content:
                if getattr(block, "type", None) == "text" and block.text.strip():
                    self.events.emit("assistant_text", text=block.text)

            if response.stop_reason == "refusal":
                details = getattr(response, "stop_details", None)
                message = "The model declined this request"
                if details is not None:
                    message += f" ({getattr(details, 'category', None)})"
                self.journal_error(message)
                return self.report

            if response.stop_reason == "pause_turn":
                # A server-side tool hit its iteration limit; re-send to resume.
                continue

            if response.stop_reason == "max_tokens":
                self._messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Your previous turn hit the output limit. Continue from where you "
                            "stopped, more concisely."
                        ),
                    }
                )
                continue

            tool_uses = [b for b in response.content if getattr(b, "type", None) == "tool_use"]
            if not tool_uses:
                if self.report is not None:
                    return self.report
                # The model stopped without finalizing. Nudge it once; if it does the same
                # thing again, stop rather than trading prose until the turn budget runs out.
                idle_turns += 1
                if idle_turns > 1:
                    self.journal_error(
                        "Session ended without a final report: the model stopped calling "
                        "tools and did not finalize when prompted."
                    )
                    return self.report
                self._messages.append(
                    {
                        "role": "user",
                        "content": (
                            "You ended a turn without calling a tool. Either continue "
                            "gathering evidence, or call finalize_report with what you have — "
                            "including, if that is the honest picture, that the evidence is "
                            "ambiguous and what a human must check."
                        ),
                    }
                )
                continue

            idle_turns = 0

            tool_results = []
            for block in tool_uses:
                self.events.emit("tool_call", name=block.name, input=serialize(block.input))
                text, is_error = await self.dispatcher.dispatch(block.name, dict(block.input))
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": text,
                        **({"is_error": True} if is_error else {}),
                    }
                )

            # All results for one assistant turn go back in a single user message.
            self._messages.append({"role": "user", "content": tool_results})

            if self.report is not None:
                return self.report

            # A proposal pauses the loop for a human before anything else happens.
            if self.approvals.has_pending():
                outcomes = await self.resolve_pending()
                if outcomes:
                    self._messages.append({"role": "user", "content": "\n\n".join(outcomes)})

        self.journal_error(
            f"Session reached the {self.max_turns}-turn budget without a final report."
        )
        return self.report

    # -------------------------------------------------------------------- private

    def _ensure_client(self) -> Any:
        if self._client is None:
            try:
                from anthropic import AsyncAnthropic
            except ImportError as exc:  # pragma: no cover
                raise RuntimeError(
                    "The anthropic SDK is required to run the agent loop. "
                    "Install it with `pip install anthropic`."
                ) from exc
            self._client = AsyncAnthropic()
        return self._client


def serialize_usage(usage: Any) -> dict[str, Any]:
    fields = (
        "input_tokens",
        "output_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
    )
    return {f: getattr(usage, f, None) for f in fields}


def _default_instruction(session: DiagnosticSession) -> str:
    lines = [
        f"Triage {session.target.describe()}.",
        "",
        "Start by building a real picture of the machine's state — storage health, "
        "filesystem usage, failed units, kernel errors, sensors, memory. Then reason from "
        "what you actually observe.",
    ]
    if session.transport.dry_run:
        lines += [
            "",
            "This session is a DRY RUN: reads are live against the target, but any approved "
            "change is simulated rather than executed. Propose remediations exactly as you "
            "otherwise would.",
        ]
    if not session.capability.can_propose_writes:
        lines += [
            "",
            f"You are at {session.capability.value}: diagnose and report only. Where a fix "
            "exists, give the exact command a human could run themselves, with the same "
            "rationale, rollback, and risk detail.",
        ]
    return "\n".join(lines)
