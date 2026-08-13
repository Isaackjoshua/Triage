"""Domain model for a diagnostic session.

Everything the session, gate, journal, and agent loop pass around lives here as a
plain dataclass or enum. Nothing in this module knows which transport is bound, and
nothing here talks to the network — that is the point: the same `Finding` comes back
whether it was produced over SSH, from a mock, or (later) from a human reading a
multimeter.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def utc_now() -> str:
    """An ISO-8601 UTC timestamp. Every record in the system is stamped with one."""
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def serialize(value: Any) -> Any:
    """Convert dataclasses/enums into JSON-safe primitives for the journal and API."""
    if is_dataclass(value) and not isinstance(value, type):
        return {k: serialize(v) for k, v in asdict(value).items()}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {k: serialize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [serialize(v) for v in value]
    return value


# --------------------------------------------------------------------------------------
# Enums
# --------------------------------------------------------------------------------------


class Classification(str, Enum):
    """What the gate decided a command is.

    UNKNOWN is not a third outcome the caller may treat leniently — the gate's policy
    is that UNKNOWN is handled exactly as WRITE. It exists as a distinct value only so
    the journal records *why* something was refused.
    """

    READ = "READ"
    WRITE = "WRITE"
    UNKNOWN = "UNKNOWN"


class FindingCategory(str, Enum):
    SOFTWARE_FIXABLE = "software_fixable"
    HARDWARE_SUSPECTED = "hardware_suspected"
    NEEDS_HUMAN = "needs_human"
    INFORMATIONAL = "informational"


class Confidence(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class RemediationStatus(str, Enum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"
    APPLIED = "applied"
    VERIFIED = "verified"
    FAILED = "failed"


class SessionPhase(str, Enum):
    DIAGNOSE = "DIAGNOSE"
    REMEDIATE = "REMEDIATE"
    COMPLETE = "COMPLETE"


class SnapshotKind(str, Enum):
    BTRFS = "btrfs"
    ZFS = "zfs"
    LVM = "lvm"
    FILE_BACKUP = "file_backup"
    NONE = "none"
    SIMULATED = "simulated"


class ObservationKind(str, Enum):
    TEXT = "text"
    MEASUREMENT = "measurement"
    IMAGE = "image"
    YES_NO = "yes_no"


# --------------------------------------------------------------------------------------
# Target and authorization
# --------------------------------------------------------------------------------------


@dataclass
class TargetDescriptor:
    """What machine we are working on, in words the operator and the model both read."""

    name: str
    os_family: str = "linux"
    notes: str = ""

    def describe(self) -> str:
        base = f"{self.name} ({self.os_family})"
        return f"{base} — {self.notes}" if self.notes else base


@dataclass
class AuthorizationRecord:
    """The operator's assertion that they are permitted to service this target.

    Journaled at session creation. The system does not verify the claim — it records
    it, so that the session is attributable after the fact.
    """

    statement: str
    asserted_by: str
    asserted_at: str = field(default_factory=utc_now)

    def describe(self) -> str:
        return f'{self.asserted_by} asserted at {self.asserted_at}: "{self.statement}"'


# --------------------------------------------------------------------------------------
# Transport payloads
# --------------------------------------------------------------------------------------


@dataclass
class TransportInfo:
    """What a bound transport is and what it can do — reported by `Transport.describe()`."""

    name: str
    capability: str
    reachable: bool
    target: str
    supports_snapshot: bool = False
    supports_observation: bool = False
    detail: str = ""


@dataclass
class CommandResult:
    command: str
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    duration_s: float = 0.0
    timed_out: bool = False
    truncated: bool = False
    simulated: bool = False
    started_at: str = field(default_factory=utc_now)

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    def summary(self) -> str:
        bits = [f"exit={self.exit_code}", f"{self.duration_s:.2f}s"]
        if self.timed_out:
            bits.append("TIMED OUT")
        if self.truncated:
            bits.append("output truncated")
        if self.simulated:
            bits.append("SIMULATED (dry-run)")
        return ", ".join(bits)


@dataclass
class Observation:
    """Something a human reported that the transport could not fetch itself.

    Minimal for SSH; it is the whole channel for the future human-relay transport, which
    is why it carries an image path from day one.
    """

    instruction: str
    expects: ObservationKind
    value: str = ""
    media_path: str | None = None
    observed_at: str = field(default_factory=utc_now)
    id: str = field(default_factory=lambda: new_id("obs"))


@dataclass
class SnapshotRef:
    """A rollback point, or an honest record that none was possible."""

    kind: SnapshotKind
    scope: str
    rollback_hint: str
    id: str = field(default_factory=lambda: new_id("snap"))
    created_at: str = field(default_factory=utc_now)
    detail: str = ""

    @property
    def is_rollback_possible(self) -> bool:
        return self.kind not in (SnapshotKind.NONE, SnapshotKind.SIMULATED)


# --------------------------------------------------------------------------------------
# Agent output
# --------------------------------------------------------------------------------------


@dataclass
class Finding:
    symptom: str
    evidence: str
    hypothesis: str
    confidence: Confidence
    category: FindingCategory
    id: str = field(default_factory=lambda: new_id("find"))
    recorded_at: str = field(default_factory=utc_now)


@dataclass
class Remediation:
    """A proposed change to the machine. The agent creates these; it never applies them.

    `finding_id` is not optional in spirit — "no fix without a finding" — but it is
    validated at the tool layer so the model gets a usable error rather than a crash.
    """

    command: str
    rationale: str
    expected_effect: str
    rollback_plan: str
    risk: RiskLevel
    finding_id: str | None = None
    id: str = field(default_factory=lambda: new_id("rem"))
    status: RemediationStatus = RemediationStatus.PROPOSED
    proposed_at: str = field(default_factory=utc_now)

    # Filled in as the remediation moves through the approval flow.
    decided_at: str | None = None
    decided_by: str | None = None
    decision_reason: str | None = None
    snapshot: SnapshotRef | None = None
    result: CommandResult | None = None
    verification: str | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status in (
            RemediationStatus.REJECTED,
            RemediationStatus.VERIFIED,
            RemediationStatus.FAILED,
        )


@dataclass
class TriageReport:
    """The structured hand-off the session ends on."""

    summary: str
    findings: list[Finding]
    remediations: list[Remediation]
    confident_about: str = ""
    uncertain_about: str = ""
    human_next_steps: str = ""
    generated_at: str = field(default_factory=utc_now)

    def by_category(self, category: FindingCategory) -> list[Finding]:
        return [f for f in self.findings if f.category is category]
