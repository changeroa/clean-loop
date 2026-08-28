# Host supervision contract

The host supervisor connects `clean-loop` to an agent runtime. The kernel owns legal state; adapters
own external side effects.

```text
host tools -> adapter observation -> kernel transition -> one directive -> host action
```

## Adapter capabilities

An adapter exposes the smallest mapping the host can support:

```text
spawn(task contract, execution ID) -> external ID
poll(external ID) -> observed state and optional progress
collect_result(external ID) -> receipt or no receipt
interrupt(external ID, reason) -> observed terminal state
```

Map host-specific statuses to exactly one observation:

| Host meaning                               | `observed_state` |
| ------------------------------------------ | ---------------- |
| Running or made observable progress        | `active`         |
| Finished normally                          | `completed`      |
| Explicitly cancelled                       | `cancelled`      |
| Interrupted by the host or operator        | `interrupted`    |
| Host reported execution failure            | `error`          |
| Query failed or current state is uncertain | `unknown`        |

Do not map a query timeout to `error`. Use `unknown`; the lease and grace policy decides whether the
execution becomes `lost`. `unreachable` is a derived kernel state, not a host observation.

## Supervisor cycle

For each cycle:

1. Run `supervise --at <UTC timestamp>`.
2. For `dispatch`, route the task, run `start-attempt`, then spawn with the returned execution ID.
3. For `poll`, query the adapter and record `observe-execution`. Include the stable external host ID
   and bounded progress metadata when available.
4. For `collect_receipt`, retrieve and validate the result. Do not invent worker success when the
   receipt is missing.
5. For `reconcile_workspace`, inspect the declared write scope and actual diff before deciding what
   happened. Persist an `outcome: error` receipt that identifies the stopped execution and observed
   partial state. Meta then dispatches a fresh verifier and applies its task verdict; retry is not
   automatic.
6. For `verify_task` or `verify_run`, dispatch a fresh verifier according to the normal contracts.
7. For `wait`, stop the cycle until the host has a new observation or the next bounded poll time.

The host chooses the polling interval. Keep it comfortably shorter than `lease_seconds`; polling
faster than the host can produce meaningful state only adds load. A polling directive includes the
current lease, unreachable-grace, or receipt `deadline`; observe again before that timestamp or let
the next supervision cycle advance the timeout.

## Fencing and late results

Pass `execution.id` to the spawned agent and require the same ID and generation in its receipt. The
kernel accepts only the active task's current attempt and execution. Preserve rejected late output
outside the run's registered artifact namespace for diagnosis; never overwrite the accepted or
supervisor-produced receipt.

Execution identity is a stale-result fence, not a security credential. The MVP assumes agents and
adapters report their assigned identity honestly. It creates one execution generation per attempt;
supporting another generation within the same attempt requires a later contract.

## Restart behavior

After a supervisor restart, run `validate`, then call `supervise` with the current UTC time. The
kernel reconstructs the next action from `run.json`; no in-memory agent conversation is a source of
truth. If the adapter cannot reconnect to the recorded `external_id`, report `unknown` and let the
lease/grace path reconcile it.

## MVP boundary

This contract defines an in-session control loop, not a daemon. It does not implement background
scheduling, leader election, concurrent orchestrators, distributed locks, or multi-machine recovery.
Exactly one supervisor may mutate a run at a time. Add concurrent writers or persistent background
supervision only under a separate architecture decision.
