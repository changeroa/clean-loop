<p align="center">
  <img src="assets/clean-loop.png" width="440" alt="A continuous loop carrying code through observation, execution, evidence, and verification">
</p>

<h1 align="center">clean-loop</h1>

<p align="center">
  <em>Agents can stop. Evidence shouldn't.</em>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.9%2B-111111?style=flat-square" alt="Python 3.9+">
  <img src="https://img.shields.io/badge/runtime-stdlib%20only-111111?style=flat-square" alt="Python standard library only">
  <img src="https://github.com/changeroa/clean-loop/actions/workflows/check.yml/badge.svg" alt="Check status">
  <img src="https://img.shields.io/badge/status-experimental-111111?style=flat-square" alt="Experimental status">
</p>

---

An agent says it finished. The process exited. A receipt appeared. The tests passed.

Those are four different facts.

`clean-loop` is a small deterministic orchestration kernel that keeps them separate. Agent hosts do
the reasoning, spawning, and polling. The kernel owns the task DAG, attempts, execution leases,
evidence, verdicts, and legal state transitions.

```text
Meta decides. Explorer observes. Worker changes. Verifier judges.
Evidence advances state.
```

## The boundary

Without an explicit lifecycle, a worker can accidentally become its own planner and verifier:

```text
spawn → edit → "looks done" → complete
```

With `clean-loop`:

```text
explore → synthesize → plan → execute → verify
                                      ├─ accept  → continue
                                      ├─ reject  → retry
                                      ├─ replan  → new plan revision
                                      └─ block   → request authority
```

Three distinctions carry most of the design:

```text
Task != Attempt
execution completed != task accepted
retry != replan
```

## How it works

```text
skill / policy
      │
      ▼
host supervisor ── spawn / poll / collect / interrupt
      │
      ▼
clean-loop kernel ── validate / transition / supervise
      │
      ▼
findings / plans / receipts / verdicts
```

The host can be Codex, Claude Code, or another agent runtime. It translates native agent statuses
through a narrow adapter. The kernel never calls a model, accesses the network, sleeps, or edits
product source.

| Layer           | Owns                                                                      |
| --------------- | ------------------------------------------------------------------------- |
| Meta agent      | framing, synthesis, invariants, planning, routing, replanning, completion |
| Explorer        | read-only facts, inferences, and unknowns                                 |
| Worker          | one bounded change inside declared `write_scope`                          |
| Verifier        | independent task and run verdicts                                         |
| Host supervisor | spawning, polling, result collection, interruption                        |
| Kernel          | state validation, DAG scheduling, leases, limits, legal transitions       |

These are authority boundaries. A host may implement them as isolated invocations, but the
acceptance verifier must use a fresh context and cannot repair the result it judges.

## Supervision

An unavailable poll is not a dead worker.

```text
starting → active → awaiting_receipt → terminated
              │
              └→ unreachable → lost
```

`active` renews a lease. `unknown` becomes `unreachable`, then receives a grace period. Only an
expired lease or receipt deadline becomes `lost`, at which point the kernel requests workspace
reconciliation before retrying.

Every attempt receives a fenced execution identity such as `T002-A01-E01`. A stale receipt from an
older attempt cannot advance the active task.

## Quick start

The runtime requires Python 3.9 or newer and has no runtime dependencies.

```bash
git clone https://github.com/changeroa/clean-loop.git
cd clean-loop

clean_loop_script="skills/clean-loop/scripts/loop.py"
run_dir=".clean-loop/runs/example-run"

python3 "$clean_loop_script" init "$run_dir" \
  --goal "Implement one bounded change"

python3 "$clean_loop_script" validate "$run_dir"
python3 "$clean_loop_script" status "$run_dir"
python3 "$clean_loop_script" next "$run_dir"
python3 "$clean_loop_script" supervise "$run_dir" \
  --at 2026-08-28T00:00:00Z
```

`supervise` returns one host directive:

```text
dispatch
poll
collect_receipt
reconcile_workspace
verify_task
verify_run
wait
```

The host performs the external action, records the observation, and asks the kernel for the next
directive. See the [host supervision contract](skills/clean-loop/references/supervision.md) for the
adapter cycle and [artifact contracts](skills/clean-loop/references/contracts.md) for exact schemas
and transitions.

## Use as an agent skill

The user-facing skill lives in [`skills/clean-loop/`](skills/clean-loop/). Point a compatible
agent's skill loader at that directory or copy it into the host's local skill directory.

For Codex:

```bash
mkdir -p ~/.codex/skills
cp -R skills/clean-loop ~/.codex/skills/clean-loop
```

Then ask the agent to use `clean-loop` for a bounded development goal. The skill makes the host the
Meta agent and uses the kernel as its source of truth.

## Artifacts

```text
.clean-loop/runs/<run-id>/
├── run.json          # mutable state projection
├── questions.json    # bounded exploration queue
├── findings/         # repository and runtime evidence
├── syntheses/        # Meta interpretation and invariants
├── plans/            # immutable DAG revisions
├── receipts/         # attempt results
└── verdicts/         # independent task and run judgments
```

Findings, syntheses, plans, receipts, and verdicts are immutable once registered. `run.json` is the
only mutable projection and assumes exactly one supervisor writer.

Write one artifact and register it immediately, or admit a complete batch atomically with
`register-artifacts`. A worker receipt and its task submission can be committed in one projection
update with `submit --artifact <receipt> --admit`; task verdicts support the same pattern through
`apply-verdict --admit`. `questions.json` remains mutable and must never be registered.

`validate` prints a bounded health summary by default; use `validate --full` for the complete
projection. `status` adds legal next-action guidance, while `next` returns only those actions. When
a completed run receives a new requirement, preserve it and create a linked successor with
`init-next <new-run-dir> --after <completed-run-dir> --goal <goal>`.

## Development

```bash
npm install
uv sync
npm run check
```

The check suite runs Ruff linting and formatting, Prettier, and the unit/CLI suite covering:

- DAG validation and serial mutating execution
- acceptance, retry, replan, and block scenarios
- transient host unavailability and lease recovery
- receipt timeout and workspace reconciliation
- stale-result fencing and supervisor restart recovery
- atomic artifact admission, event-specific argument errors, and successor-run linkage

`npm run dogfood` also runs the public CLI through deterministic host scenarios in disposable Git
workspaces. Use `npm run dogfood:keep` to retain each workspace, transcript, and report under
`.dogfood/runs/` for inspection. See the [dogfooding guide](dogfood/README.md) for the scenario
contract and the boundary between deterministic and live-host runs.

The tests prove the kernel contract, not a particular host integration. Each adapter still needs
conformance coverage for spawn, status mapping, result collection, interruption, and reconnect.

## Scope

The MVP intentionally has no database, daemon, distributed lock, automatic Git mutation, commit,
pull request, or deployment authority. Pre-plan read-only exploration may fan out to three agents;
planned tasks, including all mutations, execute serially.

Persistent background supervision, concurrent orchestrators, and distributed workers belong in later
architecture decisions.
