"""Transport-level guarantees the base class owns, so no new transport can forget them."""

from __future__ import annotations

import asyncio

import pytest

from triage.core.authorization import _mint
from triage.core.capability import Capability
from triage.core.models import CommandResult, ObservationKind
from triage.transports.base import Transport, TransportError
from triage.transports.mock import MockResponse, MockTransport, faulty_workstation


class SlowTransport(MockTransport):
    """A target that stops answering — the failure mode of a machine that is dying."""

    async def _execute(self, command: str, timeout_s: float) -> CommandResult:
        await asyncio.sleep(10)
        return CommandResult(command=command, exit_code=0)


async def test_a_hung_target_does_not_hang_the_session() -> None:
    transport = SlowTransport(script=[], timeout_s=0.05)
    await transport.connect()
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(transport.run_read("df -h"), timeout=0.3)
    await transport.close()


async def test_output_is_capped_and_the_truncation_is_declared() -> None:
    transport = MockTransport(
        script=[MockResponse(r"flood", stdout="x" * 200_000)], max_output_bytes=1_000
    )
    await transport.connect()
    result = await transport.run_read("flood")

    assert result.truncated
    assert len(result.stdout) < 2_000
    assert "bytes omitted" in result.stdout
    assert "output truncated" in result.summary()
    await transport.close()


async def test_reads_run_for_real_in_a_dry_run_but_writes_do_not() -> None:
    """A rehearsal against imaginary state would not be a rehearsal."""
    transport = faulty_workstation(dry_run=True)
    await transport.connect()

    read = await transport.run_read("df -h")
    assert not read.simulated
    assert "Filesystem" in read.stdout

    authorization = _mint(
        remediation_id="rem_1",
        command="truncate -s 0 /var/log/ledger/debug.log",
        approved_by="operator",
    )
    write = await transport.run_write(authorization)
    assert write.simulated
    assert "NOT executed" in write.stdout
    assert not any("truncate" in c for c in transport.executed)
    await transport.close()


async def test_asking_a_human_without_a_human_channel_fails_loudly() -> None:
    transport = faulty_workstation()
    await transport.connect()
    with pytest.raises(TransportError, match="No human channel"):
        await transport.capture_observation("Is the PSU fan spinning?", ObservationKind.YES_NO)
    await transport.close()


async def test_an_observation_provider_relays_the_answer_back() -> None:
    async def provider(instruction: str, expects: ObservationKind):
        from triage.core.models import Observation

        return Observation(instruction=instruction, expects=expects, value="no, it is still")

    transport = faulty_workstation(observation_provider=provider)
    await transport.connect()
    observation = await transport.capture_observation(
        "Is the PSU fan spinning?", ObservationKind.YES_NO
    )
    assert observation.value == "no, it is still"
    assert transport.describe().supports_observation
    await transport.close()


async def test_the_mock_machine_changes_when_a_fix_is_applied() -> None:
    """Otherwise 'verify the fix worked' is a formality rather than a test."""
    transport = faulty_workstation()
    await transport.connect()

    before = await transport.run_read("df -h")
    assert "100% /var" in before.stdout

    await transport.run_write(
        _mint(
            remediation_id="rem_1",
            command="truncate -s 0 /var/log/ledger/debug.log",
            approved_by="operator",
        )
    )

    after = await transport.run_read("df -h")
    assert "100% /var" not in after.stdout
    assert "14% /var" in after.stdout
    await transport.close()


async def test_an_unscripted_command_reports_that_rather_than_pretending() -> None:
    transport = faulty_workstation()
    await transport.connect()
    result = await transport.run_read("lspci -k")
    assert result.exit_code == 127
    assert "no scripted response" in result.stderr
    await transport.close()


def test_the_transport_interface_carries_the_human_channel_from_day_one() -> None:
    """`capture_observation` is in the base interface so later tiers slot in unchanged."""
    for name in ("run_read", "run_write", "capture_observation", "snapshot", "describe"):
        assert hasattr(Transport, name)


async def test_ssh_credentials_are_not_exposed_by_describe() -> None:
    from triage.transports.ssh import SSHTransport

    transport = SSHTransport(
        host="10.0.0.9",
        username="root",
        password="hunter2",
        capability=Capability.EXECUTE_RO,
    )
    info = transport.describe()
    assert "hunter2" not in str(info.__dict__)
    assert info.target == "root@10.0.0.9:22"
    assert not info.reachable  # not connected yet
