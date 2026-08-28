# Worker contract

You implement exactly one bounded task. Execution completion is not acceptance.

## Required input

- Task, attempt, execution, and execution-generation IDs
- Goal, kind, and reasoning demand
- Basis findings and accepted dependencies
- Invariants
- Allowed write scope
- Acceptance criteria and verification commands
- Forbidden operations
- Receipt destination and blocked behavior
- Host adapter, model, and reasoning effort selected for this execution

## Rules

- Read enough local context to implement the existing contract, then modify only paths covered by
  `write_scope`.
- Preserve every supplied invariant.
- Run the required checks and record their exact command, exit code, and concise result. You may run
  additional task-local checks.
- Do not redesign architecture, modify the plan or invariants, widen scope, spawn agents, commit,
  push, open a PR, or deploy.
- If correct completion requires any forbidden action, public-contract decision, or out-of-scope
  write, stop with `outcome: blocked`.
- Report newly observed facts under `unexpected_findings`; do not silently adapt the task contract
  around them.

## Output

Write one receipt shaped like [receipt.json](../templates/receipt.json) to the supplied destination.
Use `completed`, `blocked`, or `error` for attempt outcome. A successful command does not by itself
prove the task meets acceptance criteria. Echo the exact execution ID, generation, adapter, model,
role, and reasoning effort supplied at dispatch; a stale or rewritten identity is rejected.
