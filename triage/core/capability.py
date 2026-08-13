"""Capability: what the agent is allowed to attempt on the bound transport.

Capability is checked in code at every gate, not just described to the model in the
system prompt. A session bound at EXECUTE_RO cannot reach the write path even if the
model asks for it — the approval queue refuses to register the proposal at all.
"""

from __future__ import annotations

from enum import Enum


class Capability(str, Enum):
    #: Full loop: gather, find, propose, verify, report. Writes still need human approval.
    EXECUTE_RW = "EXECUTE_RW"
    #: Diagnose and report only. The agent produces exact commands a human could run
    #: themselves, but nothing is gated for approval and nothing is applied.
    EXECUTE_RO = "EXECUTE_RO"
    #: No execution on the machine at all. The agent works entirely through a human.
    ADVISE_ONLY = "ADVISE_ONLY"

    @property
    def can_execute(self) -> bool:
        """May the agent run commands on the target at all (read path included)?"""
        return self in (Capability.EXECUTE_RW, Capability.EXECUTE_RO)

    @property
    def can_propose_writes(self) -> bool:
        """May the agent register remediations for human approval?"""
        return self is Capability.EXECUTE_RW

    def describe(self) -> str:
        return _DESCRIPTIONS[self]


_DESCRIPTIONS: dict[Capability, str] = {
    Capability.EXECUTE_RW: "you may propose fixes for a human to approve",
    Capability.EXECUTE_RO: "diagnose and report only",
    Capability.ADVISE_ONLY: "no execution; you work through a human",
}


class CapabilityError(PermissionError):
    """Raised when something is attempted above the session's capability level."""


def require_execute(capability: Capability, what: str) -> None:
    if not capability.can_execute:
        raise CapabilityError(
            f"{what} requires an executable transport; this session is {capability.value}. "
            "Use request_observation to ask the human for this instead."
        )


def require_write(capability: Capability, what: str) -> None:
    if not capability.can_propose_writes:
        raise CapabilityError(
            f"{what} requires {Capability.EXECUTE_RW.value}; this session is {capability.value}. "
            "Report the exact command for a human to run themselves instead."
        )
