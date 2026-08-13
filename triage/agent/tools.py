"""The tool schema the model gets, and the dispatch that backs it.

This is where the read/touch split is enforced *against the model*. `run_read_command`
consults the gate and executes only a READ verdict; anything else comes back as a refusal
that names the reason and points at `propose_remediation`. The refusal is a normal tool
result, not an error — the model is meant to read it, understand why, and route the change
correctly, which is the behaviour we want to reinforce.

`propose_remediation` never executes. It registers a pending change and returns
"pending human approval". There is no argument, flag, or phrasing that makes it apply.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..core.capability import Capability, CapabilityError
from ..core.models import (
    Confidence,
    Finding,
    FindingCategory,
    ObservationKind,
    Remediation,
    RiskLevel,
)
from ..remediation.approval import ApprovalError

if TYPE_CHECKING:  # pragma: no cover
    from ..core.session import DiagnosticSession


def tool_schema(capability: Capability) -> list[dict[str, Any]]:
    """The tools for this session. The write tool is absent below EXECUTE_RW.

    Withholding the schema is not the safety mechanism — the approval queue refuses the
    call regardless — but offering a tool that would always be refused just invites the
    model to waste turns discovering that.
    """
    tools: list[dict[str, Any]] = [
        {
            "name": "run_read_command",
            "description": (
                "Run a read-only diagnostic command on the target and return its output. "
                "Every command is classified before it runs: if it would change the "
                "machine's state — including redirection, shell chaining, or an "
                "unclassified binary — it is NOT executed, and you get a refusal "
                "explaining why. Route changes through propose_remediation instead. "
                "Looking is free: gather generously before concluding."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The exact command line to run, e.g. 'smartctl -a /dev/sdb'.",
                    },
                    "purpose": {
                        "type": "string",
                        "description": (
                            "What you are trying to learn, in one line. Journaled next to "
                            "the output so the operator can follow your reasoning."
                        ),
                    },
                },
                "required": ["command", "purpose"],
            },
        },
        {
            "name": "record_finding",
            "description": (
                "Record a structured finding as a hypothesis forms. Findings are the "
                "evidence trail: a remediation must reference the finding that justifies "
                "it, and the final report is assembled from them."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "symptom": {
                        "type": "string",
                        "description": "The observable problem, as the machine presents it.",
                    },
                    "evidence": {
                        "type": "string",
                        "description": (
                            "The specific output you actually received that supports this — "
                            "quote the relevant lines. Do not cite output you did not get."
                        ),
                    },
                    "hypothesis": {
                        "type": "string",
                        "description": "What you believe is happening, and why the evidence implies it.",
                    },
                    "confidence": {
                        "type": "string",
                        "enum": [c.value for c in Confidence],
                        "description": (
                            "Be honest. 'low' for a plausible hypothesis you have not "
                            "confirmed; 'high' only for a signature you recognise with "
                            "corroborating evidence."
                        ),
                    },
                    "category": {
                        "type": "string",
                        "enum": [c.value for c in FindingCategory],
                        "description": (
                            "software_fixable: addressable with a gated remediation here. "
                            "hardware_suspected: failing hardware; report it with a physical "
                            "next step. needs_human: a wrong change would be destructive and "
                            "you cannot safely reduce the uncertainty. informational: worth "
                            "surfacing but not itself a fault."
                        ),
                    },
                },
                "required": ["symptom", "evidence", "hypothesis", "confidence", "category"],
            },
        },
        {
            "name": "request_observation",
            "description": (
                "Ask the operator for something the transport cannot fetch: a physical "
                "check, a measurement, a yes/no, or a photograph. Use this when the answer "
                "is not on the far end of a command — what the POST beeps sound like, "
                "whether a capacitor is bulging, what the PSU reads under load."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "instruction": {
                        "type": "string",
                        "description": (
                            "One clear step, phrased for someone standing at the machine. "
                            "Ask for one thing at a time."
                        ),
                    },
                    "expects": {
                        "type": "string",
                        "enum": [k.value for k in ObservationKind],
                        "description": "The kind of answer you need back.",
                    },
                },
                "required": ["instruction", "expects"],
            },
        },
        {
            "name": "finalize_report",
            "description": (
                "Emit the triage report and end the diagnostic phase. Call this when the "
                "picture is complete — including when the honest picture is 'the evidence "
                "is ambiguous and here is what a human must check'."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "summary": {
                        "type": "string",
                        "description": "What you found, terse and evidence-first.",
                    },
                    "confident_about": {
                        "type": "string",
                        "description": "The conclusions the evidence actually supports.",
                    },
                    "uncertain_about": {
                        "type": "string",
                        "description": (
                            "What you could not establish, and what would settle it. "
                            "Leaving this empty is almost always wrong."
                        ),
                    },
                    "human_next_steps": {
                        "type": "string",
                        "description": (
                            "Concrete actions for the operator — physical checks, parts to "
                            "swap, commands to run themselves. Name components, not categories."
                        ),
                    },
                },
                "required": ["summary", "confident_about", "uncertain_about", "human_next_steps"],
            },
        },
    ]

    if capability.can_propose_writes:
        tools.insert(
            1,
            {
                "name": "propose_remediation",
                "description": (
                    "Propose a change to the machine. This does NOT execute anything — it "
                    "registers the change for a human to approve or reject. On approval the "
                    "system snapshots, applies, and returns the result to you for "
                    "verification. Propose one logical change at a time, the smallest that "
                    "addresses the finding, and be honest about risk and rollback: assume "
                    "this machine has no healthy backup."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "command": {
                            "type": "string",
                            "description": "The exact command to run, with no placeholders.",
                        },
                        "finding_id": {
                            "type": "string",
                            "description": (
                                "The id of the recorded finding this fixes. No fix without a "
                                "finding — if you have not established what is wrong, you are "
                                "not ready to propose a change."
                            ),
                        },
                        "rationale": {
                            "type": "string",
                            "description": (
                                "Why this command, tied to specific evidence you observed."
                            ),
                        },
                        "expected_effect": {
                            "type": "string",
                            "description": "What will be different afterwards, and how you will verify it.",
                        },
                        "rollback_plan": {
                            "type": "string",
                            "description": (
                                "How to undo this. If there is no real rollback, say so plainly "
                                "and mark the risk accordingly — do not disguise its absence."
                            ),
                        },
                        "risk": {
                            "type": "string",
                            "enum": [r.value for r in RiskLevel],
                            "description": (
                                "Honest risk to the machine if this is wrong — not your "
                                "confidence that it is right."
                            ),
                        },
                    },
                    "required": [
                        "command",
                        "finding_id",
                        "rationale",
                        "expected_effect",
                        "rollback_plan",
                        "risk",
                    ],
                },
            },
        )

    return tools


class ToolDispatcher:
    """Routes tool calls to the session. The session owns the transport, gate, and journal."""

    def __init__(self, session: "DiagnosticSession") -> None:
        self.session = session

    async def dispatch(self, name: str, payload: dict[str, Any]) -> tuple[str, bool]:
        """Execute one tool call. Returns (result_text, is_error)."""
        handler = {
            "run_read_command": self._run_read_command,
            "propose_remediation": self._propose_remediation,
            "record_finding": self._record_finding,
            "request_observation": self._request_observation,
            "finalize_report": self._finalize_report,
        }.get(name)

        if handler is None:
            return (f"Unknown tool '{name}'.", True)

        try:
            return await handler(payload)
        except CapabilityError as exc:
            return (str(exc), True)
        except ApprovalError as exc:
            return (str(exc), True)
        except Exception as exc:  # surfaced to the model so it can adapt, and journaled
            self.session.journal_error(f"tool {name} failed: {exc}")
            return (f"The {name} tool failed: {exc}", True)

    # ------------------------------------------------------------------ handlers

    async def _run_read_command(self, payload: dict[str, Any]) -> tuple[str, bool]:
        command = str(payload.get("command", "")).strip()
        purpose = str(payload.get("purpose", ""))
        result, decision = await self.session.run_read(command, purpose)

        if result is None:
            return (
                f"REFUSED — not executed. {decision.reason}\n\n"
                "This command was classified "
                f"{decision.classification.value}. Reads must not change the machine. If you "
                "want this change made, call propose_remediation with it.",
                False,  # a refusal is information, not a tool failure
            )

        return (_format_result(result), False)

    async def _propose_remediation(self, payload: dict[str, Any]) -> tuple[str, bool]:
        remediation = Remediation(
            command=str(payload["command"]).strip(),
            finding_id=str(payload.get("finding_id") or "") or None,
            rationale=str(payload.get("rationale", "")),
            expected_effect=str(payload.get("expected_effect", "")),
            rollback_plan=str(payload.get("rollback_plan", "")),
            risk=RiskLevel(payload.get("risk", RiskLevel.HIGH.value)),
        )
        registered = self.session.propose_remediation(remediation)
        return (
            f"Registered {registered.id} — pending human approval. It has NOT been applied.\n"
            f"  command: {registered.command}\n"
            f"  risk: {registered.risk.value}\n"
            "You will receive the result once a human decides. Continue gathering evidence "
            "or finalize; do not re-propose this change while it is pending.",
            False,
        )

    async def _record_finding(self, payload: dict[str, Any]) -> tuple[str, bool]:
        finding = Finding(
            symptom=str(payload.get("symptom", "")),
            evidence=str(payload.get("evidence", "")),
            hypothesis=str(payload.get("hypothesis", "")),
            confidence=Confidence(payload.get("confidence", Confidence.LOW.value)),
            category=FindingCategory(payload.get("category", FindingCategory.INFORMATIONAL.value)),
        )
        recorded = self.session.record_finding(finding)
        return (
            f"Recorded {recorded.id} ({recorded.category.value}, confidence "
            f"{recorded.confidence.value}). Reference this id when proposing a fix for it.",
            False,
        )

    async def _request_observation(self, payload: dict[str, Any]) -> tuple[str, bool]:
        observation = await self.session.request_observation(
            instruction=str(payload.get("instruction", "")),
            expects=ObservationKind(payload.get("expects", ObservationKind.TEXT.value)),
        )
        answer = observation.value or "(no answer given)"
        if observation.media_path:
            answer += f"\n[image provided at {observation.media_path}]"
        return (f"The operator reports: {answer}", False)

    async def _finalize_report(self, payload: dict[str, Any]) -> tuple[str, bool]:
        report = self.session.finalize_report(
            summary=str(payload.get("summary", "")),
            confident_about=str(payload.get("confident_about", "")),
            uncertain_about=str(payload.get("uncertain_about", "")),
            human_next_steps=str(payload.get("human_next_steps", "")),
        )
        return (
            f"Triage report recorded with {len(report.findings)} finding(s). "
            "The diagnostic phase is complete.",
            False,
        )


def _format_result(result: Any) -> str:
    """Render a CommandResult for the model: status first, then the actual output."""
    parts = [f"$ {result.command}", f"[{result.summary()}]"]
    if result.stdout.strip():
        parts.append(result.stdout.rstrip())
    if result.stderr.strip():
        parts.append(f"--- stderr ---\n{result.stderr.rstrip()}")
    if not result.stdout.strip() and not result.stderr.strip():
        parts.append("(no output)")
    return "\n".join(parts)
