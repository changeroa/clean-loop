---
name: clean-loop
description:
  Orchestrate a bounded software-development goal through evidence-first exploration, an immutable
  task DAG, scoped implementation attempts, and independent acceptance. Use when the user asks to
  run a clean loop or wants planner, worker, and verifier responsibilities separated; do not use for
  simple one-step edits.
---

# Clean Loop

Clean Loop advances state only when recorded evidence supports the transition.

```text
Meta decides. Explorer observes. Worker changes. Verifier judges.
Evidence advances state.
```

Preserve these distinctions throughout the run:

```text
Task != Attempt
execution completed != task accepted
retry != replan
```

## Start a run

Read [references/vocabulary.md](references/vocabulary.md) and
[references/contracts.md](references/contracts.md) before starting. Read
[references/routing.md](references/routing.md) and
[references/supervision.md](references/supervision.md) when dispatching or monitoring an agent.

Resolve `<skill-dir>` to the directory containing this `SKILL.md`; do not assume the target
repository contains an installed copy of the skill. Initialize runtime artifacts from the target
repository root:

```bash
clean_loop_script="/absolute/path/to/clean-loop/scripts/loop.py"
run_dir=".clean-loop/runs/example-run"
python3 "$clean_loop_script" init "$run_dir" --goal "<user goal>"
```

Use `kebab-case` for `<run-id>`. Keep all source edits outside the runtime directory and inside the
active task's `write_scope`.

## Orchestrate

You are the Meta agent. You own framing, synthesis, invariants, plans, routing, verdict
interpretation, replanning, and completion. Do not modify product source code yourself.

1. Before creating the task DAG, record any pre-existing workspace changes as a fact so final
   verification can distinguish them from worker output. Define the questions required to plan.
   Create `questions.json` from [templates/questions.json](templates/questions.json) and follow its
   documented schema before dispatching explorers. Dispatch at most three read-only explorers in
   parallel with [agents/explorer.md](agents/explorer.md). Persist their structured findings under
   `findings/`.
2. Synthesize the findings. Separate facts, inferences, and unknowns; merge duplicates; identify
   contradictions; derive invariants; then choose exactly one disposition: `ready`,
   `needs_more_evidence`, or `needs_authority`. For `needs_more_evidence`, append the one permitted
   run-wide follow-up question, transition back to `exploring`, gather its findings, and create a
   new synthesis revision. For `needs_authority`, persist the blocking synthesis, transition to
   `blocked`, and ask the user for the named decision.
3. If ready, create the next immutable plan revision. Every task must be bounded, dependency-valid,
   evidence-based, and independently verifiable. Decompose `implement/high` before dispatch. If it
   cannot be decomposed within the active decisions and authority, block instead of dispatching it.
4. Run `validate`, activate the plan, and ask `supervise` for the next directive. MVP execution is
   serial. For `dispatch`, select adapter/model/effort, call `start-attempt` with an explicit UTC
   timestamp, and pass its fenced execution ID to the host agent. Dispatch `explore`, `implement`,
   and `verify` tasks with [agents/explorer.md](agents/explorer.md),
   [agents/worker.md](agents/worker.md), and [agents/verifier.md](agents/verifier.md), respectively.
   Poll through the host adapter and record `active`, `completed`, terminal, or `unknown`
   observations. An executor submits a matching execution-fenced receipt but cannot accept its own
   task.
5. Dispatch a fresh verifier with [agents/verifier.md](agents/verifier.md). Apply its task verdict.
   A rejection retries the same contract; a changed goal, dependency, basis, invariant, scope,
   acceptance criterion, or system decision requires a new plan revision. This acceptance verifier
   is separate from the task executor for every task kind. For a `verify` task it judges the
   submitted verification receipt; its verdict is an artifact, not another task, so verification
   does not recurse.
6. Meta may skip a pending task only when the original goal and every remaining dependency stay
   satisfiable. Once every active task is `accepted` or `skipped`, run fresh run-level verification
   against the original goal, active plan, final diff, invariants, receipts, tests, integration
   behavior, and scope drift. A persisted `pass` verdict permits `completed`; `replan` returns to
   planning and `block` ends the run as blocked.

Use the deterministic runtime for state changes; do not edit `run.json` by hand:

```bash
python3 "$clean_loop_script" --help
python3 "$clean_loop_script" validate "$run_dir"
python3 "$clean_loop_script" runnable "$run_dir"
python3 "$clean_loop_script" supervise "$run_dir" --at 2026-08-28T00:00:00Z
python3 "$clean_loop_script" transition --help
```

## Boundaries

- Explorers and verifiers are read-only. A verifier returns a verdict for Meta to persist; it does
  not repair the result it judges.
- A worker may modify only its declared `write_scope` plus its receipt destination. It must not
  change the DAG, invariants, architecture, authority, Git history, remote systems, or deployment
  state.
- Stop rather than silently widening scope. Record blockers and unexpected findings in the receipt.
- Treat one unavailable poll as `unknown`, not execution failure. Let the configured lease and grace
  period expire before workspace reconciliation. Never retry a stopped mutating execution before
  inspecting its write scope and actual diff.
- Reject stale receipts whose attempt, execution ID, generation, or runtime metadata no longer match
  the active projection. Do not register late worker success as current evidence.
- Defaults are one run-wide exploration follow-up, two total attempts per task, and two total plan
  revisions (`000` and `001`). Crossing a limit blocks the run; it does not trigger automatic model
  escalation.
- `blocked` is terminal in the MVP state machine. After the missing authority or external decision
  becomes available, start a new run that records it rather than editing or resuming the blocked
  run.
- Every finding, synthesis revision, plan revision, receipt, and verdict is immutable. `run.json` is
  the only mutable state projection.
- Treat corruption, invalid schemas, impossible transitions, and required missing artifacts as
  protocol failures, not ordinary task failures.
- The host owns polling and agent spawning. The kernel records observations and returns directives;
  it does not sleep, call a host API, or run as a daemon in the MVP.

Copy initial shapes from [templates](templates/). The canonical field rules and transition contracts
are in [references/contracts.md](references/contracts.md).
