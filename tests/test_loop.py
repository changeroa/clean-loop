import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

SCRIPT = Path(__file__).parents[1] / "skills/clean-loop/scripts/loop.py"
SPEC = importlib.util.spec_from_file_location("clean_loop_runtime", SCRIPT)
loop = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(loop)


class CleanLoopTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "scenario-run"
        loop.init_run(self.root, "Ship the bounded change", None)
        loop.write(
            self.root / "questions.json",
            [
                {
                    "id": "Q001",
                    "question": "What behavior must remain?",
                    "scope": ["src/**"],
                    "kind": "explore",
                    "reasoning_demand": "low",
                }
            ],
        )

    def tearDown(self):
        self.temp.cleanup()

    def event(self, event, **values):
        args = {
            "event": event,
            "artifact": None,
            "phase": None,
            "task": None,
            "reason": None,
            "execution": None,
            "adapter": "test-host",
            "model": "standard",
            "reasoning_effort": "low",
            "at": "2026-08-28T00:00:00Z",
            "observed_state": None,
            "external_id": None,
            "progress": None,
        }
        args.update(values)
        return loop.transition(self.root, SimpleNamespace(**args))

    def cli(self, *args):
        completed = subprocess.run(
            [sys.executable, str(SCRIPT), *args], capture_output=True, text=True, check=False
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)

    def artifact(self, relative, data):
        loop.write(self.root / relative, data)
        return self.event("register-artifact", artifact=relative)

    def finding(self):
        self.artifact(
            "findings/F001.json",
            {
                "id": "F001",
                "question_id": "Q001",
                "kind": "fact",
                "statement": "The focused behavior has a stable test seam",
                "confidence": "high",
                "evidence": [{"type": "repo", "ref": "tests/test_feature.py#test_behavior"}],
            },
        )

    def synthesis(self, disposition="ready", revision=0):
        data = {
            "revision": revision,
            "disposition": disposition,
            "findings": ["F001"],
            "open_conflicts": [],
            "invariants": [{"id": "I001", "statement": "Existing behavior remains stable"}],
        }
        if disposition == "ready":
            data["decision"] = "Implement only the bounded seam"
        rel = f"syntheses/{revision:03d}.json"
        self.artifact(rel, data)
        return rel

    def plan(self, revision=0, tasks=None):
        if tasks is None:
            tasks = [self.task("T001", "medium")]
        rel = f"plans/{revision:03d}.json"
        self.artifact(rel, {"revision": revision, "based_on_synthesis": 0, "tasks": tasks})
        return rel

    @staticmethod
    def task(task_id, demand, depends=None):
        return {
            "id": task_id,
            "kind": "implement",
            "reasoning_demand": demand,
            "state": "pending",
            "goal": f"Implement {task_id}",
            "depends_on": depends or [],
            "basis": ["F001"],
            "preserves": ["I001"],
            "write_scope": ["src/**", "tests/**"],
            "acceptance": ["Focused behavior is evidenced"],
            "checks": ["python3 -m unittest"],
        }

    def prepare_execution(self, tasks=None):
        self.finding()
        self.event("set-phase", phase="synthesizing")
        synthesis = self.synthesis()
        self.event("set-phase", phase="planning", artifact=synthesis)
        plan = self.plan(tasks=tasks)
        self.event("activate-plan", artifact=plan)

    def submit(
        self,
        task_id,
        attempt_id,
        model="standard",
        role="worker",
        changed_files=None,
        outcome="completed",
        checks=None,
        unexpected_findings=None,
        concerns=None,
    ):
        if changed_files is None:
            changed_files = [f"src/{task_id.lower()}.py"]
        if checks is None:
            checks = [{"command": "python3 -m unittest", "exit_code": 0, "summary": "1 passed"}]
        receipt = {
            "task_id": task_id,
            "attempt_id": attempt_id,
            "outcome": outcome,
            "execution": {
                "id": loop.load(self.root / "run.json")["tasks"][task_id]["current_execution"],
                "generation": 1,
                "role": role,
                "adapter": "test-host",
                "model": model,
                "reasoning_effort": "low",
            },
            "changed_files": changed_files,
            "checks": checks,
            "assumptions": [],
            "unexpected_findings": unexpected_findings or [],
            "concerns": concerns or [],
        }
        rel = f"receipts/{attempt_id}.json"
        self.artifact(rel, receipt)
        self.event("submit", task=task_id, artifact=rel)

    def verdict(self, target, outcome, scope="task", reasons=None):
        name = target if scope == "task" else f"run-{target}"
        rel = f"verdicts/{name}.json"
        self.artifact(
            rel,
            {
                "scope": scope,
                "target": target,
                "outcome": outcome,
                "reasons": reasons or [f"Evidence supports {outcome}"],
            },
        )
        return rel

    def accept(self, task_id, attempt_id):
        verdict = self.verdict(attempt_id, "accept")
        self.event("apply-verdict", task=task_id, artifact=verdict)

    def supervise(self, at):
        run = loop.validate_run(self.root)
        return loop.supervision_directive(self.root, run, at)

    def test_scenario_a_normal_completion(self):
        tasks = [self.task("T001", "medium"), self.task("T002", "low", ["T001"])]
        self.prepare_execution(tasks)
        self.assertEqual(loop.runnable(self.root, loop.validate_run(self.root))["id"], "T001")

        self.assertEqual(self.event("start-attempt", task="T001")["attempt_id"], "T001-A01")
        self.submit("T001", "T001-A01")
        self.accept("T001", "T001-A01")
        self.assertEqual(loop.runnable(self.root, loop.validate_run(self.root))["id"], "T002")

        self.event("start-attempt", task="T002", model="spark")
        self.submit("T002", "T002-A01", model="spark")
        self.accept("T002", "T002-A01")
        self.event("set-phase", phase="verifying")
        with self.assertRaises(loop.ProtocolError):
            self.event("set-phase", phase="completed")

        run_verdict = self.verdict("plan-000", "pass", scope="run")
        self.event("set-phase", phase="completed", artifact=run_verdict)
        run = loop.validate_run(self.root)
        self.assertEqual(run["phase"], "completed")

    def test_cli_normal_completion_lifecycle(self):
        cli_root = Path(self.temp.name) / "cli-lifecycle"
        self.cli("init", str(cli_root), "--goal", "Exercise the public lifecycle")
        loop.write(
            cli_root / "questions.json",
            [
                {
                    "id": "Q001",
                    "question": "What must remain stable?",
                    "scope": ["src/**"],
                    "kind": "explore",
                    "reasoning_demand": "low",
                }
            ],
        )
        artifacts = {
            "findings/F001.json": {
                "id": "F001",
                "question_id": "Q001",
                "kind": "fact",
                "statement": "The seam is stable",
                "confidence": "high",
                "evidence": [{"type": "repo", "ref": "src/example.py"}],
            },
            "syntheses/000.json": {
                "revision": 0,
                "disposition": "ready",
                "findings": ["F001"],
                "open_conflicts": [],
                "invariants": [{"id": "I001", "statement": "The seam remains stable"}],
                "decision": "Use the seam",
            },
            "plans/000.json": {
                "revision": 0,
                "based_on_synthesis": 0,
                "tasks": [self.task("T001", "low")],
            },
            "receipts/T001-A01.json": {
                "task_id": "T001",
                "attempt_id": "T001-A01",
                "outcome": "completed",
                "execution": {
                    "id": "T001-A01-E01",
                    "generation": 1,
                    "role": "worker",
                    "adapter": "test-host",
                    "model": "test-model",
                    "reasoning_effort": "low",
                },
                "changed_files": ["src/t001.py"],
                "checks": [{"command": "python3 -m unittest", "exit_code": 0, "summary": "passed"}],
                "assumptions": [],
                "unexpected_findings": [],
                "concerns": [],
            },
            "verdicts/T001-A01.json": {
                "scope": "task",
                "target": "T001-A01",
                "outcome": "accept",
                "reasons": ["accepted"],
            },
            "verdicts/run-plan-000.json": {
                "scope": "run",
                "target": "plan-000",
                "outcome": "pass",
                "reasons": ["passed"],
            },
        }
        for path in ["findings/F001.json", "syntheses/000.json"]:
            loop.write(cli_root / path, artifacts[path])
            self.cli("transition", str(cli_root), "register-artifact", "--artifact", path)
        self.cli("transition", str(cli_root), "set-phase", "--phase", "synthesizing")
        self.cli(
            "transition",
            str(cli_root),
            "set-phase",
            "--phase",
            "planning",
            "--artifact",
            "syntheses/000.json",
        )
        loop.write(cli_root / "plans/000.json", artifacts["plans/000.json"])
        self.cli("transition", str(cli_root), "register-artifact", "--artifact", "plans/000.json")
        self.cli("transition", str(cli_root), "activate-plan", "--artifact", "plans/000.json")
        self.assertEqual(self.cli("runnable", str(cli_root))["id"], "T001")
        self.assertEqual(
            self.cli(
                "transition",
                str(cli_root),
                "start-attempt",
                "--task",
                "T001",
                "--adapter",
                "test-host",
                "--model",
                "test-model",
                "--reasoning-effort",
                "low",
                "--at",
                "2026-08-28T00:00:00Z",
            )["attempt_id"],
            "T001-A01",
        )
        for path in ["receipts/T001-A01.json", "verdicts/T001-A01.json"]:
            loop.write(cli_root / path, artifacts[path])
            self.cli("transition", str(cli_root), "register-artifact", "--artifact", path)
        self.cli(
            "transition",
            str(cli_root),
            "submit",
            "--task",
            "T001",
            "--artifact",
            "receipts/T001-A01.json",
        )
        self.cli(
            "transition",
            str(cli_root),
            "apply-verdict",
            "--task",
            "T001",
            "--artifact",
            "verdicts/T001-A01.json",
        )
        self.cli("transition", str(cli_root), "set-phase", "--phase", "verifying")
        loop.write(cli_root / "verdicts/run-plan-000.json", artifacts["verdicts/run-plan-000.json"])
        self.cli(
            "transition",
            str(cli_root),
            "register-artifact",
            "--artifact",
            "verdicts/run-plan-000.json",
        )
        self.cli(
            "transition",
            str(cli_root),
            "set-phase",
            "--phase",
            "completed",
            "--artifact",
            "verdicts/run-plan-000.json",
        )
        self.assertEqual(self.cli("validate", str(cli_root))["phase"], "completed")

    def test_scenario_b_retry_keeps_plan_revision(self):
        self.prepare_execution()
        self.event("start-attempt", task="T001")
        self.submit("T001", "T001-A01")
        reject = self.verdict("T001-A01", "reject")
        result = self.event("apply-verdict", task="T001", artifact=reject)
        self.assertEqual(result["task"]["current_attempt"], "T001-A02")
        self.assertEqual(loop.validate_run(self.root)["active_plan"], 0)

        self.submit("T001", "T001-A02")
        self.accept("T001", "T001-A02")
        run = loop.validate_run(self.root)
        self.assertEqual(run["tasks"]["T001"]["state"], "accepted")
        self.assertEqual(run["tasks"]["T001"]["attempts"], ["T001-A01", "T001-A02"])

    def test_scenario_c_replan_creates_immutable_revision(self):
        self.prepare_execution()
        original_hash = loop.digest(self.root / "plans/000.json")
        self.event("start-attempt", task="T001")
        self.submit("T001", "T001-A01")
        replan = self.verdict("T001-A01", "replan")
        self.event("apply-verdict", task="T001", artifact=replan)
        self.assertEqual(loop.validate_run(self.root)["phase"], "planning")
        with self.assertRaisesRegex(loop.ProtocolError, "strictly newer"):
            self.event("activate-plan", artifact="plans/000.json")

        next_plan = self.plan(revision=1, tasks=[self.task("T002", "low")])
        self.event("activate-plan", artifact=next_plan)
        run = loop.validate_run(self.root)
        self.assertEqual(run["active_plan"], 1)
        self.assertEqual(run["tasks"]["T001"]["state"], "invalidated")
        self.assertEqual(loop.digest(self.root / "plans/000.json"), original_hash)

    def test_scenario_d_authority_block(self):
        self.finding()
        self.event("set-phase", phase="synthesizing")
        synthesis = self.synthesis("needs_authority")
        self.event("set-phase", phase="blocked", artifact=synthesis)
        run = loop.validate_run(self.root)
        self.assertEqual(run["phase"], "blocked")
        self.assertIn("external authority", run["blocked_reasons"][0])

    def test_cycle_is_rejected(self):
        plan = {
            "revision": 0,
            "based_on_synthesis": 0,
            "tasks": [
                self.task("T001", "low", ["T002"]),
                self.task("T002", "low", ["T001"]),
            ],
        }
        with self.assertRaisesRegex(loop.ProtocolError, "dependency cycle"):
            loop.validate_plan(plan, "plans/000.json")

    def test_implement_high_requires_decomposition(self):
        plan = {"revision": 0, "based_on_synthesis": 0, "tasks": [self.task("T001", "high")]}
        with self.assertRaisesRegex(loop.ProtocolError, "must be decomposed"):
            loop.validate_plan(plan, "plans/000.json")

    def test_execution_is_serial_even_without_dependencies(self):
        self.prepare_execution([self.task("T001", "low"), self.task("T002", "low")])
        self.event("start-attempt", task="T001")
        run = loop.validate_run(self.root)
        self.assertIsNone(loop.runnable(self.root, run))
        with self.assertRaisesRegex(loop.ProtocolError, "not next runnable"):
            self.event("start-attempt", task="T002")

    def test_explore_and_verify_task_receipts_can_be_accepted(self):
        explore = self.task("T001", "low")
        explore.update(kind="explore", write_scope=[])
        verify = self.task("T002", "low", ["T001"])
        verify.update(kind="verify", write_scope=[])
        self.prepare_execution([explore, verify])
        self.event("start-attempt", task="T001")
        self.submit("T001", "T001-A01", role="explorer", changed_files=[])
        self.accept("T001", "T001-A01")
        self.event("start-attempt", task="T002")
        self.submit("T002", "T002-A01", role="verifier", changed_files=[])
        self.accept("T002", "T002-A01")
        self.assertEqual(loop.validate_run(self.root)["tasks"]["T002"]["state"], "accepted")

    def test_accept_rejects_objectively_ineligible_receipt_evidence(self):
        cases = [
            ("blocked", None, None, None, "completed receipt"),
            ("error", None, None, None, "completed receipt"),
            (
                "completed",
                [{"command": "python3 -m unittest", "exit_code": 1, "summary": "failed"}],
                None,
                None,
                "checks must succeed",
            ),
        ]
        for outcome, checks, findings, concerns, message in cases:
            with self.subTest(outcome=outcome, message=message):
                self.tearDown()
                self.setUp()
                self.prepare_execution()
                self.event("start-attempt", task="T001")
                self.submit(
                    "T001",
                    "T001-A01",
                    outcome=outcome,
                    checks=checks,
                    unexpected_findings=findings,
                    concerns=concerns,
                )
                verdict = self.verdict("T001-A01", "accept")
                with self.assertRaisesRegex(loop.ProtocolError, message):
                    self.event("apply-verdict", task="T001", artifact=verdict)

    def test_accept_defers_receipt_findings_and_concerns_to_verifier(self):
        self.prepare_execution()
        self.event("start-attempt", task="T001")
        self.submit(
            "T001",
            "T001-A01",
            unexpected_findings=["The first lint run found an issue that the final run resolved"],
            concerns=["The verifier must confirm the recorded concern no longer blocks acceptance"],
        )

        verdict = self.verdict(
            "T001-A01",
            "accept",
            reasons=[
                "The final lint run resolves the recorded finding",
                "The recorded concern has no remaining effect on the task contract",
            ],
        )
        self.event("apply-verdict", task="T001", artifact=verdict)

        self.assertEqual(loop.validate_run(self.root)["tasks"]["T001"]["state"], "accepted")

    def test_skip_rejects_active_pending_dependent(self):
        self.prepare_execution([self.task("T001", "low"), self.task("T002", "low", ["T001"])])
        with self.assertRaisesRegex(loop.ProtocolError, "active pending dependent"):
            self.event("skip-task", task="T001", reason="No longer needed")

    def test_validate_run_rejects_invalid_projection_metadata(self):
        mutations = [
            ("active_synthesis", "0", "active_synthesis"),
            ("active_plan", 99, "active plan"),
            ("exploration_followups", "zero", "follow-up counter"),
            ("blocked_reasons", "not a collection", "blocked_reasons"),
        ]
        for key, value, message in mutations:
            with self.subTest(key=key):
                self.tearDown()
                self.setUp()
                self.prepare_execution()
                run = loop.load(self.root / "run.json")
                run[key] = value
                loop.write(self.root / "run.json", run)
                with self.assertRaisesRegex(loop.ProtocolError, message):
                    loop.validate_run(self.root)

    def test_validate_completed_verdict_targets_active_plan(self):
        self.prepare_execution()
        self.event("start-attempt", task="T001")
        self.submit("T001", "T001-A01")
        self.accept("T001", "T001-A01")
        self.event("set-phase", phase="verifying")
        verdict = self.verdict("plan-999", "pass", scope="run")
        run = loop.load(self.root / "run.json")
        run["phase"] = "completed"
        run["run_verdict"] = verdict
        loop.write(self.root / "run.json", run)
        with self.assertRaisesRegex(loop.ProtocolError, "completed requires"):
            loop.validate_run(self.root)

    def test_plan_revision_limit_blocks_and_removes_candidate(self):
        self.prepare_execution()
        self.event("start-attempt", task="T001")
        self.submit("T001", "T001-A01")
        self.event("apply-verdict", task="T001", artifact=self.verdict("T001-A01", "replan"))
        self.event(
            "activate-plan", artifact=self.plan(revision=1, tasks=[self.task("T002", "low")])
        )
        self.event("start-attempt", task="T002")
        self.submit("T002", "T002-A01")
        self.event("apply-verdict", task="T002", artifact=self.verdict("T002-A01", "replan"))

        candidate = "plans/002.json"
        loop.write(
            self.root / candidate,
            {"revision": 2, "based_on_synthesis": 0, "tasks": [self.task("T003", "low")]},
        )
        result = self.event("register-artifact", artifact=candidate)
        self.assertEqual(result["phase"], "blocked")
        self.assertFalse((self.root / candidate).exists())
        self.assertEqual(loop.validate_run(self.root)["phase"], "blocked")

    def test_exploration_follow_up_limit_blocks_the_run(self):
        self.finding()
        self.event("set-phase", phase="synthesizing")
        first = self.synthesis("needs_more_evidence", revision=0)
        self.event("set-phase", phase="exploring", artifact=first)
        self.event("set-phase", phase="synthesizing")
        second = self.synthesis("needs_more_evidence", revision=1)
        result = self.event("set-phase", phase="exploring", artifact=second)
        self.assertEqual(result["phase"], "blocked")
        self.assertIn("follow-up limit", result["blocked_reasons"][-1])
        self.assertEqual(loop.validate_run(self.root)["phase"], "blocked")

    def test_second_rejection_blocks_after_attempt_limit(self):
        self.prepare_execution()
        self.event("start-attempt", task="T001")
        self.submit("T001", "T001-A01")
        self.event("apply-verdict", task="T001", artifact=self.verdict("T001-A01", "reject"))
        self.submit("T001", "T001-A02")
        result = self.event(
            "apply-verdict", task="T001", artifact=self.verdict("T001-A02", "reject")
        )
        self.assertEqual(result["phase"], "blocked")
        self.assertIn(
            "attempt limit exhausted", loop.validate_run(self.root)["blocked_reasons"][-1]
        )

    def test_start_at_limit_blocks_without_creating_another_attempt(self):
        self.prepare_execution()
        run = loop.load(self.root / "run.json")
        run["limits"]["max_task_attempts"] = 0
        loop.write(self.root / "run.json", run)
        result = self.event("start-attempt", task="T001")
        self.assertEqual(result["phase"], "blocked")
        self.assertEqual(loop.validate_run(self.root)["tasks"]["T001"]["attempts"], [])

    def test_planning_and_verifying_can_block(self):
        self.finding()
        self.event("set-phase", phase="synthesizing")
        synthesis = self.synthesis()
        self.event("set-phase", phase="planning", artifact=synthesis)
        planning = self.event("set-phase", phase="blocked", reason="Need authority")
        self.assertEqual(planning["phase"], "blocked")

        self.tearDown()
        self.setUp()
        self.prepare_execution()
        self.event("start-attempt", task="T001")
        self.submit("T001", "T001-A01")
        self.accept("T001", "T001-A01")
        self.event("set-phase", phase="verifying")
        verdict = self.verdict("plan-000", "block", scope="run")
        verifying = self.event("set-phase", phase="blocked", artifact=verdict)
        self.assertEqual(verifying["phase"], "blocked")

    def test_verifying_replan_and_task_terminal_transitions(self):
        self.prepare_execution()
        self.event("skip-task", task="T001", reason="No longer necessary")
        self.assertEqual(loop.validate_run(self.root)["tasks"]["T001"]["state"], "skipped")

        self.tearDown()
        self.setUp()
        self.prepare_execution()
        self.event("start-attempt", task="T001")
        self.submit("T001", "T001-A01")
        self.accept("T001", "T001-A01")
        invalidated = self.event("invalidate-task", task="T001", reason="A plan change is required")
        self.assertEqual(invalidated["state"], "invalidated")
        self.assertEqual(loop.validate_run(self.root)["phase"], "planning")

        self.tearDown()
        self.setUp()
        self.prepare_execution()
        self.event("start-attempt", task="T001")
        self.submit("T001", "T001-A01")
        self.accept("T001", "T001-A01")
        self.event("set-phase", phase="verifying")
        verdict = self.verdict("plan-000", "replan", scope="run")
        result = self.event("set-phase", phase="planning", artifact=verdict)
        self.assertEqual(result["phase"], "planning")

    def test_register_plan_rejects_missing_synthesis_without_invalidating_run(self):
        self.finding()
        candidate = "plans/000.json"
        loop.write(
            self.root / candidate,
            {
                "revision": 0,
                "based_on_synthesis": 999,
                "tasks": [self.task("T001", "low")],
            },
        )

        with self.assertRaisesRegex(loop.ProtocolError, "missing synthesis"):
            self.event("register-artifact", artifact=candidate)

        self.assertFalse((self.root / candidate).exists())
        run = loop.validate_run(self.root)
        self.assertNotIn(candidate, run["artifacts"])

    def test_failed_transition_records_corruption_when_run_is_readable(self):
        loop.write(self.root / "questions.json", {"corrupt": True})
        with self.assertRaises(loop.ProtocolError):
            loop.validate_run(self.root)
        result = self.event("set-phase", phase="failed", reason="questions.json is corrupt")
        self.assertEqual(result["phase"], "failed")
        self.assertEqual(loop.load(self.root / "run.json")["phase"], "failed")

    def test_receipt_role_must_match_task_kind(self):
        task = self.task("T001", "low")
        task["kind"] = "verify"
        task["write_scope"] = []
        self.prepare_execution([task])
        self.event("start-attempt", task="T001")
        with self.assertRaisesRegex(loop.ProtocolError, "stale execution"):
            self.submit("T001", "T001-A01")

    def test_registered_artifact_is_immutable(self):
        self.finding()
        path = self.root / "findings/F001.json"
        finding = loop.load(path)
        finding["statement"] = "Changed after registration"
        loop.write(path, finding)
        with self.assertRaisesRegex(loop.ProtocolError, "unregistered or changed"):
            loop.validate_run(self.root)

    def test_cli_init_and_validate(self):
        cli_root = Path(self.temp.name) / "cli-run"
        initialized = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "init",
                str(cli_root),
                "--goal",
                "Exercise the public CLI",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(initialized.returncode, 0, initialized.stderr)
        checked = subprocess.run(
            [sys.executable, str(SCRIPT), "validate", str(cli_root)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(checked.returncode, 0, checked.stderr)
        self.assertEqual(loop.load(cli_root / "run.json")["phase"], "exploring")

    def test_execution_observation_renews_lease_and_recovers_from_unknown(self):
        self.prepare_execution()
        execution = self.event("start-attempt", task="T001")
        self.assertEqual(execution["id"], "T001-A01-E01")
        active = self.event(
            "observe-execution",
            execution=execution["id"],
            observed_state="active",
            external_id="host-agent-17",
            progress={"stage": "editing"},
            at="2026-08-28T00:01:00Z",
        )
        self.assertEqual(active["state"], "active")
        self.assertEqual(active["lease_expires_at"], "2026-08-28T00:03:00Z")

        unknown = self.event(
            "observe-execution",
            execution=execution["id"],
            observed_state="unknown",
            at="2026-08-28T00:02:00Z",
        )
        self.assertEqual(unknown["state"], "unreachable")
        self.assertEqual(self.supervise("2026-08-28T00:02:30Z")["action"], "poll")

        recovered = self.event(
            "observe-execution",
            execution=execution["id"],
            observed_state="active",
            at="2026-08-28T00:02:40Z",
        )
        self.assertEqual(recovered["state"], "active")
        self.assertIsNone(recovered["unreachable_since"])

    def test_lease_expiry_requires_grace_before_workspace_reconciliation(self):
        self.prepare_execution()
        execution = self.event("start-attempt", task="T001")
        first = self.supervise("2026-08-28T00:02:01Z")
        self.assertEqual(first["action"], "poll")
        self.assertEqual(first["execution_state"], "unreachable")
        self.assertEqual(first["deadline"], "2026-08-28T00:03:00Z")

        expired = self.supervise("2026-08-28T00:03:01Z")
        self.assertEqual(expired["action"], "reconcile_workspace")
        self.assertEqual(expired["reason"], "lease_expired")
        run = loop.validate_run(self.root)
        self.assertEqual(run["executions"][execution["id"]]["state"], "lost")

    def test_completed_host_waits_for_receipt_then_times_out(self):
        self.prepare_execution()
        execution = self.event("start-attempt", task="T001")
        completed = self.event(
            "observe-execution",
            execution=execution["id"],
            observed_state="completed",
            at="2026-08-28T00:01:00Z",
        )
        self.assertEqual(completed["state"], "awaiting_receipt")
        self.assertEqual(self.supervise("2026-08-28T00:01:20Z")["action"], "collect_receipt")

        timed_out = self.supervise("2026-08-28T00:01:31Z")
        self.assertEqual(timed_out["action"], "reconcile_workspace")
        self.assertEqual(timed_out["reason"], "receipt_timeout")

    def test_late_completed_receipt_is_rejected_without_poisoning_artifacts(self):
        self.prepare_execution()
        self.event("start-attempt", task="T001")
        self.supervise("2026-08-28T00:03:01Z")

        with self.assertRaisesRegex(loop.ProtocolError, "supervised error receipt"):
            self.submit("T001", "T001-A01")

        self.assertFalse((self.root / "receipts/T001-A01.json").exists())
        self.assertNotIn("receipts/T001-A01.json", loop.validate_run(self.root)["artifacts"])

    def test_host_error_can_be_evidenced_and_retried_with_fencing(self):
        self.prepare_execution()
        first = self.event("start-attempt", task="T001")
        self.event(
            "observe-execution",
            execution=first["id"],
            observed_state="error",
            at="2026-08-28T00:00:10Z",
        )
        self.assertEqual(self.supervise("2026-08-28T00:00:11Z")["action"], "reconcile_workspace")
        self.submit(
            "T001",
            "T001-A01",
            outcome="error",
            concerns=["Host stopped before it could submit its own receipt"],
        )
        rejected = self.verdict("T001-A01", "reject")
        self.event(
            "apply-verdict",
            task="T001",
            artifact=rejected,
            at="2026-08-28T00:00:20Z",
        )
        run = loop.validate_run(self.root)
        self.assertEqual(run["tasks"]["T001"]["current_attempt"], "T001-A02")
        self.assertEqual(run["tasks"]["T001"]["current_execution"], "T001-A02-E01")
        with self.assertRaisesRegex(loop.ProtocolError, "stale attempt"):
            loop.check_receipt_target(
                run,
                {
                    "task_id": "T001",
                    "attempt_id": "T001-A01",
                    "outcome": "completed",
                    "execution": {"id": "T001-A01-E01"},
                    "concerns": [],
                },
            )

    def test_supervisor_cli_recovers_projection_after_process_restart(self):
        self.prepare_execution()
        self.event("start-attempt", task="T001")
        result = self.cli(
            "supervise",
            str(self.root),
            "--at",
            "2026-08-28T00:01:00Z",
        )
        self.assertEqual(result["action"], "poll")
        self.assertEqual(result["execution_id"], "T001-A01-E01")


if __name__ == "__main__":
    unittest.main()
