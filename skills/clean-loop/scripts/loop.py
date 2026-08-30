#!/usr/bin/env python3
"""Deterministic state runtime for the clean-loop skill."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath

PHASES = {
    "exploring",
    "synthesizing",
    "planning",
    "executing",
    "verifying",
    "completed",
    "blocked",
    "failed",
}
STATES = {"pending", "running", "submitted", "accepted", "invalidated", "skipped"}
EXECUTION_STATES = {
    "starting",
    "active",
    "awaiting_receipt",
    "unreachable",
    "terminated",
    "lost",
}
OBSERVED_STATES = {"active", "completed", "cancelled", "interrupted", "error", "unknown"}
TERMINATION_REASONS = {
    "completed",
    "cancelled",
    "interrupted",
    "host_error",
    "lease_expired",
    "receipt_timeout",
}
GROUPS = {"findings", "syntheses", "plans", "receipts", "verdicts"}
LIMITS = {"max_exploration_followups": 1, "max_task_attempts": 2, "max_plan_revisions": 2}
SUPERVISION = {"lease_seconds": 120, "unreachable_grace_seconds": 60, "receipt_grace_seconds": 30}
PATTERNS = {
    name: re.compile(pattern)
    for name, pattern in {
        "question": r"Q\d{3,}",
        "finding": r"F\d{3,}",
        "invariant": r"I\d{3,}",
        "task": r"T\d{3,}",
        "attempt": r"(T\d{3,})-A(\d{2,})",
        "execution": r"((T\d{3,})-A(\d{2,}))-E(\d{2,})",
    }.items()
}


class ProtocolError(ValueError):
    pass


def load(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"cannot read valid JSON from {path}: {exc}") from exc


def write(path, data):
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def require(obj, fields, label):
    if not isinstance(obj, dict):
        raise ProtocolError(f"{label} must be an object")
    missing = [field for field in fields if field not in obj]
    if missing:
        raise ProtocolError(f"{label} missing fields: {', '.join(missing)}")


def strings(value, label, nonempty=False):
    if (
        not isinstance(value, list)
        or any(not isinstance(item, str) for item in value)
        or (nonempty and not value)
    ):
        raise ProtocolError(f"{label} must be {'a non-empty' if nonempty else 'a'} list of strings")


def identifier(value, kind, label):
    if not isinstance(value, str) or PATTERNS[kind].fullmatch(value) is None:
        raise ProtocolError(f"{label} is not a valid {kind} ID")


def timestamp(value, label):
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ProtocolError(f"{label} must be a UTC timestamp ending in Z")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ProtocolError(f"{label} must be a valid UTC timestamp") from exc
    if parsed.tzinfo != timezone.utc:
        raise ProtocolError(f"{label} must be UTC")
    return parsed


def format_timestamp(value):
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def deadline(value, seconds):
    return format_timestamp(timestamp(value, "observation time") + timedelta(seconds=seconds))


def safe_path(root, relative):
    if not isinstance(relative, str):
        raise ProtocolError("artifact path is required")
    pure = PurePosixPath(relative)
    if pure.is_absolute() or ".." in pure.parts or len(pure.parts) < 2:
        raise ProtocolError("artifact path must be nested and run-relative")
    path = root.joinpath(*pure.parts).resolve()
    if root.resolve() not in path.parents:
        raise ProtocolError("artifact path escapes the run directory")
    return path


def validate_finding(data, rel):
    require(data, ["id", "question_id", "kind", "statement", "confidence", "evidence"], rel)
    identifier(data["id"], "finding", f"{rel}.id")
    identifier(data["question_id"], "question", f"{rel}.question_id")
    if data["kind"] not in {"fact", "inference", "unknown"} or data["confidence"] not in {
        "low",
        "medium",
        "high",
    }:
        raise ProtocolError(f"{rel} has invalid epistemic metadata")
    if (
        Path(rel).stem != data["id"]
        or not isinstance(data["statement"], str)
        or not data["statement"].strip()
        or not isinstance(data["evidence"], list)
    ):
        raise ProtocolError(f"{rel} has invalid filename, statement, or evidence")


def validate_synthesis(data, rel):
    require(data, ["revision", "disposition", "findings", "open_conflicts", "invariants"], rel)
    if (
        not isinstance(data["revision"], int)
        or data["revision"] < 0
        or Path(rel).stem != f"{data['revision']:03d}"
    ):
        raise ProtocolError(f"{rel} revision and filename disagree")
    if data["disposition"] not in {"ready", "needs_more_evidence", "needs_authority"}:
        raise ProtocolError(f"{rel} has invalid disposition")
    strings(data["findings"], f"{rel}.findings")
    if not isinstance(data["open_conflicts"], list) or not isinstance(data["invariants"], list):
        raise ProtocolError(f"{rel} conflicts and invariants must be lists")
    seen = set()
    for item in data["invariants"]:
        require(item, ["id", "statement"], f"{rel}.invariant")
        identifier(item["id"], "invariant", f"{rel}.invariant.id")
        if (
            item["id"] in seen
            or not isinstance(item["statement"], str)
            or not item["statement"].strip()
        ):
            raise ProtocolError(f"{rel} has duplicate or invalid invariant")
        seen.add(item["id"])
    if data["disposition"] == "ready" and (
        not isinstance(data.get("decision"), str) or not data["decision"].strip()
    ):
        raise ProtocolError(f"{rel} ready synthesis requires a decision")


def contract_hash(task):
    value = {key: item for key, item in task.items() if key != "state"}
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def validate_plan(data, rel):
    require(data, ["revision", "based_on_synthesis", "tasks"], rel)
    if (
        not isinstance(data["revision"], int)
        or data["revision"] < 0
        or Path(rel).stem != f"{data['revision']:03d}"
    ):
        raise ProtocolError(f"{rel} revision and filename disagree")
    if not isinstance(data["based_on_synthesis"], int) or not isinstance(data["tasks"], list):
        raise ProtocolError(f"{rel} has invalid synthesis reference or tasks")
    fields = [
        "id",
        "kind",
        "reasoning_demand",
        "state",
        "goal",
        "depends_on",
        "basis",
        "preserves",
        "write_scope",
        "acceptance",
        "checks",
    ]
    tasks = {}
    for task in data["tasks"]:
        require(task, fields, f"{rel}.task")
        identifier(task["id"], "task", f"{rel}.task.id")
        if (
            task["id"] in tasks
            or task["kind"] not in {"explore", "implement", "verify"}
            or task["reasoning_demand"] not in {"low", "medium", "high"}
            or task["state"] != "pending"
        ):
            raise ProtocolError(f"{rel} has duplicate ID or invalid task metadata")
        if task["kind"] == "implement" and task["reasoning_demand"] == "high":
            raise ProtocolError(f"{task['id']} implement/high must be decomposed before planning")
        if not isinstance(task["goal"], str) or not task["goal"].strip():
            raise ProtocolError(f"{task['id']} requires a goal")
        for key in ["depends_on", "basis", "preserves", "write_scope", "acceptance", "checks"]:
            strings(task[key], f"{task['id']}.{key}", key in {"acceptance", "checks"})
        tasks[task["id"]] = task
    if any(dep not in tasks for task in tasks.values() for dep in task["depends_on"]):
        raise ProtocolError(f"{rel} references a missing dependency")
    visiting, visited = set(), set()

    def visit(task_id):
        if task_id in visiting:
            raise ProtocolError(f"{rel} contains a dependency cycle at {task_id}")
        if task_id in visited:
            return
        visiting.add(task_id)
        for dependency in tasks[task_id]["depends_on"]:
            visit(dependency)
        visiting.remove(task_id)
        visited.add(task_id)

    for task_id in tasks:
        visit(task_id)


def validate_receipt(data, rel):
    require(
        data,
        [
            "task_id",
            "attempt_id",
            "outcome",
            "execution",
            "changed_files",
            "checks",
            "assumptions",
            "unexpected_findings",
            "concerns",
        ],
        rel,
    )
    identifier(data["task_id"], "task", f"{rel}.task_id")
    match = PATTERNS["attempt"].fullmatch(str(data["attempt_id"]))
    if (
        not match
        or match.group(1) != data["task_id"]
        or Path(rel).stem != data["attempt_id"]
        or data["outcome"] not in {"completed", "blocked", "error"}
    ):
        raise ProtocolError(f"{rel} has invalid identity or outcome")
    require(
        data["execution"],
        ["id", "generation", "role", "adapter", "model", "reasoning_effort"],
        f"{rel}.execution",
    )
    execution = data["execution"]
    execution_match = PATTERNS["execution"].fullmatch(str(execution["id"]))
    if (
        not execution_match
        or execution_match.group(1) != data["attempt_id"]
        or isinstance(execution["generation"], bool)
        or int(execution_match.group(4)) != execution["generation"]
        or execution["generation"] < 1
        or execution["role"] not in {"explorer", "worker", "verifier"}
        or not isinstance(execution["adapter"], str)
        or not execution["adapter"]
        or any(
            not isinstance(execution[key], str) or not execution[key]
            for key in ["model", "reasoning_effort"]
        )
    ):
        raise ProtocolError(f"{rel} has invalid execution metadata")
    if any(
        not isinstance(data[key], list)
        for key in ["changed_files", "checks", "assumptions", "unexpected_findings", "concerns"]
    ):
        raise ProtocolError(f"{rel} receipt collections must be lists")
    for check in data["checks"]:
        require(check, ["command", "exit_code", "summary"], f"{rel}.check")
        if (
            not isinstance(check["command"], str)
            or not isinstance(check["exit_code"], int)
            or not isinstance(check["summary"], str)
        ):
            raise ProtocolError(f"{rel} has invalid check evidence")


def validate_verdict(data, rel):
    require(data, ["scope", "target", "outcome", "reasons"], rel)
    outcomes = {"task": {"accept", "reject", "replan", "block"}, "run": {"pass", "replan", "block"}}
    if (
        data["scope"] not in outcomes
        or data["outcome"] not in outcomes[data["scope"]]
        or not isinstance(data["target"], str)
    ):
        raise ProtocolError(f"{rel} has invalid scope, target, or outcome")
    strings(data["reasons"], f"{rel}.reasons", True)


VALIDATORS = {
    "findings": validate_finding,
    "syntheses": validate_synthesis,
    "plans": validate_plan,
    "receipts": validate_receipt,
    "verdicts": validate_verdict,
}


def artifact(root, run, rel, registered=True):
    path = safe_path(root, rel)
    group = PurePosixPath(rel).parts[0]
    if group not in VALIDATORS or not path.is_file():
        raise ProtocolError(f"missing or unsupported artifact: {rel}")
    if registered and run.get("artifacts", {}).get(rel) != digest(path):
        raise ProtocolError(f"artifact is unregistered or changed: {rel}")
    data = load(path)
    VALIDATORS[group](data, rel)
    return data


def active_plan(root, run):
    if run.get("active_plan") is None:
        raise ProtocolError("run has no active plan")
    return artifact(root, run, f"plans/{run['active_plan']:03d}.json")


def validate_execution(execution_id, data):
    require(
        data,
        [
            "id",
            "attempt_id",
            "generation",
            "state",
            "role",
            "adapter",
            "model",
            "reasoning_effort",
            "external_id",
            "observed_state",
            "started_at",
            "last_observed_at",
            "lease_expires_at",
            "unreachable_since",
            "receipt_deadline",
            "termination_reason",
            "progress",
        ],
        execution_id,
    )
    match = PATTERNS["execution"].fullmatch(str(execution_id))
    if (
        not match
        or data["id"] != execution_id
        or data["attempt_id"] != match.group(1)
        or isinstance(data["generation"], bool)
        or data["generation"] != int(match.group(4))
        or data["state"] not in EXECUTION_STATES
        or data["role"] not in {"explorer", "worker", "verifier"}
        or data["observed_state"] not in OBSERVED_STATES | {None}
        or data["termination_reason"] not in TERMINATION_REASONS | {None}
        or not isinstance(data["progress"], dict)
    ):
        raise ProtocolError(f"{execution_id} has invalid identity or lifecycle metadata")
    if any(
        not isinstance(data[key], str) or not data[key]
        for key in ["adapter", "model", "reasoning_effort"]
    ) or (
        data["external_id"] is not None
        and (not isinstance(data["external_id"], str) or not data["external_id"])
    ):
        raise ProtocolError(f"{execution_id} has invalid runtime metadata")
    for key in [
        "started_at",
        "last_observed_at",
        "lease_expires_at",
        "unreachable_since",
        "receipt_deadline",
    ]:
        if data[key] is not None:
            timestamp(data[key], f"{execution_id}.{key}")
    if data["started_at"] is None or data["last_observed_at"] is None:
        raise ProtocolError(f"{execution_id} requires start and observation timestamps")
    if data["state"] in {"starting", "active", "unreachable"} and data["lease_expires_at"] is None:
        raise ProtocolError(f"{execution_id} requires an active lease")
    if data["state"] == "unreachable" and data["unreachable_since"] is None:
        raise ProtocolError(f"{execution_id} unreachable state requires a start time")
    if data["state"] == "awaiting_receipt" and (
        data["receipt_deadline"] is None or data["termination_reason"] != "completed"
    ):
        raise ProtocolError(f"{execution_id} awaiting receipt requires a completed host execution")
    if data["state"] in {"terminated", "lost"} and data["termination_reason"] is None:
        raise ProtocolError(f"{execution_id} terminal state requires a reason")


def validate_run(root, allow_unregistered=None, projection=None):
    root = Path(root)
    run = projection if projection is not None else load(root / "run.json")
    fields = [
        "schema_version",
        "run_id",
        "goal",
        "phase",
        "active_synthesis",
        "active_plan",
        "limits",
        "exploration_followups",
        "tasks",
        "executions",
        "supervision",
        "artifacts",
        "blocked_reasons",
        "run_verdict",
    ]
    require(run, fields, "run.json")
    if (
        run["schema_version"] != 2
        or run["phase"] not in PHASES
        or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", run["run_id"])
    ):
        raise ProtocolError("run.json has invalid version, phase, or run ID")
    if (
        not isinstance(run["goal"], str)
        or not run["goal"].strip()
        or not isinstance(run["tasks"], dict)
        or not isinstance(run["executions"], dict)
        or not isinstance(run["artifacts"], dict)
    ):
        raise ProtocolError("run.json has invalid goal, tasks, or artifacts")
    if not isinstance(run["supervision"], dict) or any(
        not isinstance(run["supervision"].get(key), int)
        or isinstance(run["supervision"].get(key), bool)
        or run["supervision"][key] < 1
        for key in SUPERVISION
    ):
        raise ProtocolError("run.json has invalid supervision policy")
    if not isinstance(run["limits"], dict) or any(
        not isinstance(run["limits"].get(key), int)
        or isinstance(run["limits"].get(key), bool)
        or run["limits"][key] < 0
        for key in LIMITS
    ):
        raise ProtocolError("run.json has invalid limits")
    if (
        not isinstance(run["exploration_followups"], int)
        or isinstance(run["exploration_followups"], bool)
        or run["exploration_followups"] < 0
    ):
        raise ProtocolError("run.json has invalid exploration follow-up counter")
    strings(run["blocked_reasons"], "run.json.blocked_reasons")
    for key in ["active_synthesis", "active_plan"]:
        if run[key] is not None and (
            not isinstance(run[key], int) or isinstance(run[key], bool) or run[key] < 0
        ):
            raise ProtocolError(f"run.json has invalid {key} reference")
    if run["run_verdict"] is not None and (
        not isinstance(run["run_verdict"], str) or not run["run_verdict"]
    ):
        raise ProtocolError("run.json has invalid run verdict reference")
    predecessor = run.get("predecessor")
    if predecessor is not None:
        require(predecessor, ["run_id", "run_verdict", "run_sha256"], "run.json.predecessor")
        if (
            not isinstance(predecessor["run_id"], str)
            or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", predecessor["run_id"])
            or not isinstance(predecessor["run_verdict"], str)
            or not predecessor["run_verdict"]
            or not isinstance(predecessor["run_sha256"], str)
            or re.fullmatch(r"[a-f0-9]{64}", predecessor["run_sha256"]) is None
        ):
            raise ProtocolError("run.json has invalid predecessor evidence")
    questions = load(root / "questions.json")
    if not isinstance(questions, list):
        raise ProtocolError("questions.json must be a list")
    question_ids = set()
    for question in questions:
        require(question, ["id", "question", "scope", "kind", "reasoning_demand"], "question")
        identifier(question["id"], "question", "question.id")
        if (
            question["id"] in question_ids
            or question["kind"] != "explore"
            or question["reasoning_demand"] not in {"low", "medium", "high"}
        ):
            raise ProtocolError("questions.json has duplicate IDs or invalid metadata")
        question_ids.add(question["id"])
        strings(question["scope"], f"{question['id']}.scope")
    disk = {
        path.relative_to(root).as_posix()
        for group in GROUPS
        for path in (root / group).glob("*.json")
    }
    registered = set(run["artifacts"])
    allowed = set(allow_unregistered or ())
    unregistered = disk - registered
    stale = registered - disk
    unexpected = unregistered - allowed
    missing_candidates = allowed - unregistered
    if unexpected or stale or missing_candidates:
        problems = []
        if unexpected:
            problems.append("unregistered artifacts: " + ", ".join(sorted(unexpected)))
        if stale:
            problems.append("registered artifacts missing from disk: " + ", ".join(sorted(stale)))
        if missing_candidates:
            problems.append(
                "registration candidates not found as unregistered artifacts: "
                + ", ".join(sorted(missing_candidates))
            )
        raise ProtocolError("; ".join(problems))
    items = {rel: artifact(root, run, rel) for rel in sorted(registered)}
    findings = {data["id"]: data for rel, data in items.items() if rel.startswith("findings/")}
    if len(findings) != sum(rel.startswith("findings/") for rel in items) or any(
        item["question_id"] not in question_ids for item in findings.values()
    ):
        raise ProtocolError("finding IDs must be unique and reference existing questions")
    syntheses = {
        data["revision"]: data for rel, data in items.items() if rel.startswith("syntheses/")
    }
    if any(fid not in findings for value in syntheses.values() for fid in value["findings"]):
        raise ProtocolError("synthesis references a missing finding")
    plans = {data["revision"]: data for rel, data in items.items() if rel.startswith("plans/")}
    if sorted(plans) != list(range(len(plans))) or len(plans) > run["limits"]["max_plan_revisions"]:
        raise ProtocolError("plan revisions must be consecutive and within the limit")
    if run["active_synthesis"] is not None and run["active_synthesis"] not in syntheses:
        raise ProtocolError("active synthesis references a missing revision")
    if run["active_plan"] is not None and run["active_plan"] not in plans:
        raise ProtocolError("active plan references a missing revision")
    contracts = {}
    specs = {}
    for revision, plan in plans.items():
        if plan["based_on_synthesis"] not in syntheses:
            raise ProtocolError(f"plan {revision:03d} references a missing synthesis")
        invariants = {item["id"] for item in syntheses[plan["based_on_synthesis"]]["invariants"]}
        for task in plan["tasks"]:
            if any(item not in findings for item in task["basis"]) or any(
                item not in invariants for item in task["preserves"]
            ):
                raise ProtocolError(f"{task['id']} references missing evidence")
            current = contract_hash(task)
            if contracts.setdefault(task["id"], current) != current:
                raise ProtocolError(f"{task['id']} changed contract across revisions")
            specs.setdefault(task["id"], task)
    for execution_id, execution in run["executions"].items():
        validate_execution(execution_id, execution)
    attempts = set()
    referenced_executions = set()
    for task_id, state in run["tasks"].items():
        identifier(task_id, "task", "run task ID")
        require(
            state,
            [
                "plan_revision",
                "state",
                "contract_hash",
                "attempts",
                "current_attempt",
                "executions",
                "current_execution",
                "receipts",
                "verdicts",
            ],
            task_id,
        )
        if (
            not isinstance(state["plan_revision"], int)
            or isinstance(state["plan_revision"], bool)
            or state["plan_revision"] not in plans
            or state["state"] not in STATES
            or state["contract_hash"] != contracts.get(task_id)
            or not isinstance(state["attempts"], list)
            or len(state["attempts"]) > run["limits"]["max_task_attempts"]
            or not isinstance(state["executions"], list)
            or not isinstance(state["receipts"], list)
            or not isinstance(state["verdicts"], list)
        ):
            raise ProtocolError(f"{task_id} has invalid state, contract, or attempt count")
        for number, attempt_id in enumerate(state["attempts"], 1):
            if attempt_id != f"{task_id}-A{number:02d}" or attempt_id in attempts:
                raise ProtocolError(f"{task_id} has invalid or duplicate attempts")
            attempts.add(attempt_id)
        for execution_id in state["executions"]:
            execution = run["executions"].get(execution_id)
            if (
                execution is None
                or execution_id in referenced_executions
                or execution["attempt_id"] not in state["attempts"]
                or execution["role"]
                != {"explore": "explorer", "implement": "worker", "verify": "verifier"}[
                    specs[task_id]["kind"]
                ]
            ):
                raise ProtocolError(f"{task_id} references an invalid execution")
            referenced_executions.add(execution_id)
        if (
            state["state"] in {"running", "submitted"}
            and state["current_attempt"] not in state["attempts"]
        ):
            raise ProtocolError(f"{task_id} requires a current attempt")
        if state["state"] in {"running", "submitted"} and (
            state["current_execution"] not in state["executions"]
            or run["executions"][state["current_execution"]]["attempt_id"]
            != state["current_attempt"]
        ):
            raise ProtocolError(f"{task_id} requires a current execution")
        if (
            state["state"] not in {"running", "submitted"}
            and state["current_execution"] is not None
        ):
            raise ProtocolError(f"{task_id} has a stale current execution")
        receipts = [items.get(path) for path in state["receipts"]]
        verdicts = [items.get(path) for path in state["verdicts"]]
        if any(
            not item
            or item.get("task_id") != task_id
            or item.get("attempt_id") not in state["attempts"]
            for item in receipts
        ):
            raise ProtocolError(f"{task_id} references an invalid receipt")
        if any(
            not item or item.get("scope") != "task" or item.get("target") not in state["attempts"]
            for item in verdicts
        ):
            raise ProtocolError(f"{task_id} references an invalid verdict")
        if state["state"] == "submitted" and (
            not receipts or receipts[-1]["attempt_id"] != state["current_attempt"]
        ):
            raise ProtocolError(f"{task_id} submitted state requires its current receipt")
        if state["state"] == "submitted" and (
            receipts[-1]["execution"]["id"] != state["current_execution"]
        ):
            raise ProtocolError(f"{task_id} submitted receipt targets a stale execution")
        if state["state"] == "accepted" and (not verdicts or verdicts[-1]["outcome"] != "accept"):
            raise ProtocolError(f"{task_id} accepted state requires an accept verdict")
    if referenced_executions != set(run["executions"]):
        raise ProtocolError("every execution must belong to exactly one task")
    if run["phase"] in {"executing", "verifying", "completed"}:
        plan = active_plan(root, run)
        if any(spec["id"] not in run["tasks"] for spec in plan["tasks"]):
            raise ProtocolError("active plan is missing a task projection")
        if run["phase"] in {"verifying", "completed"} and any(
            run["tasks"][spec["id"]]["state"] not in {"accepted", "skipped"}
            for spec in plan["tasks"]
        ):
            raise ProtocolError("verification requires all active tasks to be terminal")
    if run["phase"] == "completed":
        verdict = artifact(root, run, run["run_verdict"])
        if (
            verdict["scope"] != "run"
            or verdict["outcome"] != "pass"
            or verdict["target"] != f"plan-{run['active_plan']:03d}"
        ):
            raise ProtocolError("completed requires a passing run verdict")
    return run


def init_run(root, goal, run_id=None):
    root = Path(root)
    slug = run_id or root.name
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug):
        raise ProtocolError("run ID must be kebab-case")
    if root.exists() and any(root.iterdir()):
        raise ProtocolError("run directory must not contain files")
    root.mkdir(parents=True, exist_ok=True)
    for group in GROUPS:
        (root / group).mkdir()
    run = {
        "schema_version": 2,
        "run_id": slug,
        "goal": goal,
        "phase": "exploring",
        "active_synthesis": None,
        "active_plan": None,
        "limits": dict(LIMITS),
        "exploration_followups": 0,
        "tasks": {},
        "executions": {},
        "supervision": dict(SUPERVISION),
        "artifacts": {},
        "blocked_reasons": [],
        "run_verdict": None,
    }
    write(root / "run.json", run)
    write(root / "questions.json", [])
    return run


def init_next_run(root, predecessor_root, goal, run_id=None):
    predecessor_root = Path(predecessor_root)
    predecessor = validate_run(predecessor_root)
    if predecessor["phase"] != "completed":
        raise ProtocolError(
            f"predecessor run must be completed; {predecessor['run_id']} is {predecessor['phase']}"
        )
    run = init_run(root, goal, run_id)
    run["predecessor"] = {
        "run_id": predecessor["run_id"],
        "run_verdict": predecessor["run_verdict"],
        "run_sha256": digest(predecessor_root / "run.json"),
    }
    write(Path(root) / "run.json", run)
    return run


def run_summary(root, run, include_next=True):
    active_specs = []
    if run["active_plan"] is not None:
        active_specs = active_plan(root, run)["tasks"]
    active_ids = {spec["id"] for spec in active_specs}
    state_counts = {
        state: sum(
            task_id in active_ids and task["state"] == state
            for task_id, task in run["tasks"].items()
        )
        for state in sorted(STATES)
    }
    summary = {
        "run_id": run["run_id"],
        "valid": True,
        "phase": run["phase"],
        "active_synthesis": run["active_synthesis"],
        "active_plan": run["active_plan"],
        "active_tasks": len(active_specs),
        "task_states": state_counts,
        "executions": len(run["executions"]),
        "registered_artifacts": len(run["artifacts"]),
        "blocked_reasons": run["blocked_reasons"],
        "run_verdict": run["run_verdict"],
    }
    if include_next:
        summary["next_actions"] = next_actions(root, run)
    return summary


def next_actions(root, run):
    phase = run["phase"]
    if phase == "exploring":
        return [
            {
                "action": "explore",
                "detail": "Answer questions.json with registered findings, then enter synthesizing.",
                "command": "transition <run-dir> set-phase --phase synthesizing --compact",
            }
        ]
    if phase == "synthesizing":
        syntheses = sorted(
            (artifact(root, run, rel)["revision"], rel)
            for rel in run["artifacts"]
            if rel.startswith("syntheses/")
        )
        candidates = [
            (revision, rel)
            for revision, rel in syntheses
            if run["active_synthesis"] is None or revision > run["active_synthesis"]
        ]
        if not candidates:
            return [
                {
                    "action": "create_synthesis",
                    "detail": "Create and register the next synthesis revision.",
                }
            ]
        _, latest = candidates[0]
        disposition = artifact(root, run, latest)["disposition"]
        target = {
            "ready": "planning",
            "needs_more_evidence": "exploring",
            "needs_authority": "blocked",
        }[disposition]
        return [
            {
                "action": "set_phase",
                "detail": f"The latest synthesis permits phase={target}.",
                "command": (
                    f"transition <run-dir> set-phase --phase {target} --artifact {latest} --compact"
                ),
            }
        ]
    if phase == "planning":
        candidates = []
        for rel in sorted(run["artifacts"]):
            if not rel.startswith("plans/"):
                continue
            plan = artifact(root, run, rel)
            if plan["based_on_synthesis"] == run["active_synthesis"] and (
                run["active_plan"] is None or plan["revision"] > run["active_plan"]
            ):
                candidates.append(rel)
        if candidates:
            return [
                {
                    "action": "activate_plan",
                    "detail": "Activate the registered plan based on the active synthesis.",
                    "command": (
                        f"transition <run-dir> activate-plan --artifact {candidates[0]} --compact"
                    ),
                }
            ]
        return [
            {
                "action": "create_plan",
                "detail": "Create and register a newer plan based on the active synthesis.",
            }
        ]
    if phase == "executing":
        active = next(
            (
                (task_id, task, run["executions"][task["current_execution"]])
                for task_id, task in run["tasks"].items()
                if task["state"] == "running"
            ),
            None,
        )
        if active:
            task_id, task, execution = active
            action = (
                "collect_receipt"
                if execution["state"] == "awaiting_receipt"
                else "reconcile_workspace"
                if execution["state"] in {"terminated", "lost"}
                else "observe_execution"
            )
            return [
                {
                    "action": action,
                    "task_id": task_id,
                    "attempt_id": task["current_attempt"],
                    "execution_id": execution["id"],
                    "execution_state": execution["state"],
                    "detail": "Use an explicit UTC --at timestamp for host observations.",
                }
            ]
        submitted = next(
            (
                (task_id, task)
                for task_id, task in run["tasks"].items()
                if task["state"] == "submitted"
            ),
            None,
        )
        if submitted:
            return [
                {
                    "action": "verify_task",
                    "task_id": submitted[0],
                    "attempt_id": submitted[1]["current_attempt"],
                    "detail": "Register and apply an independent task verdict.",
                }
            ]
        spec = runnable(root, run)
        if spec:
            return [
                {
                    "action": "start_attempt",
                    "task_id": spec["id"],
                    "detail": "Start the next runnable task with explicit host/model/effort/time.",
                }
            ]
        return [
            {
                "action": "set_phase",
                "detail": "All active tasks are terminal; enter run verification.",
                "command": "transition <run-dir> set-phase --phase verifying --compact",
            }
        ]
    if phase == "verifying":
        target = f"plan-{run['active_plan']:03d}"
        verdicts = []
        for rel in sorted(run["artifacts"]):
            if not rel.startswith("verdicts/"):
                continue
            verdict = artifact(root, run, rel)
            if verdict["scope"] == "run" and verdict["target"] == target:
                verdicts.append((rel, verdict))
        if verdicts:
            rel, verdict = verdicts[-1]
            target_phase = {"pass": "completed", "replan": "planning", "block": "blocked"}[
                verdict["outcome"]
            ]
            return [
                {
                    "action": "set_phase",
                    "detail": f"The registered run verdict permits phase={target_phase}.",
                    "command": (
                        f"transition <run-dir> set-phase --phase {target_phase} "
                        f"--artifact {rel} --compact"
                    ),
                }
            ]
        return [
            {
                "action": "verify_run",
                "plan_revision": run["active_plan"],
                "detail": "Register a run verdict, then use it to complete, replan, or block.",
            }
        ]
    if phase == "completed":
        return [
            {
                "action": "start_next_run",
                "detail": "New requirements belong in a successor run; do not reopen this run.",
                "command": "init-next <new-run-dir> --after <this-run-dir> --goal <goal>",
            }
        ]
    return [{"action": "wait", "phase": phase, "detail": "Human or protocol recovery is required."}]


def runnable(root, run):
    if run["phase"] != "executing":
        return None
    plan = active_plan(root, run)
    if any(run["tasks"][spec["id"]]["state"] in {"running", "submitted"} for spec in plan["tasks"]):
        return None
    for spec in plan["tasks"]:
        if run["tasks"][spec["id"]]["state"] == "pending" and all(
            run["tasks"][dep]["state"] == "accepted" for dep in spec["depends_on"]
        ):
            return spec
    return None


def block(run, reason):
    run["phase"] = "blocked"
    run["blocked_reasons"].append(reason)


def start_attempt(run, state, spec, adapter, model, reasoning_effort, at):
    if any(not isinstance(value, str) or not value for value in [adapter, model, reasoning_effort]):
        raise ProtocolError("starting an attempt requires adapter, model, and reasoning effort")
    timestamp(at, "attempt start time")
    attempt_id = f"{spec['id']}-A{len(state['attempts']) + 1:02d}"
    generation = 1
    execution_id = f"{attempt_id}-E{generation:02d}"
    role = {"explore": "explorer", "implement": "worker", "verify": "verifier"}[spec["kind"]]
    execution = {
        "id": execution_id,
        "attempt_id": attempt_id,
        "generation": generation,
        "state": "starting",
        "role": role,
        "adapter": adapter,
        "model": model,
        "reasoning_effort": reasoning_effort,
        "external_id": None,
        "observed_state": None,
        "started_at": at,
        "last_observed_at": at,
        "lease_expires_at": deadline(at, run["supervision"]["lease_seconds"]),
        "unreachable_since": None,
        "receipt_deadline": None,
        "termination_reason": None,
        "progress": {},
    }
    run["executions"][execution_id] = execution
    state["attempts"].append(attempt_id)
    state["executions"].append(execution_id)
    state.update(state="running", current_attempt=attempt_id, current_execution=execution_id)
    return execution


def observe_execution(run, args):
    execution = run["executions"].get(args.execution)
    if execution is None:
        raise ProtocolError("observation targets an unknown execution")
    task_id = PATTERNS["attempt"].fullmatch(execution["attempt_id"]).group(1)
    task = run["tasks"][task_id]
    if task["state"] != "running" or task["current_execution"] != execution["id"]:
        raise ProtocolError("observation targets a stale execution")
    if execution["state"] in {"terminated", "lost"}:
        raise ProtocolError("terminal execution cannot accept observations")
    observed_at = timestamp(args.at, "observation time")
    if observed_at < timestamp(execution["last_observed_at"], "last observation time"):
        raise ProtocolError("observations must be monotonic")
    if args.external_id:
        if execution["external_id"] not in {None, args.external_id}:
            raise ProtocolError("execution cannot change its external host ID")
        execution["external_id"] = args.external_id
    if args.progress is not None:
        progress = args.progress
        if isinstance(progress, str):
            try:
                progress = json.loads(progress)
            except json.JSONDecodeError as exc:
                raise ProtocolError("progress must be a JSON object") from exc
        if not isinstance(progress, dict):
            raise ProtocolError("progress must be a JSON object")
        execution["progress"] = progress
    execution["observed_state"] = args.observed_state
    execution["last_observed_at"] = args.at
    if args.observed_state == "active":
        execution.update(
            state="active",
            lease_expires_at=deadline(args.at, run["supervision"]["lease_seconds"]),
            unreachable_since=None,
            receipt_deadline=None,
            termination_reason=None,
        )
    elif args.observed_state == "unknown":
        execution["state"] = "unreachable"
        if execution["unreachable_since"] is None:
            execution["unreachable_since"] = args.at
    elif args.observed_state == "completed":
        execution.update(
            state="awaiting_receipt",
            receipt_deadline=deadline(args.at, run["supervision"]["receipt_grace_seconds"]),
            termination_reason="completed",
        )
    else:
        reason = {
            "cancelled": "cancelled",
            "interrupted": "interrupted",
            "error": "host_error",
        }[args.observed_state]
        execution.update(state="terminated", termination_reason=reason)
    return execution


def check_receipt_target(run, receipt):
    state = run["tasks"].get(receipt["task_id"])
    if (
        not state
        or state["state"] != "running"
        or state["current_attempt"] != receipt["attempt_id"]
    ):
        raise ProtocolError("receipt targets a stale attempt")
    execution = run["executions"][state["current_execution"]]
    submitted = receipt["execution"]
    if submitted["id"] != execution["id"] or any(
        submitted[key] != execution[key]
        for key in ["generation", "role", "adapter", "model", "reasoning_effort"]
    ):
        raise ProtocolError("receipt targets a stale execution")
    if execution["state"] in {"terminated", "lost"} and (
        receipt["outcome"] != "error" or not receipt["concerns"]
    ):
        raise ProtocolError("terminal execution requires a supervised error receipt")
    return state, execution


def supervision_directive(root, run, at):
    now = timestamp(at, "supervision time")
    changed = False
    active = None
    for task_id, task in run["tasks"].items():
        if task["state"] == "running":
            active = (task_id, task, run["executions"][task["current_execution"]])
            break
    if active:
        task_id, task, execution = active
        if execution["state"] in {"starting", "active"} and now > timestamp(
            execution["lease_expires_at"], "lease expiry"
        ):
            execution.update(
                state="unreachable",
                observed_state="unknown",
                unreachable_since=execution["lease_expires_at"],
            )
            changed = True
        if execution["state"] == "unreachable":
            lost_at = timestamp(execution["unreachable_since"], "unreachable time") + timedelta(
                seconds=run["supervision"]["unreachable_grace_seconds"]
            )
            if now > lost_at:
                execution.update(state="lost", termination_reason="lease_expired")
                changed = True
        if execution["state"] == "awaiting_receipt" and now > timestamp(
            execution["receipt_deadline"], "receipt deadline"
        ):
            execution.update(state="lost", termination_reason="receipt_timeout")
            changed = True
        action = {
            "starting": "poll",
            "active": "poll",
            "unreachable": "poll",
            "awaiting_receipt": "collect_receipt",
            "terminated": "reconcile_workspace",
            "lost": "reconcile_workspace",
        }[execution["state"]]
        directive = {
            "action": action,
            "task_id": task_id,
            "attempt_id": task["current_attempt"],
            "execution_id": execution["id"],
            "execution_state": execution["state"],
        }
        if execution["state"] in {"starting", "active"}:
            directive["deadline"] = execution["lease_expires_at"]
        elif execution["state"] == "unreachable":
            directive["deadline"] = format_timestamp(
                timestamp(execution["unreachable_since"], "unreachable time")
                + timedelta(seconds=run["supervision"]["unreachable_grace_seconds"])
            )
        elif execution["state"] == "awaiting_receipt":
            directive["deadline"] = execution["receipt_deadline"]
        if execution["termination_reason"]:
            directive["reason"] = execution["termination_reason"]
    elif run["phase"] == "executing":
        submitted = next(
            (
                (task_id, task)
                for task_id, task in run["tasks"].items()
                if task["state"] == "submitted"
            ),
            None,
        )
        if submitted:
            directive = {
                "action": "verify_task",
                "task_id": submitted[0],
                "attempt_id": submitted[1]["current_attempt"],
            }
        else:
            spec = runnable(root, run)
            directive = {"action": "dispatch", "task": spec} if spec else {"action": "wait"}
    elif run["phase"] == "verifying":
        directive = {"action": "verify_run", "plan_revision": run["active_plan"]}
    else:
        directive = {"action": "wait", "phase": run["phase"]}
    if changed:
        write(Path(root) / "run.json", run)
    return directive


def validate_registration_path(root, relative):
    if relative == "questions.json":
        raise ProtocolError("questions.json is mutable and must not be registered")
    if relative == "run.json":
        raise ProtocolError("run.json is the mutable projection and must not be registered")
    path = safe_path(Path(root), relative)
    group = PurePosixPath(relative).parts[0]
    if group not in VALIDATORS:
        raise ProtocolError(
            f"unsupported artifact group for {relative}; expected one of: "
            + ", ".join(sorted(VALIDATORS))
        )
    return path


def require_event_arguments(args):
    required = {
        "register-artifact": ["artifact"],
        "register-artifacts": ["artifacts"],
        "set-phase": ["phase"],
        "activate-plan": ["artifact"],
        "start-attempt": ["task", "adapter", "model", "reasoning_effort", "at"],
        "observe-execution": ["execution", "observed_state", "at"],
        "submit": ["task", "artifact"],
        "apply-verdict": ["task", "artifact"],
        "skip-task": ["task", "reason"],
        "invalidate-task": ["task", "reason"],
    }
    missing = []
    for name in required[args.event]:
        value = getattr(args, name, None)
        if value is None or value == "" or value == []:
            missing.append("--" + name.replace("_", "-"))
    if missing:
        raise ProtocolError(f"{args.event} requires: {', '.join(missing)}")
    if getattr(args, "admit", False) and args.event not in {"submit", "apply-verdict"}:
        raise ProtocolError("--admit is supported only for submit and apply-verdict")


def register_artifacts(root, relatives):
    root = Path(root)
    if len(set(relatives)) != len(relatives):
        raise ProtocolError("register-artifacts received duplicate paths")
    for relative in relatives:
        validate_registration_path(root, relative)
    run = validate_run(root, set(relatives))
    proposed = json.loads(json.dumps(run))
    for relative in relatives:
        data = artifact(root, run, relative, False)
        if relative.startswith("receipts/"):
            check_receipt_target(run, data)
        proposed["artifacts"][relative] = digest(safe_path(root, relative))
    plans = [relative for relative in proposed["artifacts"] if relative.startswith("plans/")]
    if len(plans) > proposed["limits"]["max_plan_revisions"]:
        raise ProtocolError(
            f"register-artifacts would exceed the plan revision limit "
            f"({proposed['limits']['max_plan_revisions']})"
        )
    validate_run(root, projection=proposed)
    write(root / "run.json", proposed)
    return {
        "registered": sorted(relatives),
        "registered_artifacts": len(proposed["artifacts"]),
        "phase": proposed["phase"],
    }


def transition_summary(root, event, result, run):
    summary = {
        "event": event,
        "phase": run["phase"],
        "active_plan": run["active_plan"],
        "registered_artifacts": len(run["artifacts"]),
    }
    if event == "start-attempt":
        summary.update(
            task_id=PATTERNS["attempt"].fullmatch(result["attempt_id"]).group(1),
            attempt_id=result["attempt_id"],
            execution_id=result["id"],
            action="poll",
        )
    elif event == "register-artifacts":
        summary["registered"] = result["registered"]
    elif event == "apply-verdict":
        summary.update(outcome=result["outcome"])
    elif event in {"skip-task", "invalidate-task"}:
        summary.update(task_state=result["state"])
    summary["next_actions"] = next_actions(Path(root), run)
    return summary


def transition(root, args):
    root = Path(root)
    event = args.event
    require_event_arguments(args)
    if event == "register-artifact":
        validate_registration_path(root, args.artifact)
    elif event == "register-artifacts":
        return register_artifacts(root, args.artifacts)
    admit = getattr(args, "admit", False)
    if admit:
        validate_registration_path(root, args.artifact)
    if event == "set-phase" and args.phase == "failed":
        run = load(root / "run.json")
        require(run, ["phase", "blocked_reasons"], "run.json")
        if run["phase"] in {"completed", "blocked", "failed"}:
            raise ProtocolError(f"illegal phase transition: {run['phase']} -> failed")
        if not args.reason:
            raise ProtocolError("failed requires a protocol-failure reason")
        strings(run["blocked_reasons"], "run.json.blocked_reasons")
        run["phase"] = "failed"
        run["blocked_reasons"].append(args.reason)
        write(root / "run.json", run)
        return run
    allow_unregistered = {args.artifact} if event == "register-artifact" or admit else None
    run = validate_run(root, allow_unregistered)
    if event == "set-phase":
        needs_artifact = (
            run["phase"] == "synthesizing" and args.phase in {"planning", "exploring", "blocked"}
        ) or (run["phase"] == "verifying" and args.phase in {"completed", "planning", "blocked"})
        if needs_artifact and not args.artifact:
            raise ProtocolError(f"set-phase {run['phase']} -> {args.phase} requires: --artifact")
    if admit:
        candidate = artifact(root, run, args.artifact, False)
        if event == "submit":
            check_receipt_target(run, candidate)
        run["artifacts"][args.artifact] = digest(safe_path(root, args.artifact))
    if event == "register-artifact":
        result = artifact(root, run, args.artifact, False)
        if args.artifact.startswith("receipts/"):
            try:
                check_receipt_target(run, result)
            except ProtocolError:
                safe_path(root, args.artifact).unlink()
                raise
        current = digest(safe_path(root, args.artifact))
        if run["artifacts"].get(args.artifact, current) != current:
            raise ProtocolError("immutable artifact was changed")
        if args.artifact.startswith("plans/"):
            synthesis = f"syntheses/{result['based_on_synthesis']:03d}.json"
            try:
                artifact(root, run, synthesis)
            except ProtocolError:
                safe_path(root, args.artifact).unlink()
                raise ProtocolError("plan references a missing synthesis") from None
        plans = [path for path in run["artifacts"] if path.startswith("plans/")]
        if (
            args.artifact.startswith("plans/")
            and args.artifact not in plans
            and len(plans) >= run["limits"]["max_plan_revisions"]
        ):
            safe_path(root, args.artifact).unlink()
            block(run, "plan revision limit exceeded")
            result = run
        else:
            run["artifacts"][args.artifact] = current
    elif event == "set-phase":
        current, target = run["phase"], args.phase
        if (current, target) == ("exploring", "synthesizing"):
            run["phase"] = target
        elif current == "synthesizing" and target in {"planning", "exploring", "blocked"}:
            result = artifact(root, run, args.artifact)
            expected = {
                "planning": "ready",
                "exploring": "needs_more_evidence",
                "blocked": "needs_authority",
            }[target]
            if result["disposition"] != expected:
                raise ProtocolError("synthesis does not permit target phase")
            run["active_synthesis"] = result["revision"]
            if (
                target == "exploring"
                and run["exploration_followups"] >= run["limits"]["max_exploration_followups"]
            ):
                block(run, "exploration follow-up limit exceeded")
            elif target == "exploring":
                run["exploration_followups"] += 1
                run["phase"] = target
            elif target == "blocked":
                block(run, "synthesis requires external authority")
            else:
                run["phase"] = target
        elif (current, target) == ("planning", "blocked"):
            if not args.reason:
                raise ProtocolError("blocking requires a reason")
            block(run, args.reason)
        elif (current, target) == ("executing", "verifying"):
            if any(
                run["tasks"][item["id"]]["state"] not in {"accepted", "skipped"}
                for item in active_plan(root, run)["tasks"]
            ):
                raise ProtocolError("all active tasks must be accepted or skipped")
            run["phase"] = target
        elif current == "verifying" and target in {"completed", "planning", "blocked"}:
            result = artifact(root, run, args.artifact)
            expected = {"completed": "pass", "planning": "replan", "blocked": "block"}[target]
            if (
                result["scope"] != "run"
                or result["outcome"] != expected
                or result["target"] != f"plan-{run['active_plan']:03d}"
            ):
                raise ProtocolError("run verdict does not permit target phase")
            run["run_verdict"] = args.artifact
            run["phase"] = target
        else:
            raise ProtocolError(f"illegal phase transition: {current} -> {target}")
        result = run
    elif event == "activate-plan":
        if run["phase"] != "planning":
            hint = "transition <run-dir> set-phase --phase planning --artifact <synthesis>"
            raise ProtocolError(
                f"cannot activate a plan while phase={run['phase']}; "
                f"enter planning first with: {hint}"
            )
        result = artifact(root, run, args.artifact)
        if result["based_on_synthesis"] != run["active_synthesis"]:
            raise ProtocolError("plan is not based on active synthesis")
        if run["active_plan"] is not None and result["revision"] <= run["active_plan"]:
            raise ProtocolError("replan requires a strictly newer plan revision")
        new_ids = {task["id"] for task in result["tasks"]}
        if run["active_plan"] is not None:
            for old in active_plan(root, run)["tasks"]:
                if old["id"] not in new_ids and run["tasks"][old["id"]]["state"] != "skipped":
                    run["tasks"][old["id"]].update(
                        state="invalidated", current_attempt=None, current_execution=None
                    )
        for spec in result["tasks"]:
            state = run["tasks"].get(spec["id"])
            if state and (
                state["contract_hash"] != contract_hash(spec) or state["state"] == "invalidated"
            ):
                raise ProtocolError(f"{spec['id']} cannot reuse a changed or invalidated contract")
            if not state:
                state = {
                    "plan_revision": result["revision"],
                    "state": "pending",
                    "contract_hash": contract_hash(spec),
                    "attempts": [],
                    "current_attempt": None,
                    "executions": [],
                    "current_execution": None,
                    "receipts": [],
                    "verdicts": [],
                }
                run["tasks"][spec["id"]] = state
            state["plan_revision"] = result["revision"]
        run["active_plan"] = result["revision"]
        run["phase"] = "executing"
    elif event == "start-attempt":
        spec = runnable(root, run)
        if not spec or spec["id"] != args.task:
            raise ProtocolError("task is not next runnable")
        state = run["tasks"][args.task]
        if len(state["attempts"]) >= run["limits"]["max_task_attempts"]:
            block(run, f"{args.task} attempt limit exceeded")
            result = run
        else:
            result = start_attempt(
                run,
                state,
                spec,
                args.adapter,
                args.model,
                args.reasoning_effort,
                args.at,
            )
    elif event == "observe-execution":
        result = observe_execution(run, args)
    elif event == "submit":
        state = run["tasks"].get(args.task)
        if not state or state["state"] != "running":
            raise ProtocolError("only a running task can submit")
        result = artifact(root, run, args.artifact)
        if result["task_id"] != args.task or result["attempt_id"] != state["current_attempt"]:
            raise ProtocolError("receipt does not target current attempt")
        spec = next(item for item in active_plan(root, run)["tasks"] if item["id"] == args.task)
        expected_role = {"explore": "explorer", "implement": "worker", "verify": "verifier"}[
            spec["kind"]
        ]
        if result["execution"]["role"] != expected_role:
            raise ProtocolError("receipt execution role does not match task kind")
        _, execution = check_receipt_target(run, result)
        if spec["kind"] != "implement" and result["changed_files"]:
            raise ProtocolError("read-only task receipt cannot contain changed files")
        scopes = spec["write_scope"]

        def covered(path):
            pure = PurePosixPath(path)
            return (
                not pure.is_absolute()
                and ".." not in pure.parts
                and any(
                    path == scope
                    or scope.endswith("/**")
                    and (path == scope[:-3] or path.startswith(scope[:-2]))
                    for scope in scopes
                )
            )

        if any(not isinstance(path, str) or not covered(path) for path in result["changed_files"]):
            raise ProtocolError("changed file is outside write scope")
        commands = {check["command"] for check in result["checks"]}
        if result["outcome"] == "completed" and any(
            check not in commands for check in spec["checks"]
        ):
            raise ProtocolError("receipt omits a required check")
        state["receipts"].append(args.artifact)
        state["state"] = "submitted"
        if execution["state"] not in {"terminated", "lost"}:
            execution.update(state="terminated", termination_reason="completed")
    elif event == "apply-verdict":
        state = run["tasks"].get(args.task)
        if not state or state["state"] != "submitted":
            raise ProtocolError("verdict requires a submitted task")
        verdict = artifact(root, run, args.artifact)
        if verdict["scope"] != "task" or verdict["target"] != state["current_attempt"]:
            raise ProtocolError("verdict does not target current attempt")
        state["verdicts"].append(args.artifact)
        outcome = verdict["outcome"]
        if outcome == "accept":
            spec = next(item for item in active_plan(root, run)["tasks"] if item["id"] == args.task)
            if any(run["tasks"][dep]["state"] != "accepted" for dep in spec["depends_on"]):
                raise ProtocolError("dependency is no longer accepted")
            receipt = artifact(root, run, state["receipts"][-1])
            required_checks = [
                check for check in receipt["checks"] if check["command"] in spec["checks"]
            ]
            if receipt["outcome"] != "completed":
                raise ProtocolError("only a completed receipt can be accepted")
            if any(check["exit_code"] != 0 for check in required_checks):
                raise ProtocolError("required receipt checks must succeed")
            state.update(state="accepted", current_attempt=None, current_execution=None)
        elif outcome == "reject" and len(state["attempts"]) < run["limits"]["max_task_attempts"]:
            if not args.at:
                raise ProtocolError(
                    "applying a reject verdict requires --at <UTC timestamp ending in Z> "
                    "to start the retry attempt"
                )
            previous = run["executions"][state["current_execution"]]
            spec = next(item for item in active_plan(root, run)["tasks"] if item["id"] == args.task)
            start_attempt(
                run,
                state,
                spec,
                previous["adapter"],
                previous["model"],
                previous["reasoning_effort"],
                args.at,
            )
        elif outcome == "reject":
            state.update(state="pending", current_attempt=None, current_execution=None)
            block(run, f"{args.task} attempt limit exhausted after rejection")
        elif outcome == "replan":
            state.update(state="invalidated", current_attempt=None, current_execution=None)
            run["phase"] = "planning"
        else:
            state.update(state="pending", current_attempt=None, current_execution=None)
            block(run, f"{args.task} verifier blocked the run")
        result = {"outcome": outcome, "task": state, "phase": run["phase"]}
    elif event in {"skip-task", "invalidate-task"}:
        state = run["tasks"].get(args.task)
        allowed = {"skip-task": "pending", "invalidate-task": "accepted"}[event]
        if not state or state["state"] != allowed or not args.reason:
            raise ProtocolError(f"{event} requires an eligible task and reason")
        if event == "skip-task" and any(
            spec["id"] != args.task
            and args.task in spec["depends_on"]
            and run["tasks"][spec["id"]]["state"] == "pending"
            for spec in active_plan(root, run)["tasks"]
        ):
            raise ProtocolError("cannot skip a task with an active pending dependent")
        state["state"] = "skipped" if event == "skip-task" else "invalidated"
        state["current_execution"] = None
        if event == "invalidate-task":
            run["phase"] = "planning"
        result = state
    else:
        raise ProtocolError(f"unknown event: {event}")
    write(root / "run.json", run)
    return result


def make_parser():
    top = argparse.ArgumentParser(description=__doc__)
    commands = top.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init")
    init.add_argument("run_dir", type=Path)
    init.add_argument("--goal", required=True)
    init.add_argument("--run-id")
    init_next = commands.add_parser("init-next")
    init_next.add_argument("run_dir", type=Path)
    init_next.add_argument("--after", required=True, type=Path)
    init_next.add_argument("--goal", required=True)
    init_next.add_argument("--run-id")
    validate = commands.add_parser("validate")
    validate.add_argument("run_dir", type=Path)
    validate.add_argument("--full", action="store_true")
    for name in ["runnable", "status", "next"]:
        command = commands.add_parser(name)
        command.add_argument("run_dir", type=Path)
    supervise = commands.add_parser("supervise")
    supervise.add_argument("run_dir", type=Path)
    supervise.add_argument("--at", required=True)
    change = commands.add_parser("transition")
    change.add_argument("run_dir", type=Path)
    change.add_argument(
        "event",
        choices=[
            "register-artifact",
            "register-artifacts",
            "set-phase",
            "activate-plan",
            "start-attempt",
            "observe-execution",
            "submit",
            "apply-verdict",
            "skip-task",
            "invalidate-task",
        ],
    )
    change.add_argument("--artifact")
    change.add_argument("--artifacts", nargs="+")
    change.add_argument("--phase", choices=sorted(PHASES))
    change.add_argument("--task")
    change.add_argument("--reason")
    change.add_argument("--execution")
    change.add_argument("--adapter")
    change.add_argument("--model")
    change.add_argument("--reasoning-effort")
    change.add_argument("--at")
    change.add_argument("--observed-state", choices=sorted(OBSERVED_STATES))
    change.add_argument("--external-id")
    change.add_argument("--progress")
    change.add_argument("--admit", action="store_true")
    change.add_argument("--compact", action="store_true")
    return top


def main(argv=None):
    args = make_parser().parse_args(argv)
    try:
        if args.command == "init":
            result = init_run(args.run_dir, args.goal, args.run_id)
        elif args.command == "init-next":
            result = init_next_run(args.run_dir, args.after, args.goal, args.run_id)
        elif args.command == "validate":
            run = validate_run(args.run_dir)
            result = run if args.full else run_summary(args.run_dir, run, include_next=False)
        elif args.command == "runnable":
            result = runnable(args.run_dir, validate_run(args.run_dir))
        elif args.command == "status":
            result = run_summary(args.run_dir, validate_run(args.run_dir))
        elif args.command == "next":
            result = next_actions(args.run_dir, validate_run(args.run_dir))
        elif args.command == "supervise":
            result = supervision_directive(args.run_dir, validate_run(args.run_dir), args.at)
            validate_run(args.run_dir)
        else:
            result = transition(args.run_dir, args)
            if args.compact:
                run = validate_run(args.run_dir)
                result = transition_summary(args.run_dir, args.event, result, run)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except ProtocolError as exc:
        print(f"protocol error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
