# Artifact and transition contracts

## Runtime layout

```text
.clean-loop/runs/<run-id>/
├── run.json
├── questions.json
├── findings/
├── syntheses/
├── plans/
├── receipts/
└── verdicts/
```

`run.json` is the mutable projection for phase, active revisions, current task and execution states,
leases, attempt counters, and registered artifact digests. Findings, syntheses, plans, receipts, and
verdicts are append-only immutable evidence.

`questions.json` is the mutable exploration queue. It is not registered as an immutable artifact:
Meta may define the questions before exploration and add a bounded follow-up only when the synthesis
permits it.

## Questions schema

`questions.json` is a JSON array. Every question is an object with:

- `id`: a unique `Q` identifier such as `Q001`.
- `question`: one non-empty question that must be answered before planning.
- `scope`: a list of repository-relative search scopes.
- `kind`: exactly `explore`.
- `reasoning_demand`: `low`, `medium`, or `high`.

Start from [questions.json](../templates/questions.json). Keep each question bounded enough for an
explorer to answer without changing source or runtime state. Findings must cite one of these IDs.

## Required artifact fields

- Question: `id`, `question`, `scope`, `kind`, `reasoning_demand`.
- Finding: `id`, `question_id`, `kind`, `statement`, `confidence`, `evidence`.
- Synthesis: `revision`, `disposition`, `findings`, `open_conflicts`, `invariants`, and `decision`
  when ready.
- Plan: `revision`, `based_on_synthesis`, `tasks`. Each task has `id`, `kind`, `reasoning_demand`,
  initial `state: pending`, `goal`, `depends_on`, `basis`, `preserves`, `write_scope`, `acceptance`,
  and `checks`.
- Receipt: `task_id`, `attempt_id`, `outcome`, execution ID/generation/role/adapter/model/effort,
  `changed_files`, `checks`, `assumptions`, `unexpected_findings`, `concerns`.
- Verdict: `scope`, `target`, `outcome`, and non-empty `reasons`.

`unexpected_findings` and `concerns` preserve semantic evidence, including items the worker believes
it resolved before submission. They do not mechanically prevent acceptance merely because the arrays
are non-empty. The independent verifier must address each recorded item and choose `accept`,
`reject`, `replan`, or `block` according to its current impact. The deterministic kernel enforces
objective receipt gates such as execution identity, outcome, scope, required checks, and exit codes;
it does not replace that semantic verdict with an emptiness check.

Paths stored in artifacts are repository-relative POSIX paths. A write scope ending in `/**` covers
its directory recursively; a plain path covers exactly that path. Receipt execution role must match
task kind: `explore -> explorer`, `implement -> worker`, and `verify -> verifier`. Explore and
verify receipts must have no changed files.

## Run phases

```text
exploring -> synthesizing
synthesizing -> planning | exploring | blocked
planning -> executing | blocked
executing -> verifying | planning | blocked
verifying -> completed | planning | blocked
```

`failed` is reserved for protocol corruption, an invalid schema, impossible transition, missing
required artifact, or orchestrator failure.

Phase gates:

- Synthesis disposition `ready` permits planning; `needs_more_evidence` permits one follow-up
  exploration; `needs_authority` blocks.
- Executing requires a validated active plan.
- Verifying requires every active task to be `accepted` or `skipped`.
- Completed requires a run verdict with `outcome: pass` targeting the active plan.

## Task and attempt transitions

```text
pending -> running -> submitted
submitted + accept -> accepted
submitted + reject -> running (a new attempt)
submitted + replan -> invalidated (run returns to planning)
submitted + block -> pending (run becomes blocked)
pending -> skipped
accepted -> invalidated
```

Starting an attempt is legal only for the next runnable task: it belongs to the active plan, is
pending, and all dependencies are accepted. Submission requires a matching immutable receipt. A task
verdict must target the current attempt.

For `accept`, the receipt must have `outcome: completed`, all required checks must be present and
successful, dependencies must still be accepted, and the verifier's reasons must account for any
recorded unexpected findings or concerns. A resolved item may remain in the immutable receipt as
history. An unresolved item requires `reject`, `replan`, or `block`; it is not erased to make the
receipt acceptable.

Each attempt has a fenced execution ID such as `T001-A01-E01`. A receipt must match the current
attempt and execution, including generation, adapter, model, role, and reasoning effort. A stale
receipt is rejected before registration. If an execution is already `terminated` or `lost`, only a
supervisor-produced `outcome: error` receipt with a concrete concern may close the attempt; a late
worker success receipt cannot advance state.

Retry preserves the entire task contract and plan revision. Replan is required when goal,
dependency, basis, invariant, write scope, acceptance, or system decision changes. Every plan
revision is immutable and a later revision supersedes it without editing it in place.

## Limits

```json
{
  "max_exploration_followups": 1,
  "max_task_attempts": 2,
  "max_plan_revisions": 2
}
```

The revision limit counts total plans, so the default permits `000` and `001`. Starting an attempt
consumes one attempt even if it later completes as `blocked` or `error`. When an automatic limit is
exhausted, record the reason and block the run.

## Supervision policy

```json
{
  "lease_seconds": 120,
  "unreachable_grace_seconds": 60,
  "receipt_grace_seconds": 30
}
```

The host owns polling and supplies explicit UTC timestamps. The kernel never reads the clock,
sleeps, accesses the network, or calls a model. An `active` observation renews the lease. An
`unknown` observation moves the execution to `unreachable` without declaring failure. `supervise`
advances an expired lease or receipt deadline deterministically and returns one directive. See
[supervision.md](supervision.md) for the adapter contract.

## Runtime commands

Run `loop.py --help` for the authoritative CLI. After `init`, use these event shapes with the
resolved script path:

```bash
python3 "$clean_loop_script" transition "$run_dir" register-artifact --artifact findings/F001.json
python3 "$clean_loop_script" transition "$run_dir" register-artifacts \
  --artifacts findings/F001.json findings/F002.json findings/F003.json
python3 "$clean_loop_script" transition "$run_dir" set-phase --phase synthesizing
python3 "$clean_loop_script" transition "$run_dir" set-phase --phase planning --artifact syntheses/000.json
python3 "$clean_loop_script" transition "$run_dir" activate-plan --artifact plans/000.json
python3 "$clean_loop_script" runnable "$run_dir"
python3 "$clean_loop_script" transition "$run_dir" start-attempt --task T001 \
  --adapter codex --model coding-model --reasoning-effort low --at 2026-08-28T00:00:00Z
python3 "$clean_loop_script" transition "$run_dir" observe-execution \
  --execution T001-A01-E01 --observed-state active --at 2026-08-28T00:00:10Z
python3 "$clean_loop_script" supervise "$run_dir" --at 2026-08-28T00:00:20Z
python3 "$clean_loop_script" transition "$run_dir" submit --task T001 \
  --artifact receipts/T001-A01.json --admit --compact
python3 "$clean_loop_script" transition "$run_dir" apply-verdict --task T001 \
  --artifact verdicts/T001-A01.json --admit --compact
python3 "$clean_loop_script" transition "$run_dir" set-phase --phase verifying
python3 "$clean_loop_script" transition "$run_dir" set-phase --phase completed \
  --artifact verdicts/run-plan-000.json
```

`register-artifacts` validates the complete proposed batch and updates `run.json` once; order inside
the batch does not matter, and any failure leaves the projection unchanged. Every path must name a
new file in a governed immutable group. `questions.json` and `run.json` are never registration
targets.

Without `--admit`, register every receipt and verdict before an event consumes it. With `--admit`,
`submit` or `apply-verdict` validates, registers, and consumes its artifact in one projection
update. Use `--at <UTC timestamp ending in Z>` for every host observation and attempt start; a
rejected task verdict requires `--at` when attempts remain because applying it starts the next
attempt.

`validate` returns a bounded summary. Use `validate --full` for the complete projection, `status`
for the summary plus legal next actions, and `next` for only those actions. After new requirements
arrive for a completed run, use:

```bash
python3 "$clean_loop_script" init-next "$next_run_dir" \
  --after "$run_dir" --goal "<new bounded goal>"
```

The successor records the completed predecessor run ID, run-verdict path, and `run.json` SHA-256.

`validate` checks both schema-level contracts and cross-artifact consistency.
