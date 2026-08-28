# Canonical vocabulary

Use each field for exactly one question.

| Object    | Field                | Values                                                                                              | Question                                              |
| --------- | -------------------- | --------------------------------------------------------------------------------------------------- | ----------------------------------------------------- |
| Run       | `phase`              | `exploring`, `synthesizing`, `planning`, `executing`, `verifying`, `completed`, `blocked`, `failed` | Where is the whole run?                               |
| Task      | `kind`               | `explore`, `implement`, `verify`                                                                    | What does the task do?                                |
| Task      | `reasoning_demand`   | `low`, `medium`, `high`                                                                             | How much semantic judgment does the task require?     |
| Task      | `state`              | `pending`, `running`, `submitted`, `accepted`, `invalidated`, `skipped`                             | Where is the task in its lifecycle?                   |
| Attempt   | `outcome`            | `completed`, `blocked`, `error`                                                                     | How did this task attempt terminate?                  |
| Execution | `state`              | `starting`, `active`, `awaiting_receipt`, `unreachable`, `terminated`, `lost`                       | What does the host currently know about this runtime? |
| Execution | `termination_reason` | `completed`, `cancelled`, `interrupted`, `host_error`, `lease_expired`, `receipt_timeout`           | Why did runtime supervision end?                      |
| Execution | `role`               | `meta`, `explorer`, `worker`, `verifier`                                                            | What responsibility and authority did the agent have? |
| Execution | `adapter`            | host adapter identifier                                                                             | Which host surface launched and observes the agent?   |
| Execution | `model`              | runtime model identifier                                                                            | Which model actually executed?                        |
| Execution | `reasoning_effort`   | runtime effort identifier                                                                           | How much reasoning was allocated to this execution?   |
| Finding   | `kind`               | `fact`, `inference`, `unknown`                                                                      | What epistemic status does the claim have?            |
| Verdict   | `outcome`            | task: `accept`, `reject`, `replan`, `block`; run: `pass`, `replan`, `block`                         | Should the submitted result advance state?            |

## Reasoning demand

- `low`: the solution is nearly specified; there are few choices and no architectural judgment.
- `medium`: local behavior and abstractions must be interpreted; multiple local implementations may
  be plausible.
- `high`: system-level interpretation, public contracts, or trade-offs remain.

Reasoning demand belongs to the task. Adapter, model, and reasoning effort belong to the execution
selected at runtime. `observed_state` is a host observation and must not be reused as a task state
or attempt outcome.

## Execution supervision

- `starting`: dispatch is recorded but the host has not yet reported the agent active.
- `active`: the host reported progress and renewed the lease.
- `unreachable`: the host could not establish current state; this is not yet failure.
- `awaiting_receipt`: the host completed but the attempt receipt has not arrived.
- `terminated`: the host reported a terminal result or a receipt closed the execution.
- `lost`: lease or receipt grace expired and workspace reconciliation is required.

Never infer `lost` from one failed poll. `unreachable` must survive the configured grace period
before the kernel emits `reconcile_workspace`.

## IDs

Use monotonically allocated run-local IDs and never reuse them:

```text
Q001  F001  I001  T001  T001-A01  T001-A01-E01
```

Plan and synthesis filenames use zero-padded revisions such as `000.json` and `001.json`. Keys and
filenames are `lower_snake_case`; run slugs are `kebab-case`; enum values are lowercase.
