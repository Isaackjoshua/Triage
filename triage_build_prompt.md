# Build Prompt — `Triage`: a transport-agnostic AI machine-diagnostics agent

> Paste this into Claude Code (or your agentic build tool of choice) as the top-level spec.
> `Triage` is a working codename — rename freely. The name is apt because the product's job is medical-style triage: sort a sick machine's problems into *fix it now*, *hardware — a human must act*, and *needs a human's eyes*.

---

## 1. Role & mission

You are implementing the first two phases of a system that lets an operator point an AI agent (Claude, via the Anthropic Messages API with tool use) at a **faulty computer**, have it run diagnostics, **fix the software problems it safely can**, and **report + suggest fixes for the ones it can't** (hardware, or anything needing human hands).

The essence in one line: **one reasoning brain, many ways to reach the patient, and a hard safety split between looking and touching.**

Do not build a generic remote-admin tool with an LLM bolted on. The specific shape below is the point. Preserve it.

---

## 2. The core idea that must not get flattened

There is a single boundary that governs this entire system — call it **the wall**. It is the line where software can no longer run *on the target itself*.

**Below the wall**, the agent can *execute*: run commands, read real telemetry, apply fixes. **Above the wall**, the agent's role *inverts* — it becomes an advisor whose hands, eyes, and sensors are a human being.

Everything is organized around three constant concepts that hold at every tier:

- **Transport** — *how the brain reaches the patient.* SSH, WinRM, serial console, an agent running in a booted live-USB environment, out-of-band management (BMC/AMT), or **a human relaying instructions and observations back**. Transports are pluggable behind one interface.
- **Capability** — *what the agent is allowed to attempt on this transport.* `EXECUTE_RW`, `EXECUTE_RO`, or `ADVISE_ONLY`.
- **The fix-gating layer** — the split between *diagnose (read)* and *remediate (write)*, plus snapshot-before-mutate and human approval. **This rides along identically regardless of transport.**

**The insight that keeps this one system instead of four: a human being driven by the agent is just the deepest transport.** At the executable tiers, `execute(command)` runs over SSH. At the wall, `execute(instruction)` means "show the human what to do, and capture what they report back — text, a meter reading, or a photo." Same brain, same loop, same gating. The only thing that changes per tier is *who or what sits on the far end of the pipe.* Model that as first-class and the whole spectrum is coherent.

**In this build you implement the abstraction plus the two executable-transport phases. You do not build the human/vision or out-of-band transports yet — but you leave clean seams for them.**

---

## 3. Scope for THIS pass

Build **Phase 0 (scaffolding + the spanning abstraction)** and **Phase 1 (the executable SSH core)**. That is the shippable MVP. Everything past it is Section 10 (roadmap) and must be *designed-for* but *not implemented*.

**In scope now:**
- The `Session` / `Transport` / `Capability` / `Gate` / `Journal` domain model.
- A working `SSHTransport` against a reachable **Linux** target with credentials.
- A read-only diagnostics tool suite the agent calls freely.
- Structured findings + a final triage report separating software-fixable / hardware-suspected / needs-human.
- The write path: agent *proposes* remediations, human *approves*, system *snapshots then applies then verifies*.
- A `MockTransport` and a global `--dry-run` mode so the whole thing is testable **without a real sick machine**.
- A minimal CLI client where approvals happen inline.

**Explicitly out of scope now (Section 10):** live-USB transport, human-relay + vision transport, out-of-band (IPMI/Redfish/AMT), Windows/WinRM targets, any web UI.

---

## 4. Architecture

### 4.1 Domain model

- **`DiagnosticSession`** — one engagement against one target. Holds: id, target descriptor, bound `Transport`, `Capability`, current `phase` (`DIAGNOSE` | `REMEDIATE`), accumulated `Finding`s, pending `Remediation`s, and a reference to its `Journal`. Owns the agent loop.
- **`Transport`** (abstract interface — the load-bearing abstraction). Concrete: `SSHTransport`, `MockTransport` now; others later. Interface:
  - `describe() -> TransportInfo` — capability level, reachability, what it supports.
  - `run_read(command, timeout) -> CommandResult` — execute a command the gate has already classified read-only.
  - `run_write(command, timeout) -> CommandResult` — execute an *already-approved* mutating command. Never called except by the approval flow.
  - `capture_observation(instruction, expects) -> Observation` — for non-executable transports; returns text/measurement/image. On `SSHTransport` this can prompt the operator for input, but it exists mainly for the future human transport. Keep it in the interface from day one so later phases slot in without reshaping.
  - `snapshot(scope) -> SnapshotRef | None` — take a rollback point if the target supports it; return `None` if not possible.
- **`Capability`** — enum `EXECUTE_RW` / `EXECUTE_RO` / `ADVISE_ONLY`. The session refuses to run the write path unless capability is `EXECUTE_RW`.
- **`CommandGate`** — the safety classifier + policy (Section 5). Classifies every command `READ` / `WRITE` / `UNKNOWN`. **`UNKNOWN` is treated as `WRITE` (fail safe).**
- **`Journal`** — append-only audit log (SQLite table + optional JSONL mirror). Records every command, its classification, its result, every finding, every proposed remediation, every approval decision, and every snapshot ref. This is the independent record of what the agent did — it must be complete enough to reconstruct the session and to *distrust the agent after the fact if needed*.
- **`Finding`** — `{ symptom, evidence, hypothesis, confidence, category }` where `category ∈ { software_fixable, hardware_suspected, needs_human, informational }`.
- **`Remediation`** — `{ command, rationale, expected_effect, rollback_plan, risk, status }` where `status ∈ { proposed, approved, rejected, applied, verified, failed }`.

### 4.2 The agent loop

Standard tool-use loop against the Anthropic Messages API:

1. Send the model the session context, the target's transport/capability, and the tool schema.
2. Model gathers state by calling read tools; results are fed back.
3. Model records structured `Finding`s as it forms hypotheses.
4. For anything software-fixable, the model *proposes* a `Remediation` — it **cannot apply it directly**.
5. Proposals surface to the human. On approval: snapshot → apply → capture result → feed back so the model can **verify** the fix actually worked (and roll back / re-plan if not).
6. Model calls `finalize_report()` to emit the triage summary.

The loop is transport-agnostic: it only ever talks to tools, and the tools talk to the bound transport.

### 4.3 The tool schema the model gets (Phase 1)

- **`run_read_command(command, purpose)`** — executes **only if** `CommandGate` classifies it `READ`. If `WRITE`/`UNKNOWN`, it is **not executed**; return a rejection telling the model to route it through `propose_remediation`. This is where the read/touch split is enforced against the model.
- **`propose_remediation(command, rationale, expected_effect, rollback_plan, risk)`** — does **not** execute. Registers a pending `Remediation`, returns `"pending human approval"`.
- **`record_finding(symptom, evidence, hypothesis, confidence, category)`** — append a structured finding.
- **`request_observation(instruction, expects)`** — ask the operator for something the transport can't fetch (a value, a yes/no, later a photo). Minimal for SSH; real payoff in the future human transport. Wire it now.
- **`finalize_report()`** — emit the structured triage report and end the diagnostic phase.

### 4.4 Read-diagnostics suite (Linux target, all read-only)

Curate an allowlisted set the agent can lean on — e.g. `smartctl -a` / `-H` (health/attributes; **not** `-t`, which starts a self-test and is a side-effect → WRITE), `sensors`, `journalctl -p err -b`, `systemctl --failed`, `dmesg` (ring buffer read), `df -h`, `free -h`, `lsblk`, `findmnt`, `ip a`, `uptime`, `dmidecode` (via sudo, read-only), `cat`/`head` on specific logs and `/proc` entries, package-manager *query* subcommands. Ship this as data (a classified command catalog), not hardcoded logic, so it's auditable and extensible.

---

## 5. Safety invariants (enforce in CODE, not just in the prompt)

These are non-negotiable and must be structurally impossible to bypass, not merely discouraged in a system prompt:

1. **No command mutates state without passing the gate.** The only path to `run_write` is through an approved `Remediation`.
2. **The classifier fails safe.** `UNKNOWN` → `WRITE`. Never "parse and hope." When in doubt, it needs approval.
3. **Snapshot before every approved write.** Detect filesystem (btrfs / ZFS / LVM) and take a rollback point. If no snapshot is possible, *back up the specific file(s)* the command will touch when determinable; if even that's impossible, require an explicit second confirmation acknowledging "no automatic rollback available."
4. **Human-in-the-loop for all writes.** The model proposes; a human approves. No auto-apply, ever — not even for "obviously safe" fixes. Remember the patient by definition has no healthy backup, so one bad `dd`/`fstab` edit/bootloader change turns a recoverable problem into an unrecoverable one.
5. **Everything is journaled append-only.** Command, classification, result, finding, proposal, decision, snapshot ref. The journal is the trust anchor: a corrupted machine can lie about its own health, so the operator needs an independent, complete record.
6. **`--dry-run` runs the full loop** — including proposing remediations — but every `run_write` and `snapshot` is simulated and clearly marked. This is how you demo and test safely.
7. **Timeouts and output caps** on every command; a hung target must not hang the session.

---

## 6. Authorization & consent

This is a tool for machines the operator is **authorized to touch**. Build it that way:

- At session creation, the operator must assert authorization for the target (record it in the journal).
- Where the target has its own user/owner, surface a consent step before any write.
- Credentials are provided by the operator per session and are never written to the journal or logs.
- Nothing in here is for accessing machines you don't own or administer. The system prompt driving the agent should state this plainly, and the code should not paper over missing authorization.

---

## 7. Recommended stack (swap if you prefer)

- **Language/runtime:** Python 3.11+.
- **Service core:** FastAPI — exposes the session API (create session, stream events, list/approve/reject remediations). Keep all transport/session/gate logic UI-agnostic behind this so a web UI can be added later without touching the core.
- **Agent:** Anthropic Messages API with tool use. Keep model choice and API specifics in config; consult the official docs for the current tool-use loop shape and models rather than hardcoding assumptions — https://docs.claude.com/en/api/overview and the tool-use guide under the same docs site.
- **SSH transport:** `asyncssh` (pairs cleanly with async FastAPI) or `paramiko`.
- **Persistence:** SQLite for sessions, findings, journal.
- **MVP client:** a CLI that creates a session, streams the agent's activity, and prompts inline for remediation approvals (y/n + reason). The web UI is a later add-on; do not build it now.

---

## 8. Suggested module layout

```
triage/
  core/
    session.py        # DiagnosticSession, the agent loop
    capability.py     # Capability enum + policy checks
    gate.py           # CommandGate: classifier + WRITE/UNKNOWN fail-safe
    journal.py        # append-only audit log (SQLite + JSONL)
    models.py         # Finding, Remediation, CommandResult, Observation, ...
  transports/
    base.py           # Transport interface (the load-bearing abstraction)
    ssh.py            # SSHTransport  (Phase 1)
    mock.py           # MockTransport (for tests + dry-run demos)
    # later: liveusb.py, human.py, oob.py, winrm.py  (leave seams, do not implement)
  agent/
    tools.py          # tool schema + dispatch to the bound transport
    prompts.py        # system prompt: mission, safety, authorization stance
    catalog.py        # classified read-only command catalog (data, not logic)
  remediation/
    approval.py       # pending-remediation queue + human approval flow
    snapshot.py       # fs detection + snapshot/backup/rollback
  api/
    app.py            # FastAPI service
  cli/
    main.py           # MVP client with inline approvals
  tests/
```

---

## 9. Acceptance criteria (definition of done for the MVP)

- [ ] Create a session against a reachable Linux box over SSH with operator-supplied creds; authorization asserted and journaled.
- [ ] Agent runs a batch of **read** diagnostics and produces a structured findings report separating `software_fixable` / `hardware_suspected` / `needs_human` / `informational`.
- [ ] For a software-fixable issue, the agent produces an exact `Remediation` with rationale, expected effect, and rollback plan; it **blocks on human approval**; on approval the system **snapshots (or warns), applies, then verifies**; the outcome is fed back to the agent.
- [ ] A `WRITE`/`UNKNOWN` command routed through `run_read_command` is **refused** and steered to `propose_remediation`.
- [ ] The full session is reconstructable from the append-only journal; credentials never appear in it.
- [ ] The entire flow runs under `--dry-run` and against `MockTransport` with no real target.
- [ ] No code path applies a mutating command without an approved `Remediation`.

Build these first; treat them as the test plan.

---

## 10. Roadmap — design for these, do NOT build them now

Leave the interfaces clean so each slots in without reshaping the core:

- **Phase 2 — live-USB transport.** Boot the won't-boot target from a controlled live environment carrying the agent, mount the installed disk, repair. Operationally this often *reduces to an `SSHTransport`* pointed at the live env — so the seam may already exist. Requires the target to POST and someone to select boot media (physically or via remote KVM).
- **Phase 3 — human-relay + vision transport (`ADVISE_ONLY`).** The deepest transport: `capture_observation` relays instructions and ingests the human's reports, **including phone-camera images** the multimodal model reads (bulged caps, scorched VRM, unseated RAM, wrong power connector, missing standoff). Guided minimal-config bring-up flowcharts (one stick, onboard video, PSU test). Ceiling: perception and naming the failed part — *not* fixing dead silicon. Note the different UX: an expert guide, not an executor, and its fidelity is bounded by how good a sensor the human is.
- **Phase 4 — out-of-band (bonus, opportunistic).** BMC/iDRAC/iLO via IPMI/Redfish, Intel AMT/vPro. The one genuine wire to a dead-OS machine: power control, serial-over-LAN, virtual media to force a boot. Server/business-class only, needs prior provisioning — so it lights up *extra* powers when the target has it; it is never the foundation.

A note the implementer should keep in mind: two normal PCs won't talk over a plain USB cable (both are USB hosts). Direct Ethernet is the realistic wired path, and it still needs a live NIC stack on the target. The universal "plug a cable into any dead box" is exactly what out-of-band hardware provides and exactly what consumer machines lack — which is why the executable core leans on SSH/network, and the hardware end leans on the human transport.

---

## 11. How to work

- Build **Phase 0 then Phase 1**, incrementally, testing against `MockTransport` + `--dry-run` before ever touching a real machine.
- Get the `Transport` interface and the `CommandGate` right **before** filling in SSH — they are the spine; everything hangs off them.
- Treat the safety invariants (Section 5) as inviolable. If you believe one must bend, **stop and ask** rather than working around it.
- Keep transport-specific code strictly inside `transports/`; the session, gate, agent loop, and journal must not know which transport they're driving.
- Ship the MVP against the Section 9 criteria first. Resist starting later phases until the executable core is solid.
