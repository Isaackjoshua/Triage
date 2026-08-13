"""Transport — the load-bearing abstraction.

A transport is *how the brain reaches the patient*. The session, gate, journal, and agent
loop never learn which one is bound; they only ever talk to this interface. That is what
keeps one system instead of four as the tiers get deeper: at the executable tiers
``_execute`` runs a command over a wire, and at the wall it will mean "show a human what
to do and capture what they report back". Same loop, same gating, different far end.

Two methods carry the safety split:

* ``run_read`` takes a command string, and is only ever called after the gate classified
  it READ.
* ``run_write`` does **not** take a command string. It takes a `WriteAuthorization` and
  reads the command out of it, so there is no way to reach the write path without an
  approved remediation.

Subclasses implement ``_execute`` and, where they can, ``snapshot``. Everything else —
timeouts, output caps, dry-run simulation, single-use authorization — is handled here so
a new transport cannot forget it.
"""

from __future__ import annotations

import abc
from typing import Awaitable, Callable

from ..core.authorization import AuthorizationError, WriteAuthorization
from ..core.capability import Capability
from ..core.models import (
    CommandResult,
    Observation,
    ObservationKind,
    SnapshotRef,
    SnapshotKind,
    TransportInfo,
    utc_now,
)

DEFAULT_TIMEOUT_S = 60.0
DEFAULT_MAX_OUTPUT_BYTES = 64_000

#: Supplied by the client (CLI prompt, API queue) so a transport can ask a human for
#: something it cannot fetch. Minimal for SSH; it is the entire channel for the future
#: human-relay transport, which is why it is in the interface from day one.
ObservationProvider = Callable[[str, ObservationKind], Awaitable[Observation]]


class TransportError(RuntimeError):
    """Transport-level failure — unreachable target, auth failure, dropped connection."""


class Transport(abc.ABC):
    #: Human-readable transport name, e.g. "ssh".
    name: str = "transport"

    def __init__(
        self,
        target: str,
        capability: Capability = Capability.EXECUTE_RO,
        *,
        dry_run: bool = False,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
        observation_provider: ObservationProvider | None = None,
    ) -> None:
        self.target = target
        self.capability = capability
        self.dry_run = dry_run
        self.timeout_s = timeout_s
        self.max_output_bytes = max_output_bytes
        self.observation_provider = observation_provider
        self._connected = False

    # ------------------------------------------------------------------ lifecycle

    async def connect(self) -> TransportInfo:
        await self._connect()
        self._connected = True
        return self.describe()

    async def close(self) -> None:
        if self._connected:
            await self._close()
            self._connected = False

    async def __aenter__(self) -> "Transport":
        await self.connect()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    # ------------------------------------------------------------------ interface

    @abc.abstractmethod
    def describe(self) -> TransportInfo:
        """Capability level, reachability, and what this transport supports."""

    @abc.abstractmethod
    async def _execute(self, command: str, timeout_s: float) -> CommandResult:
        """Run a command on the far end. Subclass responsibility, never called directly."""

    async def _connect(self) -> None:
        """Establish the connection. Override where there is one to establish."""

    async def _close(self) -> None:
        """Tear the connection down. Override where there is one to tear down."""

    # ----------------------------------------------------------------- read path

    async def run_read(self, command: str, timeout_s: float | None = None) -> CommandResult:
        """Run a command the gate has already classified READ.

        Reads run in dry-run mode too — looking is free and risks nothing, and a dry run
        that could not see the machine would not be a useful rehearsal.
        """
        result = await self._execute(command, timeout_s or self.timeout_s)
        return self._cap_output(result)

    # ---------------------------------------------------------------- write path

    async def run_write(
        self, authorization: WriteAuthorization, timeout_s: float | None = None
    ) -> CommandResult:
        """Apply an already-approved mutating command.

        The only caller is the session's apply step, and the only way to obtain the
        argument is `ApprovalQueue.approve()` after a human said yes.
        """
        if not isinstance(authorization, WriteAuthorization):
            raise AuthorizationError(
                "run_write requires a WriteAuthorization minted by the approval flow, "
                f"not {type(authorization).__name__}."
            )
        command = authorization.consume()

        if self.dry_run or authorization.dry_run:
            return CommandResult(
                command=command,
                exit_code=0,
                stdout="[dry-run] command was NOT executed on the target.",
                simulated=True,
                started_at=utc_now(),
            )

        result = await self._execute(command, timeout_s or self.timeout_s)
        return self._cap_output(result)

    # ------------------------------------------------------------- human channel

    async def capture_observation(
        self, instruction: str, expects: ObservationKind = ObservationKind.TEXT
    ) -> Observation:
        """Ask a human for something this transport cannot fetch itself."""
        if self.observation_provider is None:
            raise TransportError(
                "No human channel is attached to this session, so an observation cannot be "
                "requested. Gather this with a read command instead, or attach a client "
                "that can relay the question."
            )
        return await self.observation_provider(instruction, expects)

    # ----------------------------------------------------------------- snapshots

    async def snapshot(self, scope: str) -> SnapshotRef | None:
        """Take a rollback point if the target supports one, else return None.

        Returning None is a legitimate, honest answer: the approval flow turns it into
        "no automatic rollback available" and asks the human to acknowledge that
        explicitly, rather than pretending a rollback exists.
        """
        if self.dry_run:
            return SnapshotRef(
                kind=SnapshotKind.SIMULATED,
                scope=scope,
                rollback_hint="[dry-run] no snapshot was taken; nothing was changed.",
            )
        return None

    # ------------------------------------------------------------------- helpers

    def _cap_output(self, result: CommandResult) -> CommandResult:
        """Truncate oversized output so one chatty command cannot swamp the session."""
        limit = self.max_output_bytes
        for field_name in ("stdout", "stderr"):
            text = getattr(result, field_name)
            if len(text) > limit:
                head = text[: limit // 2]
                tail = text[-limit // 2 :]
                setattr(
                    result,
                    field_name,
                    f"{head}\n\n...[{len(text) - limit} bytes omitted]...\n\n{tail}",
                )
                result.truncated = True
        return result
