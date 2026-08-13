# Architecture

How the pieces fit, and — more importantly — where the seams are for the tiers that are
not built yet.

## The spine

```
                    ┌──────────────────────────────────────────┐
                    │            DiagnosticSession             │
                    │  owns the loop, the phase, the findings  │
                    └───┬───────────┬────────────┬─────────────┘
                        │           │            │
        ┌───────────────▼──┐  ┌─────▼──────┐  ┌──▼────────────────┐
        │   CommandGate    │  │  Journal   │  │  ApprovalQueue    │
        │ READ/WRITE/UNK   │  │ append-only│  │ + SnapshotManager │
        └───────────────┬──┘  └────────────┘  └──┬────────────────┘
                        │                        │
                        │  run_read(command)     │  run_write(WriteAuthorization)
                        └────────────┬───────────┘
                                     ▼
                    ┌────────────────────────────────────┐
                    │             Transport              │
                    │  ssh · mock · (liveusb · human ·   │
                    │   oob · winrm — not built yet)     │
                    └────────────────────────────────────┘
```

The session never learns which transport is bound. The gate never learns either. The
journal records what happened regardless. That is the property that has to survive every
future phase, and it is why the two execution methods have deliberately asymmetric
signatures:

```python
async def run_read(self, command: str, ...) -> CommandResult
async def run_write(self, authorization: WriteAuthorization, ...) -> CommandResult
```

`run_write` cannot be called with a command. It reads the command out of an authorization
that only `ApprovalQueue.approve()` can mint, only after a human said yes, and that raises
if used twice. "No mutating command without an approved remediation" is therefore a type
signature, not a rule someone has to remember.

## The path a change takes

```
 model calls propose_remediation
        │
        ▼
 ApprovalQueue.propose()          ← refuses below EXECUTE_RW; refuses without a finding
        │
        ▼
 SnapshotManager.plan()           ← read-only: what protection is *available*
        │
        ▼
 human decides                    ← shown the command, evidence, effect, rollback, risk
        │                            and whether a rollback point actually exists
        ▼
 SnapshotManager.protect()        ← takes the rollback point, immediately before the write
        │
        ├── the plan was wrong? ──► ask the human again, with the truth
        ▼
 ApprovalQueue.approve()          ← mints a single-use WriteAuthorization
        │                            requires a second acknowledgement if nothing can be restored
        ▼
 Transport.run_write(auth)        ← simulated under --dry-run
        │
        ▼
 outcome returned to the model    ← "now VERIFY", not "done"
```

Every arrow writes a journal entry. The journal is hash-chained, so the sequence above is
reconstructable afterwards from a record the agent could not have edited.

## Why a plan and a protect

`plan()` is read-only detection; `protect()` takes the rollback point. Splitting them lets
the human see what protection exists *before* deciding, rather than after.

The split also exposes a case worth naming: a plan is a forecast, and forecasts are wrong.
A promised btrfs snapshot or file backup can fail to materialise between the decision and
the write. The session does not proceed unprotected (the human approved something else)
and does not reject on the operator's behalf — it goes back and asks again with the truth.

## The snapshot ladder

| Rung | When | Reversible |
|---|---|---|
| btrfs / ZFS snapshot | the target's filesystem supports it | yes, cheaply |
| File backup | the blast radius is determinable from the command | yes, by hand |
| Nothing | neither of the above | **no — and it says so** |

The third rung is the important one. It is what triggers the second acknowledgement, and
it is never disguised as one of the first two.

## Seams for the phases not built

The build spec's roadmap is Phases 2–4. None are implemented; all four seams already exist.

### Phase 2 — live-USB transport

Boot a won't-boot target from a controlled live environment carrying the agent, mount the
installed disk, repair. **The seam is already there**: operationally this reduces to an
`SSHTransport` pointed at the live environment. What is missing is not architecture but
the operational wrapper — the image, and someone to select boot media.

The one thing to add when building it: the *target* the agent reasons about is the mounted
installed system, not the live environment it is running in. `TargetDescriptor.notes` is
the field for saying so, and the snapshot scope needs to be the mount point rather than `/`.

### Phase 3 — human-relay + vision (`ADVISE_ONLY`)

The deepest transport, and the reason `capture_observation` is in the `Transport` interface
from day one rather than being added later:

```python
async def capture_observation(self, instruction: str, expects: ObservationKind) -> Observation
```

`Observation` already carries `media_path` for a phone photograph, and `ObservationKind`
already has `IMAGE` and `MEASUREMENT` alongside `TEXT` and `YES_NO`. The CLI and the API
both already implement providers for it — the CLI prompts at the terminal, the service
parks on a future and exposes `/sessions/{id}/observations`.

What a `HumanTransport` needs to add:

- `_execute` becomes "show the human this instruction, capture what they report" — the
  same shape, a slower far end.
- `describe()` reports `capability=ADVISE_ONLY`, which the session already handles: the
  write tool is withheld, and `require_execute` steers the model to `request_observation`.
- Image content must reach the model as an image content block rather than a path string.
  This is the one genuine code change outside `transports/` — `ToolDispatcher` currently
  renders an observation as text.

The system prompt already carries the `ADVISE_ONLY` behavioural section.

### Phase 4 — out-of-band (BMC/iDRAC/iLO, AMT/vPro)

The one genuine wire to a dead-OS machine: power control, serial-over-LAN, virtual media.
Server and business-class only, and it needs prior provisioning — so it lights up *extra*
powers when the target has it, and is never the foundation.

The seam is the same `Transport` interface. Two things it will want that do not exist yet:

- Power actions (`power_cycle`, `mount_virtual_media`) are mutations that are not shell
  commands, so they need to reach the write path without going through `CommandGate`.
  The clean way is a catalog entry per action plus an authorization minted the same way —
  not a bypass.
- Serial-over-LAN is a stream, not a request/response. `_execute` will need a
  send-and-expect wrapper around it.

### Windows / WinRM

`CommandGate` and the catalog are Linux-shaped today: `catalog.json` describes POSIX
binaries, and `_lex` uses POSIX shell tokenization. The catalog being *data* is what makes
this tractable — a Windows target needs its own catalog file and its own lexer, not a
different gate. `Catalog.load()` already takes override paths.

## What is deliberately not abstract

Some things could have been made pluggable and were not, because the indirection would cost
more than it returns:

- **The journal is SQLite.** It is a local, single-writer, append-only record. A pluggable
  storage backend would mostly be an opportunity to lose the trigger-enforced append-only
  property.
- **The model is Claude via the Messages API.** Model choice and effort are config
  (`TRIAGE_MODEL`, `TRIAGE_EFFORT`); the provider is not. The loop depends on tool-use
  semantics specific to it.
- **The gate is not extensible by code.** Only by data. A plugin that could add
  classification logic would be a place to accidentally widen the read path.
