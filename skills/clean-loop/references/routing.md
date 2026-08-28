# Routing policy

Routing is runtime policy, not plan data. Select the host adapter, actual model, and reasoning
effort only when dispatching an attempt, then record all three in its execution projection and
receipt.

Use this capability policy against the models available in the current host:

| Kind        | Demand   | Capability and effort                            |
| ----------- | -------- | ------------------------------------------------ |
| `explore`   | `low`    | economical reasoning model, low effort           |
| `explore`   | `medium` | stronger reasoning model, low or medium effort   |
| `explore`   | `high`   | Meta-grade reasoning model                       |
| `implement` | `low`    | fast coding model, low effort                    |
| `implement` | `medium` | standard coding/reasoning model, medium effort   |
| `implement` | `high`   | return to Meta for decomposition before dispatch |
| `verify`    | `low`    | economical independent verifier                  |
| `verify`    | `medium` | stronger independent verifier, medium effort     |
| `verify`    | `high`   | Meta-grade verifier                              |

Prefer a verifier with context independent from the worker. Model-family independence is useful when
available but is not an MVP guarantee.

Do not silently promote a task because retries are exhausted. Block at the loop limit and report
what authority or plan change is needed.

Security, IAM, production, and migration changes require Meta review regardless of reasoning demand.
This is a hard MVP rule, not a hidden `risk` field.
