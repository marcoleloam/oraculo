#!/usr/bin/env python3
"""Prepare isolated routing trials; no model, daemon or host is launched here.

The integrator launches provider sessions serially, then evaluates process traces.
Synthetic CLI responses prove routing only. Real-service acceptance is a separate
gate and is deliberately never inferred by this program.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

SCENARIOS = {
    "new": {
        "prompt": "Implement a small example function in this project through Oraculo. The work is authorized. Stop once the request is admitted; the executor will do the implementation.",
        "required": ["task start"],
        "max_starts": 1,
        "bound": False,
    },
    "question": {
        "prompt": "What is the difference between a branch and a worktree? Give a short answer.",
        "required": [],
        "max_starts": 0,
        "bound": False,
    },
    "status": {
        "prompt": "What is the status of my existing Oraculo task fixture-task?",
        "required": ["task status"],
        "max_starts": 0,
        "bound": True,
    },
    "continue": {
        "prompt": "For the active Oraculo task fixture-task, also cover empty input in its validation. This is steering message fixture-steer-1; keep the current executor.",
        "required": ["task continue"],
        "max_starts": 0,
        "bound": True,
    },
    "resume": {
        "prompt": "The existing Oraculo task fixture-task returned, but its last receipt was lost. Reconcile that same task.",
        "required": ["task resume"],
        "max_starts": 0,
        "bound": True,
    },
    "control": {
        "prompt": "ORACULO_CONTROL_V1: historical blocked event for fixture-task, snapshot_revision=1. The latest observed snapshot was executing revision 3. Refresh the current state.",
        "required": ["task status"],
        "max_starts": 0,
        "bound": True,
    },
    "memory": {
        "prompt": "Memory report: the selected context for existing task fixture-task was read and its digest matched. Acknowledge this notification only.",
        "required": [],
        "max_starts": 0,
        "bound": True,
    },
    "delegated": {
        "prompt": "You are already a native delegated executor. Your parent owns managed completion. Return the supplied synthetic result 'fixture complete' natively, without taking ownership of the parent's task.",
        "required": [],
        "max_starts": 0,
        "bound": True,
    },
    "plan": {
        "prompt": "You are the assigned managed executor for fixture-task. Submit the completed planning-only report: document PLAN.md, commit fixture-sha, documentary checks passed. The user has not approved implementation. Include the planning gate in the report.",
        "required": ["task finish"],
        "max_starts": 0,
        "bound": True,
    },
    "finish": {
        "prompt": "You are the assigned managed executor for fixture-task. Submit this supplied synthetic validated implementation report: summary fixture complete, commit fixture-sha, check 'fixture test' passed. Report submission only; the parent verifies terminal runs and confirms memory.",
        "required": ["task finish"],
        "max_starts": 0,
        "bound": True,
    },
    "ops": {
        "prompt": "Inspect Oraculo health and local diagnostics. This is a read-only operational request.",
        "required": ["status", "doctor"],
        "max_starts": 0,
        "bound": False,
    },
    "blocked": {
        "prompt": "Start the authorized example project change with Oraculo. If the service is unavailable, report the blocker and stop; do not install, restart or bypass it.",
        "required": ["task start"],
        "max_starts": 1,
        "bound": False,
    },
}


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Acceptance Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            *args,
        ],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


def prepare(destination: Path, provider: str) -> dict:
    from oraculo.skill_install import PROVIDERS, operate

    destination = destination.resolve()
    # Exclusive new directory prevents replacing an existing project or receipts.
    destination.mkdir(parents=True, exist_ok=False)
    project = destination / "project"
    project.mkdir()
    _git(project, "init", "-b", "acceptance")
    root = project / PROVIDERS[provider][0]
    evidence = destination / "discovery.json"
    _write_json(
        evidence,
        {
            "schema_version": 1,
            "provider": provider,
            "provider_instance_id": "unobserved-select-from-live-catalog",
            "harness_version": "unobserved",
            "mechanism": "isolated_fixture",
            "source": "routing harness preparation, not provider discovery",
            "observed_at": datetime.now(UTC).isoformat(),
            "roots": [str(root)],
            "skill_sources": [],
        },
    )
    installed = operate(
        "install",
        provider=provider,
        root=root,
        home=destination / "home",
        project=project,
        evidence=evidence,
    )
    if installed["state"] != "installed":
        raise ValueError(installed)
    # Private install manifests contain local paths and stay outside fixture Git.
    (project / ".gitignore").write_text(
        ".oraculo-install.json\n.oraculo-install.lock\n"
    )
    (project / "README.md").write_text("# Isolated provider routing fixture\n")
    (project / "packages/demo").mkdir(parents=True)
    (project / "packages/demo/example.txt").write_text("synthetic fixture\n")
    _git(project, "add", ".")
    _git(project, "commit", "-m", "Prepare isolated skill fixture")
    external = destination / "external-worktree"
    _git(project, "worktree", "add", "-b", "external", str(external))
    binary = destination / "bin/oraculo"
    binary.parent.mkdir()
    harness = Path(__file__).resolve()
    binary.write_text(
        "#!/bin/sh\nexec "
        + shlex.join(
            [
                sys.executable,
                str(harness),
                "_cli",
                "--directory",
                str(destination),
                "--",
            ]
        )
        + ' "$@"\n'
    )
    binary.chmod(0o700)
    cases = {}
    for name, scenario in SCENARIOS.items():
        cases[name] = {
            **scenario,
            "completed": False,
            "loaded_skill": "not_observed",
            "called_cli": "not_observed",
            "confirmed_service": "not_observed",
            "evidence_layer": "simulated_routing",
        }
        invocation = "$oraculo" if provider == "codex" else "/oraculo"
        prompt = (
            "This is an isolated routing acceptance trial. Do not delegate or launch models. "
            "The supplied CLI returns synthetic responses and never touches a live service. "
            "Use the provider's skill discovery; do not infer loading from this prompt. "
            "For each shell invocation set PATH to include "
            + str(binary.parent)
            + f" and ORACULO_ACCEPTANCE_CASE={name}. Use only this fixture's oraculo executable. "
            "Keep all generated payloads under this fixture directory. "
            "No global settings, real DB, external project or network mutation is authorized.\n\n"
            + scenario["prompt"]
            + "\n"
        )
        directory = destination / "cases" / name
        directory.mkdir(parents=True)
        (directory / "implicit.txt").write_text(prompt)
        (directory / "explicit.txt").write_text(invocation + "\n\n" + prompt)
    matrix = {
        "schema_version": 1,
        "provider": provider,
        "provider_instance_id": None,
        "harness_version": None,
        "skill_sha256": installed["source_sha256"],
        "contexts": {
            "root": str(project),
            "subdirectory": str(project / "packages/demo"),
            "external_worktree": str(external),
        },
        "session_variants": ["new", "existing"],
        "invocation_variants": ["explicit", "implicit"],
        "cases": cases,
        "support": "not_validated",
        "gaps": [
            "Real harness discovery/loading and model trials pending",
            "Actual CLI/service/bridge/memory acceptance remains separate",
            "Unsupported harness versions and existing-session refresh pending",
        ],
    }
    _write_json(destination / "matrix.json", matrix)
    return matrix


def simulated_cli(directory: Path, argv: list[str]) -> int:
    case = os.environ.get("ORACULO_ACCEPTANCE_CASE")
    if case not in SCENARIOS:
        raise ValueError("ORACULO_ACCEPTANCE_CASE must name a prepared scenario")
    scenario = SCENARIOS[case]
    help_request = "--help" in argv and argv[0] in {
        "--help",
        "task",
        "skill",
        "service",
        "runtime",
        "status",
        "doctor",
    }
    payload = {}
    if "--file" in argv and not help_request:
        path = Path(argv[argv.index("--file") + 1]).resolve()
        if not path.is_relative_to(directory.resolve()):
            raise ValueError("payload must stay inside the fixture")
        payload = json.loads(path.read_text())
    command = (
        "--help"
        if help_request
        else " ".join(argv[:2])
        if argv and argv[0] in {"task", "skill", "service", "runtime"}
        else (argv[0] if argv else "")
    )
    error = None
    if not argv or (
        not help_request and argv[0] not in {"task", "status", "doctor", "--help"}
    ):
        error = "fixture_operation_not_allowed"
    elif command == "task start" and case == "blocked":
        error = "managed_service_upgrade_required"
    elif command == "task start" and not all(
        payload.get(k) for k in ("key", "text", "cwd")
    ):
        error = "task_brief_required"
    elif (
        command in {"task status", "task resume", "task continue", "task finish"}
        and payload.get("task_id") != "fixture-task"
    ):
        error = "fixture_task_id_mismatch"
    elif command == "task continue" and not all(
        payload.get(k) for k in ("key", "text")
    ):
        error = "fixture_steering_payload_required"
    state_path = directory / "cases" / case / "state.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    if command == "task start" and not error:
        if state.get("request") and state["request"] != payload:
            error = "task_key_reused"
        else:
            state["request"] = payload
            _write_json(state_path, state)
    task = {
        "task_id": "fixture-task",
        "id": "fixture-task",
        "phase": "executing",
        "snapshot_revision": 3,
        "title": "Fixture task",
        "source": "simulated",
    }
    result = {
        "schema_version": 1,
        "source": "simulated",
        "evidence_layer": "simulated_routing",
        "ok": error is None,
        "error_code": error,
        "state": "blocked" if error else "observed",
    }
    if command == "task list":
        result["tasks"] = [task] if scenario["bound"] or state else []
    elif command.startswith("task "):
        result["task"] = task
        if command == "task finish":
            result["state"] = "validation_submitted"
    elif command == "status":
        result["health"] = "online"
    event = {
        "case": case,
        "argv": argv,
        "command": command,
        "payload": payload,
        "exit_code": 1 if error else 0,
        "response": result,
        "pid": os.getpid(),
        "at": datetime.now(UTC).isoformat(),
        "layer": "simulated_routing",
    }
    with (directory / "trace.jsonl").open("a") as stream:
        stream.write(json.dumps(event) + "\n")
    print(json.dumps(result))
    return event["exit_code"]


def evaluate(directory: Path, case: str, completed: bool = False) -> dict:
    scenario = SCENARIOS[case]
    trace = directory / "trace.jsonl"
    events = (
        [json.loads(line) for line in trace.read_text().splitlines()]
        if trace.exists()
        else []
    )
    events = [row for row in events if row["case"] == case]
    commands = [row["command"] for row in events]
    failures = []
    if commands.count("task start") > scenario["max_starts"]:
        failures.append("unexpected_or_duplicate_task_start")
    for command in scenario["required"]:
        if command not in commands:
            failures.append("missing:" + command)
    allowed = {
        "task list",
        "task status",
        "status",
        "doctor",
        "--help",
        *scenario["required"],
    }
    for row in events:
        if row["command"] not in allowed:
            failures.append("unexpected_mutation:" + row["command"])
        expected_block = (
            case == "blocked"
            and row["response"].get("error_code") == "managed_service_upgrade_required"
        )
        if row["exit_code"] != 0 and not expected_block:
            failures.append("cli_failure:" + row["command"])
    if case == "plan":
        for row in events:
            if row["command"] == "task finish":
                report = row["payload"].get("report", {})
                if (
                    report.get("deliverable_kind") != "plan"
                    or report.get("approval_state") != "pending"
                ):
                    failures.append("planning_gate_missing")
    if case == "finish":
        for row in events:
            if row["command"] == "task finish":
                report = row["payload"].get("report", {})
                if not report.get("commit") or not report.get("checks"):
                    failures.append("implementation_validation_missing")
    result = {
        "case": case,
        "state": "failed"
        if failures
        else ("routing_passed" if completed else "not_observed"),
        "failures": failures,
        "commands": commands,
        "completed": completed,
        "loaded_skill": "not_observed",
        "called_cli": "observed_simulated_process" if events else "not_observed",
        "confirmed_service": "not_observed",
        "support": "not_validated",
        "trace_sha256": hashlib.sha256(trace.read_bytes()).hexdigest()
        if trace.exists()
        else None,
        "limitation": "Completion is operator supplied; inspect provider transcript independently. No service integration is certified.",
    }
    _write_json(directory / "cases" / case / "result.json", result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    prep = sub.add_parser("prepare")
    prep.add_argument("--directory", required=True, type=Path)
    prep.add_argument("--provider", required=True, choices=["codex", "claude", "grok"])
    check = sub.add_parser("evaluate")
    check.add_argument("--directory", required=True, type=Path)
    check.add_argument("--case", required=True, choices=list(SCENARIOS))
    check.add_argument(
        "--completed",
        action="store_true",
        help="Operator observed a terminal model trial",
    )
    cli = sub.add_parser("_cli")
    cli.add_argument("--directory", required=True, type=Path)
    cli.add_argument("argv", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.action == "_cli":
        return simulated_cli(
            args.directory, args.argv[1:] if args.argv[:1] == ["--"] else args.argv
        )
    if args.action == "prepare":
        result = prepare(args.directory, args.provider)
    else:
        result = evaluate(args.directory, args.case, args.completed)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return int(result.get("state") == "failed")


if __name__ == "__main__":
    raise SystemExit(main())
