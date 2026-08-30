#!/usr/bin/env python3
"""Run deterministic clean-loop scenarios through the public CLI."""

from __future__ import annotations

import argparse
import json
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
LOOP_SCRIPT = REPO_ROOT / "skills/clean-loop/scripts/loop.py"
FIXTURE_ROOT = REPO_ROOT / "dogfood/fixtures/tiny-app"
SCENARIO_ROOT = REPO_ROOT / "dogfood/scenarios"
CHECK_COMMAND = "python3 -m unittest discover -s tests -v"


class DogfoodError(RuntimeError):
    """A scenario did not satisfy its deterministic contract."""


def write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


class Transcript:
    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.entries: list[dict[str, object]] = []

    def normalize(self, value: str) -> str:
        return (
            value.replace(str(self.workspace), "<workspace>")
            .replace(str(REPO_ROOT), "<clean-loop>")
            .replace(sys.executable, "<python>")
        )

    def run(
        self,
        command: list[str],
        *,
        kind: str,
        expected: tuple[int, ...] = (0,),
    ) -> subprocess.CompletedProcess:
        completed = subprocess.run(
            command,
            cwd=self.workspace,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        entry = {
            "kind": kind,
            "cwd": "<workspace>",
            "command": self.normalize(shlex.join(command)),
            "exit_code": completed.returncode,
            "expected_exit_codes": list(expected),
            "stdout": self.normalize(completed.stdout),
            "stderr": self.normalize(completed.stderr),
        }
        self.entries.append(entry)
        if completed.returncode not in expected:
            raise DogfoodError(
                f"unexpected exit {completed.returncode}: {entry['command']}\n"
                f"stdout:\n{entry['stdout']}\nstderr:\n{entry['stderr']}"
            )
        return completed


class ScenarioHarness:
    def __init__(self, scenario: dict[str, object], workspace: Path):
        self.scenario = scenario
        self.workspace = workspace
        self.transcript = Transcript(workspace)
        self.run_dir = Path(".clean-loop/runs") / str(scenario["id"])
        self.recovery_steps = 0
        self.attempt_count = 0
        self.final_phase: str | None = None
        self.successor_phase: str | None = None

    def require(self, condition: bool, message: str) -> None:
        if not condition:
            raise DogfoodError(message)

    def host(
        self, command: list[str], expected: tuple[int, ...] = (0,)
    ) -> subprocess.CompletedProcess:
        return self.transcript.run(command, kind="host", expected=expected)

    def cli_raw(
        self, *arguments: str, expected: tuple[int, ...] = (0,)
    ) -> subprocess.CompletedProcess:
        command = [sys.executable, str(LOOP_SCRIPT), *arguments]
        return self.transcript.run(command, kind="kernel", expected=expected)

    def cli(self, *arguments: str) -> object:
        completed = self.cli_raw(*arguments)
        try:
            return json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise DogfoodError(f"kernel command returned invalid JSON: {arguments[0]}") from exc

    def initialize_workspace(self) -> None:
        self.host(["git", "init", "-q"])
        self.host(["git", "config", "user.name", "clean-loop dogfood"])
        self.host(["git", "config", "user.email", "dogfood@clean-loop.invalid"])
        self.host(["git", "add", "."])
        self.host(["git", "commit", "-q", "-m", "fixture baseline"])

    def initialize_run(self) -> None:
        run_dir = self.run_dir.as_posix()
        self.cli("init", run_dir, "--goal", str(self.scenario["goal"]))
        write_json(
            self.workspace / self.run_dir / "questions.json",
            [
                {
                    "id": "Q001",
                    "question": "What behavior demonstrates the reported greeting defect?",
                    "scope": ["tiny_app.py", "tests/**"],
                    "kind": "explore",
                    "reasoning_demand": "low",
                },
                {
                    "id": "Q002",
                    "question": "Which source boundary can fix it without widening scope?",
                    "scope": ["tiny_app.py"],
                    "kind": "explore",
                    "reasoning_demand": "low",
                },
            ],
        )
        self.cli("status", run_dir)
        findings = {
            "F001": {
                "id": "F001",
                "question_id": "Q001",
                "kind": "fact",
                "statement": "The whitespace test fails against the baseline greeting function.",
                "confidence": "high",
                "evidence": [{"type": "repo", "ref": "tests/test_tiny_app.py"}],
            },
            "F002": {
                "id": "F002",
                "question_id": "Q002",
                "kind": "fact",
                "statement": "The greeting function is the bounded normalization seam.",
                "confidence": "high",
                "evidence": [{"type": "repo", "ref": "tiny_app.py"}],
            },
        }
        for finding_id, finding in findings.items():
            write_json(self.workspace / self.run_dir / f"findings/{finding_id}.json", finding)
        self.cli(
            "transition",
            run_dir,
            "register-artifacts",
            "--artifacts",
            "findings/F002.json",
            "findings/F001.json",
            "--compact",
        )
        self.cli(
            "transition",
            run_dir,
            "set-phase",
            "--phase",
            "synthesizing",
            "--compact",
        )
        write_json(
            self.workspace / self.run_dir / "syntheses/000.json",
            {
                "revision": 0,
                "disposition": "ready",
                "findings": ["F001", "F002"],
                "open_conflicts": [],
                "invariants": [
                    {"id": "I001", "statement": "Plain names retain their greeting output."}
                ],
                "decision": "Normalize only at the greeting input boundary.",
            },
        )
        self.cli(
            "transition",
            run_dir,
            "register-artifact",
            "--artifact",
            "syntheses/000.json",
            "--compact",
        )
        next_action = self.cli("next", run_dir)
        self.require(
            isinstance(next_action, list)
            and next_action[0]["action"] == "set_phase"
            and "--phase planning" in next_action[0]["command"],
            "synthesizing did not produce actionable planning guidance",
        )
        self.cli(
            "transition",
            run_dir,
            "set-phase",
            "--phase",
            "planning",
            "--artifact",
            "syntheses/000.json",
            "--compact",
        )
        write_json(
            self.workspace / self.run_dir / "plans/000.json",
            {
                "revision": 0,
                "based_on_synthesis": 0,
                "tasks": [
                    {
                        "id": "T001",
                        "kind": "implement",
                        "reasoning_demand": "low",
                        "state": "pending",
                        "goal": str(self.scenario["goal"]),
                        "depends_on": [],
                        "basis": ["F001", "F002"],
                        "preserves": ["I001"],
                        "write_scope": ["tiny_app.py"],
                        "acceptance": ["All greeting tests pass."],
                        "checks": [CHECK_COMMAND],
                    }
                ],
            },
        )
        self.cli(
            "transition",
            run_dir,
            "register-artifact",
            "--artifact",
            "plans/000.json",
            "--compact",
        )
        self.cli(
            "transition",
            run_dir,
            "activate-plan",
            "--artifact",
            "plans/000.json",
            "--compact",
        )

    def first_execution(self) -> tuple[str, str]:
        started = self.cli(
            "transition",
            self.run_dir.as_posix(),
            "start-attempt",
            "--task",
            "T001",
            "--adapter",
            "deterministic",
            "--model",
            "fixture-worker",
            "--reasoning-effort",
            "low",
            "--at",
            "2026-08-30T00:00:00Z",
            "--compact",
        )
        self.require(isinstance(started, dict), "start-attempt did not return a summary")
        return str(started["attempt_id"]), str(started["execution_id"])

    def apply_worker_attempt(
        self,
        attempt: dict[str, object],
        attempt_id: str,
        execution_id: str,
    ) -> None:
        patch = SCENARIO_ROOT / str(attempt["patch"])
        self.host(["git", "apply", str(patch)])
        expected_exit = int(attempt["expected_test_exit"])
        checked = self.host(
            shlex.split(CHECK_COMMAND),
            expected=(expected_exit,),
        )
        changed = self.host(["git", "diff", "--name-only"]).stdout.splitlines()
        self.require(changed == ["tiny_app.py"], f"worker changed unexpected files: {changed}")
        write_json(
            self.workspace / self.run_dir / f"receipts/{attempt_id}.json",
            {
                "task_id": "T001",
                "attempt_id": attempt_id,
                "outcome": "completed",
                "execution": {
                    "id": execution_id,
                    "generation": int(execution_id.rsplit("E", 1)[1]),
                    "role": "worker",
                    "adapter": "deterministic",
                    "model": "fixture-worker",
                    "reasoning_effort": "low",
                },
                "changed_files": changed,
                "checks": [
                    {
                        "command": CHECK_COMMAND,
                        "exit_code": checked.returncode,
                        "summary": f"fixture suite exited {checked.returncode}",
                    }
                ],
                "assumptions": [],
                "unexpected_findings": [],
                "concerns": [],
            },
        )
        self.cli(
            "transition",
            self.run_dir.as_posix(),
            "submit",
            "--task",
            "T001",
            "--artifact",
            f"receipts/{attempt_id}.json",
            "--admit",
            "--compact",
        )

    def apply_verdict(self, attempt: dict[str, object], attempt_id: str) -> None:
        outcome = str(attempt["verdict"])
        write_json(
            self.workspace / self.run_dir / f"verdicts/{attempt_id}.json",
            {
                "scope": "task",
                "target": attempt_id,
                "outcome": outcome,
                "reasons": [f"Deterministic fixture evidence supports {outcome}."],
            },
        )
        base_arguments = [
            "transition",
            self.run_dir.as_posix(),
            "apply-verdict",
            "--task",
            "T001",
            "--artifact",
            f"verdicts/{attempt_id}.json",
            "--admit",
            "--compact",
        ]
        exercise_recovery = (
            outcome == "reject"
            and bool(self.scenario.get("exercise_missing_retry_time"))
            and self.recovery_steps == 0
        )
        if exercise_recovery:
            failed = self.cli_raw(*base_arguments, expected=(2,))
            self.require(
                "requires --at <UTC timestamp ending in Z>" in failed.stderr,
                "retry error did not explain the required timestamp",
            )
            self.recovery_steps += 1
        if outcome == "reject":
            base_arguments.extend(["--at", "2026-08-30T00:01:00Z"])
        self.cli(*base_arguments)

    def complete_run(self) -> None:
        run_dir = self.run_dir.as_posix()
        self.cli(
            "transition",
            run_dir,
            "set-phase",
            "--phase",
            "verifying",
            "--compact",
        )
        next_action = self.cli("next", run_dir)
        self.require(
            isinstance(next_action, list) and next_action[0]["action"] == "verify_run",
            "verifying did not request a run verdict",
        )
        verdict_path = "verdicts/run-plan-000.json"
        write_json(
            self.workspace / self.run_dir / verdict_path,
            {
                "scope": "run",
                "target": "plan-000",
                "outcome": "pass",
                "reasons": ["The goal, scope, diff, and acceptance checks pass."],
            },
        )
        self.cli(
            "transition",
            run_dir,
            "register-artifact",
            "--artifact",
            verdict_path,
            "--compact",
        )
        next_action = self.cli("next", run_dir)
        self.require(
            isinstance(next_action, list)
            and next_action[0]["action"] == "set_phase"
            and "--phase completed" in next_action[0]["command"],
            "registered run verdict did not produce completion guidance",
        )
        self.cli(
            "transition",
            run_dir,
            "set-phase",
            "--phase",
            "completed",
            "--artifact",
            verdict_path,
            "--compact",
        )
        summary = self.cli("validate", run_dir)
        self.require(isinstance(summary, dict) and summary["phase"] == "completed", "run failed")
        projection = self.cli("validate", run_dir, "--full")
        self.attempt_count = len(projection["tasks"]["T001"]["attempts"])
        self.final_phase = str(summary["phase"])

    def initialize_followup(self) -> None:
        followup_goal = self.scenario.get("followup_goal")
        if not followup_goal:
            return
        successor = Path(".clean-loop/runs") / f"{self.scenario['id']}-followup"
        created = self.cli(
            "init-next",
            successor.as_posix(),
            "--after",
            self.run_dir.as_posix(),
            "--goal",
            str(followup_goal),
        )
        self.require(
            created["predecessor"]["run_id"] == self.scenario["id"],
            "successor did not preserve predecessor evidence",
        )
        status = self.cli("status", successor.as_posix())
        self.require(status["next_actions"][0]["action"] == "explore", "successor is not usable")
        self.successor_phase = str(status["phase"])

    def run(self) -> None:
        self.initialize_workspace()
        self.initialize_run()
        attempt_id, execution_id = self.first_execution()
        attempts = self.scenario["attempts"]
        for index, attempt in enumerate(attempts):
            self.require(
                attempt_id == f"T001-A{index + 1:02d}",
                f"unexpected attempt identity: {attempt_id}",
            )
            self.apply_worker_attempt(attempt, attempt_id, execution_id)
            self.apply_verdict(attempt, attempt_id)
            if attempt["verdict"] == "accept":
                break
            status = self.cli("status", self.run_dir.as_posix())
            next_action = status["next_actions"][0]
            self.require(next_action["action"] == "observe_execution", "retry did not start")
            attempt_id = str(next_action["attempt_id"])
            execution_id = str(next_action["execution_id"])
        self.complete_run()
        self.initialize_followup()

    def report(self, passed: bool, error: str | None = None) -> dict[str, object]:
        kernel_entries = [item for item in self.transcript.entries if item["kind"] == "kernel"]
        host_entries = [item for item in self.transcript.entries if item["kind"] == "host"]
        protocol_errors = sum(item["exit_code"] != 0 for item in kernel_entries)
        output_bytes = sum(
            len(str(item["stdout"]).encode()) + len(str(item["stderr"]).encode())
            for item in kernel_entries
        )
        result: dict[str, object] = {
            "scenario": self.scenario["id"],
            "passed": passed,
            "kernel_commands": len(kernel_entries),
            "host_commands": len(host_entries),
            "protocol_errors": protocol_errors,
            "recovery_steps": self.recovery_steps,
            "kernel_output_bytes": output_bytes,
            "attempts": self.attempt_count,
            "final_phase": self.final_phase,
        }
        if self.successor_phase is not None:
            result["successor_phase"] = self.successor_phase
        if error is not None:
            result["error"] = error
        return result


def load_scenarios() -> dict[str, dict[str, object]]:
    scenarios = {}
    for path in sorted(SCENARIO_ROOT.glob("*.json")):
        scenario = json.loads(path.read_text(encoding="utf-8"))
        required = {"id", "description", "goal", "attempts", "expected_protocol_errors"}
        missing = required - set(scenario)
        if missing:
            raise DogfoodError(f"{path.name} missing fields: {', '.join(sorted(missing))}")
        scenario_id = scenario["id"]
        if scenario_id != path.stem or scenario_id in scenarios:
            raise DogfoodError(f"{path.name} has an invalid or duplicate scenario ID")
        attempts = scenario["attempts"]
        if not isinstance(attempts, list) or not attempts or len(attempts) > 2:
            raise DogfoodError(f"{path.name} must declare one or two attempts")
        if attempts[-1].get("verdict") != "accept" or any(
            attempt.get("verdict") not in {"accept", "reject"}
            or not (SCENARIO_ROOT / str(attempt.get("patch"))).is_file()
            or not isinstance(attempt.get("expected_test_exit"), int)
            for attempt in attempts
        ):
            raise DogfoodError(f"{path.name} has an invalid attempt sequence")
        scenarios[scenario_id] = scenario
    if not scenarios:
        raise DogfoodError("no dogfood scenarios found")
    return scenarios


def make_parser(scenario_ids: list[str]) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scenario",
        action="append",
        choices=scenario_ids,
        help="Run one scenario; repeat to select more than one. Defaults to all.",
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="Retain workspaces, transcripts, and the aggregate report under .dogfood/runs.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        scenarios = load_scenarios()
        args = make_parser(sorted(scenarios)).parse_args(argv)
        selected = list(dict.fromkeys(args.scenario or sorted(scenarios)))
        temporary = None
        if args.keep:
            retained_root = REPO_ROOT / ".dogfood/runs"
            retained_root.mkdir(parents=True, exist_ok=True)
            root = Path(tempfile.mkdtemp(prefix="run-", dir=retained_root))
        else:
            temporary = tempfile.TemporaryDirectory(prefix="clean-loop-dogfood-")
            root = Path(temporary.name)

        reports = []
        for scenario_id in selected:
            workspace = root / scenario_id
            shutil.copytree(FIXTURE_ROOT, workspace)
            harness = ScenarioHarness(scenarios[scenario_id], workspace)
            error = None
            try:
                harness.run()
                report = harness.report(True)
                expected_errors = int(scenarios[scenario_id]["expected_protocol_errors"])
                if report["protocol_errors"] != expected_errors:
                    raise DogfoodError(
                        f"expected {expected_errors} protocol errors, got {report['protocol_errors']}"
                    )
            except Exception as exc:  # noqa: BLE001 - each scenario must report and preserve failure
                error = f"{type(exc).__name__}: {exc}"
                report = harness.report(False, error)
            write_json(workspace / "transcript.json", harness.transcript.entries)
            reports.append(report)

        aggregate: dict[str, object] = {
            "passed": all(report["passed"] for report in reports),
            "scenarios": reports,
        }
        if args.keep:
            aggregate["retained_root"] = str(root)
            write_json(root / "report.json", aggregate)
        print(json.dumps(aggregate, indent=2, sort_keys=True))
        if temporary is not None:
            temporary.cleanup()
        return 0 if aggregate["passed"] else 1
    except (DogfoodError, OSError, json.JSONDecodeError) as exc:
        print(f"dogfood error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
