"""The CLI: the inline approval prompt, and the read-only subcommands.

The approval prompt is the one place a human actually stands between the model and the
machine, so what it asks — and what it does with each answer — is worth testing directly.
"""

from __future__ import annotations

import pytest

from fakes import DynamicClient, Response, finalize, finding, propose, read

from triage.cli import main as cli
from triage.core.models import Remediation, RiskLevel, SnapshotKind
from triage.remediation.snapshot import SnapshotPlan


def _remediation() -> Remediation:
    return Remediation(
        command="truncate -s 0 /var/log/ledger/debug.log",
        rationale="df and du agree this file filled /var",
        expected_effect="/var drops to ~14%",
        rollback_plan="a copy is taken first",
        risk=RiskLevel.MEDIUM,
        finding_id="find_1",
    )


def _answers(monkeypatch, answers: list[str]) -> list[str]:
    """Feed scripted answers to the prompt and capture what it asked."""
    asked: list[str] = []
    queue = list(answers)

    async def fake_ask(prompt: str) -> str:
        asked.append(prompt)
        return queue.pop(0) if queue else ""

    monkeypatch.setattr(cli, "_ask", fake_ask)
    return asked


async def test_approving_a_protected_change(monkeypatch, capsys) -> None:
    _answers(monkeypatch, ["y", "the log is discardable"])
    plan = SnapshotPlan(SnapshotKind.FILE_BACKUP, "/var/log", "copy aside", ["/var/log/x"])

    decision = await cli.make_approval_handler("operator")(_remediation(), plan)

    assert decision.approved
    assert decision.reason == "the log is discardable"
    assert not decision.acknowledge_no_rollback

    shown = capsys.readouterr().out
    # The operator must be able to see what they are agreeing to.
    assert "truncate -s 0 /var/log/ledger/debug.log" in shown
    assert "df and du agree" in shown  # rationale, tied to evidence
    assert "/var drops to ~14%" in shown  # expected effect
    assert "a copy is taken first" in shown  # rollback plan
    assert "medium" in shown  # honest risk


async def test_declining_records_the_reason(monkeypatch) -> None:
    _answers(monkeypatch, ["n", "the log may be needed for the postmortem"])
    plan = SnapshotPlan(SnapshotKind.FILE_BACKUP, "/var/log", "copy aside", ["/var/log/x"])

    decision = await cli.make_approval_handler("operator")(_remediation(), plan)

    assert not decision.approved
    assert "postmortem" in decision.reason


async def test_anything_other_than_yes_is_a_no(monkeypatch) -> None:
    """The default must be to leave the machine alone."""
    for answer in ["", "  ", "no", "maybe", "Y E S", "sure"]:
        _answers(monkeypatch, [answer, ""])
        plan = SnapshotPlan(SnapshotKind.FILE_BACKUP, "/var", "copy aside", ["/var/x"])
        decision = await cli.make_approval_handler("operator")(_remediation(), plan)
        assert not decision.approved, f"{answer!r} was treated as approval"


async def test_an_unprotected_change_needs_a_second_deliberate_answer(
    monkeypatch, capsys
) -> None:
    """'Yes, run it' and 'yes, and nothing can be restored' are different decisions."""
    asked = _answers(monkeypatch, ["y", "no rollback", "accepted the risk"])
    plan = SnapshotPlan(SnapshotKind.NONE, "/", "nothing can be restored")

    decision = await cli.make_approval_handler("operator")(_remediation(), plan)

    assert decision.approved and decision.acknowledge_no_rollback
    assert len(asked) == 3, "the acknowledgement was not asked as its own question"

    shown = capsys.readouterr().out
    assert "NO AUTOMATIC ROLLBACK AVAILABLE" in shown
    assert "nothing to restore from" in shown


async def test_a_wrong_acknowledgement_does_not_approve(monkeypatch) -> None:
    _answers(monkeypatch, ["y", "yes"])  # 'yes' is not the required phrase
    plan = SnapshotPlan(SnapshotKind.NONE, "/", "nothing can be restored")

    decision = await cli.make_approval_handler("operator")(_remediation(), plan)

    assert not decision.approved
    assert "acknowledgement not given" in decision.reason


async def test_the_observation_prompt_relays_an_answer(monkeypatch) -> None:
    from triage.core.models import ObservationKind

    _answers(monkeypatch, ["no, the fan is not turning"])
    provider = cli.make_observation_provider()
    observation = await provider("Is the PSU fan spinning?", ObservationKind.YES_NO)
    assert observation.value == "no, the fan is not turning"


async def test_the_observation_prompt_accepts_a_photograph(monkeypatch) -> None:
    from triage.core.models import ObservationKind

    _answers(monkeypatch, ["/tmp/motherboard.jpg", "two caps near the CPU look domed"])
    provider = cli.make_observation_provider()
    observation = await provider("Photograph the board near the CPU.", ObservationKind.IMAGE)
    assert observation.media_path == "/tmp/motherboard.jpg"
    assert "domed" in observation.value


def test_catalog_subcommand_lists_the_read_surface(capsys) -> None:
    assert cli.main(["catalog"]) == 0
    shown = capsys.readouterr().out
    assert "smartctl" in shown and "journalctl" in shown
    assert "dd" not in shown.split("catalogued")[0].split("\n")[0]  # writes hidden by default

    assert cli.main(["catalog", "--all"]) == 0
    assert "dd" in capsys.readouterr().out


def test_verify_subcommand_reports_an_intact_journal(tmp_path, capsys) -> None:
    from triage.core.journal import Journal, JournalKind

    path = tmp_path / "j.sqlite"
    journal = Journal(path)
    journal.open_session("s1", "box", "mock", "EXECUTE_RW", "I own it")
    journal.record("s1", JournalKind.FINDING, symptom="/var full")
    journal.close()

    assert cli.main(["verify", "--journal", str(path)]) == 0
    assert "intact" in capsys.readouterr().out


def test_verify_subcommand_fails_loudly_on_a_tampered_journal(tmp_path, capsys) -> None:
    import sqlite3

    from triage.core.journal import Journal, JournalKind

    path = tmp_path / "j.sqlite"
    journal = Journal(path)
    journal.open_session("s1", "box", "mock", "EXECUTE_RW", "I own it")
    journal.record("s1", JournalKind.COMMAND, command="df -h")
    journal.close()

    conn = sqlite3.connect(str(path))
    conn.execute("DROP TRIGGER entries_no_update")
    conn.execute("UPDATE entries SET payload = '{}' WHERE seq = 2")
    conn.commit()
    conn.close()

    assert cli.main(["verify", "--journal", str(path)]) == 1
    assert "INTEGRITY FAILURE" in capsys.readouterr().out


def test_journal_subcommand_lists_sessions_and_entries(tmp_path, capsys) -> None:
    from triage.core.journal import Journal, JournalKind

    path = tmp_path / "j.sqlite"
    journal = Journal(path)
    journal.open_session("s1", "ws-14", "mock", "EXECUTE_RW", "I own it", dry_run=True)
    journal.record("s1", JournalKind.COMMAND, command="df -h")
    journal.close()

    cli.main(["journal", "--journal", str(path), "--list-sessions"])
    assert "ws-14" in capsys.readouterr().out

    cli.main(["journal", "--journal", str(path), "--session", "s1", "-v"])
    shown = capsys.readouterr().out
    assert "session_created" in shown and "df -h" in shown


def test_run_refuses_without_a_target(capsys) -> None:
    assert cli.main(["run", "--authorization", "I own it"]) == 2
    assert "--mock" in capsys.readouterr().out


def test_run_refuses_without_an_authorization_assertion(monkeypatch, capsys) -> None:
    monkeypatch.setattr("builtins.input", lambda *a: "")
    assert cli.main(["run", "--mock", "--authorization", ""]) == 2
    assert "No authorization asserted" in capsys.readouterr().out


def test_run_drives_a_full_session_and_prints_the_report(
    monkeypatch, tmp_path, capsys
) -> None:
    """The whole CLI path, with a scripted model standing in for the API."""
    _answers(monkeypatch, ["y", "the log is discardable"])

    original = cli.DiagnosticSession

    def factory(*args, **kwargs):
        holder: dict = {}
        kwargs["client"] = DynamicClient(
            [
                lambda s: Response([read("c1", "df -h", "filesystem usage")]),
                lambda s: Response(
                    [
                        finding(
                            "f1",
                            "/var is 100% full",
                            "df: /dev/sdb1 100% /var",
                            "an unrotated log filled it",
                            "high",
                            "software_fixable",
                        ),
                        finding(
                            "f2",
                            "/dev/sdb has 24 pending sectors",
                            "smartctl: Current_Pending_Sector 24",
                            "the disk is failing",
                            "high",
                            "hardware_suspected",
                        ),
                    ]
                ),
                lambda s: Response(
                    [
                        propose(
                            "p1",
                            "truncate -s 0 /var/log/ledger/debug.log",
                            s.findings[0].id,
                        )
                    ]
                ),
                lambda s: Response([read("c2", "df -h", "verify")]),
                lambda s: Response([finalize("r1")]),
                lambda s: Response([], stop_reason="end_turn"),
            ],
            lambda: holder["session"],
        )
        session = original(*args, **kwargs)
        holder["session"] = session
        return session

    monkeypatch.setattr(cli, "DiagnosticSession", factory)

    exit_code = cli.main(
        [
            "run",
            "--mock",
            "--authorization",
            "I administer this machine (asset #4412).",
            "--approver",
            "operator",
            "--journal",
            str(tmp_path / "cli.sqlite"),
        ]
    )
    assert exit_code == 0

    shown = capsys.readouterr().out
    assert "TRIAGE REPORT" in shown
    assert "SOFTWARE-FIXABLE" in shown
    assert "HARDWARE SUSPECTED" in shown
    assert "WHAT YOU MUST DO NEXT" in shown
    assert "journal chain intact" in shown


def test_dry_run_is_announced_prominently(monkeypatch, tmp_path, capsys) -> None:
    from triage.core.models import AuthorizationRecord, TargetDescriptor
    from triage.core.session import DiagnosticSession
    from triage.core.journal import Journal
    from triage.transports.mock import faulty_workstation

    session = DiagnosticSession(
        faulty_workstation(dry_run=True),
        Journal(tmp_path / "d.sqlite"),
        target=TargetDescriptor(name="ws-14"),
        authorization=AuthorizationRecord(statement="I own it", asserted_by="operator"),
    )
    cli._render(
        "session_started",
        {
            "session_id": session.id,
            "target": "ws-14",
            "transport": "mock",
            "capability": "EXECUTE_RW",
            "dry_run": True,
        },
    )
    assert "DRY RUN" in capsys.readouterr().out
