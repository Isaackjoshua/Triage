# Triage

A transport-agnostic AI machine-diagnostics agent. You point it at a faulty computer; it runs
diagnostics, fixes the software problems it safely can, and reports the ones it can't — hardware,
or anything needing human hands.

**One reasoning brain, many ways to reach the patient, and a hard safety split between looking and
touching.**

The specs this implementation is built from live at the repo root:
[`triage_build_prompt.md`](triage_build_prompt.md) (what to build) and
[`triage_agent_system_prompt.md`](triage_agent_system_prompt.md) (how the agent behaves at runtime).

## The wall

There is a single boundary that governs the system: the line where software can no longer run *on
the target itself*. Below the wall the agent can execute; above it, its role inverts and it becomes
an advisor whose hands and eyes are a human being. Three concepts hold at every tier:

- **Transport** — how the brain reaches the patient. SSH now; live-USB, out-of-band, and a
  human relay later. Pluggable behind one interface.
- **Capability** — what the agent may attempt on this transport: `EXECUTE_RW`, `EXECUTE_RO`, or
  `ADVISE_ONLY`.
- **The fix-gating layer** — the split between diagnose (read) and remediate (write), plus
  snapshot-before-mutate and human approval. This rides along identically regardless of transport.

## Status

Phase 0 (the spanning abstraction) and Phase 1 (the executable SSH core) — the shippable MVP.
Live-USB, human-relay + vision, out-of-band, and Windows targets are designed-for but not built.

## Install

```sh
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

The agent loop calls the Anthropic Messages API, so set credentials before running against a real
target:

```sh
export ANTHROPIC_API_KEY=...   # or run `ant auth login`
```

## Try it without a sick machine

`MockTransport` plays a scripted faulty box and `--dry-run` simulates every write, so the whole
loop — including proposing remediations and approving them — runs with no real target:

```sh
triage run --mock --dry-run
```

## Run it against a real Linux target

```sh
triage run \
  --host 192.168.1.42 --user root --key ~/.ssh/id_ed25519 \
  --capability EXECUTE_RW \
  --authorization "I administer this machine (asset #4412)"
```

Add `--dry-run` to keep the read path live while simulating every write.

## Safety invariants

These are enforced in code, not merely requested in the system prompt:

1. No command mutates state without passing the gate. The only path to `run_write` is an approved
   `Remediation`, carried by a single-use `WriteAuthorization` that only the approval queue can mint.
2. The classifier fails safe. `UNKNOWN` is treated as `WRITE`.
3. Snapshot before every approved write — filesystem snapshot where possible, else a backup of the
   specific files the command touches, else an explicit second confirmation acknowledging that no
   automatic rollback is available.
4. Human-in-the-loop for all writes. The model proposes; a human approves. No auto-apply, ever.
5. Everything is journaled append-only, with a hash chain, in SQLite plus an optional JSONL mirror.
   Credentials never enter it.
6. `--dry-run` runs the full loop, simulating and clearly marking every write and snapshot.
7. Timeouts and output caps on every command.

## Authorization

Triage is for machines the operator is authorized to service. Session creation requires an
authorization assertion, which is journaled. Nothing here is for gaining access to machines you do
not own or administer.

## Layout

```
triage/
  core/         session, capability, gate, journal, models, events
  transports/   base, ssh, mock  (seams for liveusb, human, oob, winrm)
  agent/        tool schema + dispatch, system prompt, command catalog
  remediation/  approval queue + write authorization, snapshot/rollback
  api/          FastAPI service
  cli/          MVP client with inline approvals
tests/
```
