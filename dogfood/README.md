# Dogfooding clean-loop

This harness exercises the public `loop.py` CLI in disposable Git repositories. It complements the
unit suite by protecting known command, output, recovery, and host-boundary workflows. A live-host
run is still required to evaluate whether an agent can discover and understand those workflows.

## Run it

```bash
npm run dogfood
npm run dogfood -- --scenario retry
npm run dogfood:keep
```

The default run executes every manifest in `scenarios/`. Without `--keep`, workspaces are temporary.
With `--keep`, the harness writes isolated workspaces, `transcript.json`, and `report.json` beneath
`.dogfood/runs/`. A retained workspace contains the fixture repository and its complete
`.clean-loop` history.

Each report records:

- public kernel commands and deterministic host commands
- expected protocol errors and recovery steps
- kernel output size, attempt count, and final phase
- the retained workspace path when applicable

These counters are diagnostics, not an independent UX score. Ground truth enters through the fixture
test process and kernel projection validation; the harness itself chooses the scenario and scripted
verdicts.

## Scenario contract

Scenario manifests are JSON so the harness keeps the repository's standard-library-only runtime.
Each manifest declares a bounded goal and one or more attempts:

```json
{
  "id": "happy-path",
  "goal": "Normalize surrounding whitespace in greetings",
  "attempts": [
    {
      "patch": "patches/greeting-strip.patch",
      "expected_test_exit": 0,
      "verdict": "accept"
    }
  ],
  "expected_protocol_errors": 0
}
```

The fixture intentionally starts with a failing whitespace test. Patches represent sequential,
deterministic worker output. The harness itself performs the host responsibilities:

1. copy `fixtures/tiny-app` and create a baseline Git commit;
2. initialize a run and write host-owned questions and artifacts;
3. invoke only the public CLI for registration and transitions;
4. apply each worker patch and run the declared acceptance command;
5. submit execution-fenced receipts and scripted verifier verdicts based on fixture checks;
6. validate the completed projection and any linked successor run.

The `retry` scenario deliberately omits the retry timestamp once, verifies the actionable protocol
error, and retries the unchanged verdict transition correctly. This is an expected recovery, not a
scenario failure.

## Adding a scenario

Keep scenarios narrow and deterministic. Add a manifest and any sequential Git patches it needs,
then run both the focused scenario and the full check:

```bash
npm run dogfood -- --scenario <scenario-id> --keep
npm run check
```

Do not put generated run directories or transcripts in source control. Promote a useful failure into
a minimal manifest, fixture change, or regression test instead.

## Live-host boundary

This harness does not claim to exercise model selection, native agent status mapping, credentials,
or Android Termux process behavior. A later live adapter should give a real host only the natural
language goal, installed skill, and sandbox path, then preserve its transcript in the same report
shape. Live runs should be manual or scheduled rather than pull-request gates because they are
nondeterministic and may incur external cost.
