"""The MVP client: create a session, stream the agent's activity, approve inline.

Approvals happen at the terminal, in front of the person who is responsible for the
machine. The prompt shows the exact command, the rationale tied to evidence, the expected
effect, the rollback plan, the risk, and — separately and prominently — whether a rollback
point actually exists. When it does not, the second confirmation is a distinct question
with its own answer, because "yes, run it" and "yes, and I accept that nothing can be
restored" are different decisions.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys
from typing import Any

from ..core.capability import Capability
from ..core.journal import Journal
from ..core.models import (
    AuthorizationRecord,
    Observation,
    ObservationKind,
    Remediation,
    TargetDescriptor,
)
from ..core.session import ApprovalDecision, DiagnosticSession
from ..remediation.snapshot import SnapshotPlan
from ..transports.base import Transport
from ..transports.mock import faulty_workstation

# ------------------------------------------------------------------------------ output

_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


def dim(t: str) -> str:
    return _c(t, "2")


def bold(t: str) -> str:
    return _c(t, "1")


def red(t: str) -> str:
    return _c(t, "31")


def green(t: str) -> str:
    return _c(t, "32")


def yellow(t: str) -> str:
    return _c(t, "33")


def cyan(t: str) -> str:
    return _c(t, "36")


def rule(title: str = "") -> None:
    width = 78
    if title:
        print(f"\n{bold('── ' + title + ' ')}{'─' * max(0, width - len(title) - 4)}")
    else:
        print("─" * width)


# ------------------------------------------------------------------- event rendering


async def render_events(session: DiagnosticSession) -> None:
    queue = session.events.subscribe()
    try:
        while True:
            event = await queue.get()
            if event is None:
                return
            _render(event.kind, event.payload)
    except asyncio.CancelledError:  # pragma: no cover - shutdown path
        pass
    finally:
        session.events.unsubscribe(queue)


def _render(kind: str, p: dict[str, Any]) -> None:
    if kind == "session_started":
        mode = yellow("  [DRY RUN — writes will be simulated]") if p.get("dry_run") else ""
        print(
            f"{bold('session')} {p['session_id']}  {p['target']}  "
            f"via {p['transport']}  {cyan(p['capability'])}{mode}"
        )
    elif kind == "command_started":
        print(f"{dim('$')} {p['command']}  {dim('— ' + p.get('purpose', ''))}")
    elif kind == "command_finished":
        print(f"  {dim(p['summary'])}")
    elif kind == "command_refused":
        print(f"{red('REFUSED')} {p['command']}")
        print(f"  {dim(p['reason'])}")
    elif kind == "assistant_text":
        for line in p["text"].strip().splitlines():
            print(f"  {line}")
    elif kind == "finding":
        f = p["finding"]
        colour = {
            "software_fixable": green,
            "hardware_suspected": red,
            "needs_human": yellow,
            "informational": dim,
        }.get(f["category"], dim)
        print(f"{colour('FINDING')} [{f['category']}/{f['confidence']}] {f['symptom']}")
    elif kind == "remediation_proposed":
        r = p["remediation"]
        print(f"{yellow('PROPOSED')} {r['id']}  {r['command']}")
    elif kind == "remediation_applied":
        marker = green("APPLIED") if p["ok"] else red("APPLY FAILED")
        print(f"{marker} {p['remediation_id']}  {dim(p['summary'])}")
    elif kind == "remediation_rejected":
        print(f"{red('REJECTED')} {p['remediation_id']}  {dim(p.get('reason', ''))}")
    elif kind == "observation_requested":
        print(f"{cyan('OBSERVE')} {p['instruction']}")
    elif kind == "error":
        print(f"{red('ERROR')} {p['message']}")


# ------------------------------------------------------------------ human decisions


async def _ask(prompt: str) -> str:
    """Read from the terminal without blocking the event loop."""
    return (await asyncio.to_thread(input, prompt)).strip()


def make_approval_handler(approver: str) -> Any:
    async def handler(remediation: Remediation, plan: SnapshotPlan) -> ApprovalDecision:
        rule("APPROVAL REQUIRED")
        print(f"  {bold('command')}      {bold(remediation.command)}")
        print(f"  {'rationale':<12} {remediation.rationale}")
        print(f"  {'expect':<12} {remediation.expected_effect}")
        print(f"  {'rollback':<12} {remediation.rollback_plan}")
        risk = remediation.risk.value
        colour = red if risk in ("high", "critical") else yellow
        print(f"  {'risk':<12} {colour(risk)}")

        if plan.has_rollback:
            print(f"  {'snapshot':<12} {green(plan.describe())}")
        else:
            print(f"  {'snapshot':<12} {red(plan.describe())}")
        print()

        answer = (await _ask("  Apply this change? [y/N] ")).lower()
        if answer not in ("y", "yes"):
            reason = await _ask("  Reason for rejecting (optional): ")
            rule()
            return ApprovalDecision(False, approver=approver, reason=reason)

        acknowledged = False
        if not plan.has_rollback:
            print(
                red(
                    "\n  There is no rollback point for this change. If it does the wrong "
                    "thing,\n  there is nothing to restore from."
                )
            )
            confirm = (
                await _ask("  Type 'no rollback' to confirm you accept that: ")
            ).lower()
            if confirm != "no rollback":
                rule()
                return ApprovalDecision(
                    False, approver=approver, reason="no-rollback acknowledgement not given"
                )
            acknowledged = True

        reason = await _ask("  Note for the journal (optional): ")
        rule()
        return ApprovalDecision(
            True, approver=approver, reason=reason, acknowledge_no_rollback=acknowledged
        )

    return handler


def make_observation_provider() -> Any:
    async def provider(instruction: str, expects: ObservationKind) -> Observation:
        rule("OBSERVATION REQUESTED")
        print(f"  {instruction}")
        print(dim(f"  (expects: {expects.value})"))
        if expects is ObservationKind.IMAGE:
            path = await _ask("  Path to a photo (blank to skip): ")
            note = await _ask("  Anything to add in words: ")
            rule()
            return Observation(
                instruction=instruction, expects=expects, value=note, media_path=path or None
            )
        value = await _ask("  Your answer: ")
        rule()
        return Observation(instruction=instruction, expects=expects, value=value)

    return provider


# ------------------------------------------------------------------------ transports


def build_transport(args: argparse.Namespace, capability: Capability) -> Transport:
    if args.mock:
        return faulty_workstation(
            capability=capability,
            dry_run=args.dry_run,
            timeout_s=args.timeout,
            observation_provider=make_observation_provider(),
        )

    from ..transports.ssh import SSHTransport

    password = None
    if args.ask_password:
        password = getpass.getpass(f"SSH password for {args.user}@{args.host}: ")
    elif os.environ.get("TRIAGE_SSH_PASSWORD"):
        # Read from the environment rather than argv, so the credential never appears
        # in the process list. It is not journaled either way.
        password = os.environ["TRIAGE_SSH_PASSWORD"]

    return SSHTransport(
        host=args.host,
        username=args.user,
        port=args.port,
        password=password,
        client_keys=[args.key] if args.key else None,
        known_hosts="" if args.insecure_host_key else None,
        capability=capability,
        dry_run=args.dry_run,
        timeout_s=args.timeout,
        observation_provider=make_observation_provider(),
    )


# --------------------------------------------------------------------------- commands


async def cmd_run(args: argparse.Namespace) -> int:
    capability = Capability(args.capability)

    if not args.mock and not args.host:
        print(red("A target is required: pass --host (with --user), or --mock."))
        return 2

    authorization = args.authorization
    if not authorization:
        print(
            "Triage operates only on machines you are authorized to service.\n"
            "This assertion is recorded in the journal."
        )
        authorization = input("Assert your authorization for this target: ").strip()
    if not authorization:
        print(red("No authorization asserted. Refusing to start a session."))
        return 2

    journal = Journal(args.journal, jsonl_path=args.jsonl)
    if os.environ.get("TRIAGE_SSH_PASSWORD"):
        journal.register_secret(os.environ["TRIAGE_SSH_PASSWORD"])

    transport = build_transport(args, capability)
    target = TargetDescriptor(
        name=args.target or (transport.target if not args.mock else "mock faulty workstation"),
        notes=args.notes or "",
    )

    session = DiagnosticSession(
        transport,
        journal,
        target=target,
        authorization=AuthorizationRecord(
            statement=authorization, asserted_by=args.approver or getpass.getuser()
        ),
        capability=capability,
        approval_handler=make_approval_handler(args.approver or getpass.getuser()),
        max_turns=args.max_turns,
    )

    renderer = asyncio.create_task(render_events(session))
    try:
        await session.start()
        report = await session.run(args.instruction)
    except KeyboardInterrupt:  # pragma: no cover - interactive
        print(yellow("\nInterrupted. The machine is left as it is; the journal is intact."))
        return 130
    finally:
        await session.close()
        renderer.cancel()

    if report is not None:
        _print_report(session, report)
    else:
        print(yellow("\nThe session ended without a final report. The journal has the detail."))

    intact, bad_seq, note = journal.verify()
    print(dim(f"\njournal: {args.journal}  ({len(journal.entries(session.id))} entries, {note})"))
    if not intact:
        print(red(f"JOURNAL INTEGRITY FAILURE at entry {bad_seq} — do not trust this record."))
        return 1
    return 0


def _print_report(session: DiagnosticSession, report: Any) -> None:
    rule("TRIAGE REPORT")
    print(report.summary.strip() + "\n")

    for category, label, colour in (
        ("software_fixable", "SOFTWARE-FIXABLE", green),
        ("hardware_suspected", "HARDWARE SUSPECTED", red),
        ("needs_human", "NEEDS A HUMAN", yellow),
        ("informational", "INFORMATIONAL", dim),
    ):
        matching = [f for f in report.findings if f.category.value == category]
        if not matching:
            continue
        print(colour(f"  {label}"))
        for finding in matching:
            print(f"    · {finding.symptom}  {dim('(' + finding.confidence.value + ')')}")
            print(f"      {dim(finding.hypothesis)}")

    if report.remediations:
        print(bold("\n  CHANGES"))
        for remediation in report.remediations:
            print(f"    · [{remediation.status.value}] {remediation.command}")

    if report.confident_about:
        print(bold("\n  CONFIDENT"))
        print(f"    {report.confident_about}")
    if report.uncertain_about:
        print(bold("\n  UNCERTAIN"))
        print(f"    {report.uncertain_about}")
    if report.human_next_steps:
        print(bold("\n  WHAT YOU MUST DO NEXT"))
        for line in report.human_next_steps.strip().splitlines():
            print(f"    {line}")
    rule()


def cmd_journal(args: argparse.Namespace) -> int:
    journal = Journal(args.journal)
    if args.list_sessions:
        for row in journal.sessions():
            flag = " [dry-run]" if row["dry_run"] else ""
            print(f"{row['id']}  {row['created_at']}  {row['target']}  {row['capability']}{flag}")
        return 0
    for entry in journal.entries(args.session):
        print(f"{entry.seq:>4}  {entry.at}  {entry.kind}")
        if args.verbose:
            for key, value in entry.payload.items():
                text = str(value)
                if len(text) > 400:
                    text = text[:400] + "…"
                print(f"        {key}: {text}")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    intact, bad_seq, note = Journal(args.journal).verify()
    if intact:
        print(green(f"journal intact — {note}"))
        return 0
    print(red(f"JOURNAL INTEGRITY FAILURE at entry {bad_seq}: {note}"))
    return 1


def cmd_catalog(args: argparse.Namespace) -> int:
    from ..agent.catalog import default_catalog

    catalog = default_catalog()
    entries = catalog.entries() if args.all else catalog.read_capable()
    for entry in entries:
        sudo = dim(" [sudo]") if entry.requires_sudo else ""
        marker = green("READ ") if entry.is_read_capable else red("WRITE")
        print(f"{marker} {entry.name:<18}{sudo} {dim(entry.summary)}")
    print(dim(f"\n{len(entries)} of {len(catalog)} catalogued commands"))
    return 0


# ----------------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="triage",
        description="Point a diagnostic agent at a faulty computer. It looks freely, "
        "and touches only through your approval.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run a diagnostic session")
    target = run.add_argument_group("target")
    target.add_argument("--host", help="SSH host of the target")
    target.add_argument("--user", help="SSH username")
    target.add_argument("--port", type=int, default=22)
    target.add_argument("--key", help="path to a private key")
    target.add_argument(
        "--ask-password", action="store_true", help="prompt for an SSH password"
    )
    target.add_argument(
        "--insecure-host-key",
        action="store_true",
        help="skip host key verification (for a machine that may have been reinstalled)",
    )
    target.add_argument("--mock", action="store_true", help="run against the scripted mock target")
    target.add_argument("--target", help="a name for the target, for the report")
    target.add_argument("--notes", help="anything the agent should know about this machine")

    safety = run.add_argument_group("safety")
    safety.add_argument(
        "--capability",
        choices=[c.value for c in Capability],
        default=Capability.EXECUTE_RW.value,
        help="what the agent may attempt (default: EXECUTE_RW — writes still need approval)",
    )
    safety.add_argument(
        "--dry-run",
        action="store_true",
        help="run the full loop but simulate every write and snapshot",
    )
    safety.add_argument("--authorization", help="your assertion that you may service this target")
    safety.add_argument("--approver", help="who is approving changes (default: current user)")

    session = run.add_argument_group("session")
    session.add_argument("--instruction", help="what to investigate (default: general triage)")
    session.add_argument("--journal", default="triage.sqlite", help="journal database path")
    session.add_argument("--jsonl", help="also mirror the journal to this JSONL file")
    session.add_argument("--timeout", type=float, default=60.0, help="per-command timeout")
    session.add_argument("--max-turns", type=int, default=40)
    run.set_defaults(func=lambda a: asyncio.run(cmd_run(a)))

    show = sub.add_parser("journal", help="show journal entries")
    show.add_argument("--journal", default="triage.sqlite")
    show.add_argument("--session", help="limit to one session id")
    show.add_argument("--list-sessions", action="store_true")
    show.add_argument("-v", "--verbose", action="store_true", help="include entry payloads")
    show.set_defaults(func=cmd_journal)

    verify = sub.add_parser("verify", help="check the journal hash chain")
    verify.add_argument("--journal", default="triage.sqlite")
    verify.set_defaults(func=cmd_verify)

    catalog = sub.add_parser("catalog", help="show the classified command catalog")
    catalog.add_argument("--all", action="store_true", help="include write-classified commands")
    catalog.set_defaults(func=cmd_catalog)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
