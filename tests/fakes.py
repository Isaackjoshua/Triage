"""A scripted stand-in for the Anthropic client, so the loop is testable without the API.

The session only ever touches `client.messages.create(...)` and the shape of what comes
back, so a small fake covering that shape exercises the real loop — real gate, real
journal, real approval flow — with a deterministic model.
"""

from __future__ import annotations

from typing import Any


class Block:
    """A content block: text, tool_use, or thinking."""

    def __init__(self, **fields: Any) -> None:
        self.__dict__.update(fields)


class Usage:
    def __init__(self) -> None:
        self.input_tokens = 100
        self.output_tokens = 50
        self.cache_read_input_tokens = 0
        self.cache_creation_input_tokens = 0


class Response:
    def __init__(self, content: list[Block], stop_reason: str = "tool_use") -> None:
        self.content = content
        self.stop_reason = stop_reason
        self.stop_details = None
        self.usage = Usage()


def text(body: str) -> Block:
    return Block(type="text", text=body)


def tool_use(call_id: str, name: str, payload: dict[str, Any]) -> Block:
    return Block(type="tool_use", id=call_id, name=name, input=payload)


def read(call_id: str, command: str, purpose: str = "gathering state") -> Block:
    return tool_use(call_id, "run_read_command", {"command": command, "purpose": purpose})


def finding(
    call_id: str,
    symptom: str,
    evidence: str,
    hypothesis: str,
    confidence: str,
    category: str,
) -> Block:
    return tool_use(
        call_id,
        "record_finding",
        {
            "symptom": symptom,
            "evidence": evidence,
            "hypothesis": hypothesis,
            "confidence": confidence,
            "category": category,
        },
    )


def propose(
    call_id: str,
    command: str,
    finding_id: str,
    *,
    risk: str = "medium",
    rationale: str = "evidence-backed",
    expected_effect: str = "the symptom clears",
    rollback_plan: str = "restore from the snapshot taken before the change",
) -> Block:
    return tool_use(
        call_id,
        "propose_remediation",
        {
            "command": command,
            "finding_id": finding_id,
            "rationale": rationale,
            "expected_effect": expected_effect,
            "rollback_plan": rollback_plan,
            "risk": risk,
        },
    )


def finalize(
    call_id: str,
    summary: str = "triage complete",
    confident_about: str = "the log filled the filesystem",
    uncertain_about: str = "how much life is left in the failing disk",
    human_next_steps: str = "replace /dev/sdb",
) -> Block:
    return tool_use(
        call_id,
        "finalize_report",
        {
            "summary": summary,
            "confident_about": confident_about,
            "uncertain_about": uncertain_about,
            "human_next_steps": human_next_steps,
        },
    )


class FakeMessages:
    def __init__(self, script: list[Response]) -> None:
        self.script = script
        self.calls: list[dict[str, Any]] = []
        self.index = 0

    async def create(self, **kwargs: Any) -> Response:
        self.calls.append(kwargs)
        response = self.script[min(self.index, len(self.script) - 1)]
        self.index += 1
        return response


class FakeClient:
    """Drop-in for AsyncAnthropic covering the surface the session uses."""

    def __init__(self, script: list[Response]) -> None:
        self.messages = FakeMessages(script)

    @property
    def calls(self) -> list[dict[str, Any]]:
        return self.messages.calls


class DynamicClient(FakeClient):
    """Builds each turn from the live session, for scripts that need real ids.

    A remediation must reference the finding that justifies it, and finding ids are
    generated at record time — so a fully static script cannot propose a real fix.
    """

    def __init__(self, turns: list[Any], session_getter: Any) -> None:
        super().__init__([])
        self.turns = turns
        self.session_getter = session_getter
        self.messages = _DynamicMessages(turns, session_getter)


class _DynamicMessages(FakeMessages):
    def __init__(self, turns: list[Any], session_getter: Any) -> None:
        super().__init__([])
        self.turns = turns
        self.session_getter = session_getter

    async def create(self, **kwargs: Any) -> Response:
        self.calls.append(kwargs)
        index = min(self.index, len(self.turns) - 1)
        self.index += 1
        turn = self.turns[index]
        return turn(self.session_getter()) if callable(turn) else turn
