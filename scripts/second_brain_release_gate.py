#!/usr/bin/env python3
"""Run the synthetic Second Brain release gate and write a JSON receipt.

The receipt records command exits, durations and artifact hashes. A pass is
technical eligibility for a later controlled activation. It is not human
utility, not a real 14-day pilot, and not proof that the installed service
was updated.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RECALL_FLOOR = 0.90
REQUIRED_SCALES = (50, 500, 5000)
AUTHORED_AGAINST = "c113006653a3cff63703dd925ed1d44ecc4b4163"
FUNCTIONAL_SOURCE = "f403c749404c06e8c5d7f1458557801d47caf58f"

LIFECYCLE_COUNTERS = {
    "cross_project_leaks": 0,
    "unsupported_new_facts": 0,
    "duplicated_operations": 0,
    "post_cancel_new_writes": 0,
    "required_skips": 0,
}
LIFECYCLE_FLAGS = {
    "query_canonical_unchanged": True,
    "raw_preserved": True,
    "reviewed_preserved": True,
    "source_limits_respected": True,
    "context_budgets_respected": True,
    "disabled_default": True,
    "no_additional_spend": True,
    "human_utility_observed": False,
    "real_elapsed_wait_used": False,
    "simulated_clock": True,
    "fixture_host_metadata": True,
    "not_live_host": True,
}
LIFECYCLE_EQUALS = {
    "clock": "virtual_injected_clock",
    "pilot_state": "accepted_simulation",
    "virtual_days": 30,
}

ACTIVATION_CONDITIONS = (
    {
        "id": "runtime_quiescence",
        "phase": "host_activation",
        "status": "not_executed",
        "owner": "parent",
        "note": "Installed service quiescence, backup and active-agent window.",
    },
    {
        "id": "bundle_install",
        "phase": "host_activation",
        "status": "not_executed",
        "owner": "parent",
        "note": "Package install and release switch stay outside this gate.",
    },
    {
        "id": "dispatch_health",
        "phase": "host_activation",
        "status": "not_executed",
        "owner": "parent",
        "note": (
            "Historical health dispatch_attempts_exhausted is not diagnosed "
            "or cleared here."
        ),
    },
    {
        "id": "live_corpus_selection",
        "phase": "host_activation",
        "status": "not_executed",
        "owner": "parent",
        "note": "No live project corpus is selected without a user choice.",
    },
    {
        "id": "real_elapsed_pilot",
        "phase": "human_utility",
        "status": "not_this_gate",
        "owner": "not_applicable",
        "note": (
            "The mandatory calendar wait is replaced by the explicit user "
            "decision. Simulation remains accepted_simulation."
        ),
    },
)

POST_ACTIVATION = {
    "id": "post_activation_readonly_smoke",
    "phase": "post_activation",
    "status": "not_executed",
    "owner": "parent",
    "note": "Read-only smoke after a real release switch. This runner does not start it.",
}


def command_status(exit_code: int | None, timed_out: bool) -> str:
    """Derive status from the process. A requested pass cannot override the exit."""
    if timed_out or exit_code != 0:
        return "failed"
    return "passed"


def classify_junit(path: Path) -> dict:
    if not path.is_file():
        return {
            "status": "failed",
            "reason": "junit_missing",
            "tests": None,
            "failures": None,
            "errors": None,
            "skipped": None,
        }
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.findall("testsuite"))
    if not suites:
        return {
            "status": "failed",
            "reason": "junit_missing_suite",
            "tests": None,
            "failures": None,
            "errors": None,
            "skipped": None,
        }
    totals = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    for suite in suites:
        for key in totals:
            totals[key] += int(suite.attrib.get(key, "0"))
    clean = totals["failures"] == 0 and totals["errors"] == 0 and totals["skipped"] == 0
    return {
        "status": "passed" if clean and totals["tests"] > 0 else "failed",
        "reason": None if clean and totals["tests"] > 0 else "junit_not_clean",
        **totals,
    }


def _number(value) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def evaluate_lifecycle_artifact(payload: dict | None) -> dict:
    if not isinstance(payload, dict):
        return {"passed": False, "reason": "lifecycle_artifact_missing", "gaps": []}
    gaps = []
    for key, expected in LIFECYCLE_COUNTERS.items():
        if payload.get(key) != expected:
            gaps.append(key)
    for key, expected in LIFECYCLE_FLAGS.items():
        if payload.get(key) is not expected:
            gaps.append(key)
    for key, expected in LIFECYCLE_EQUALS.items():
        if payload.get(key) != expected:
            gaps.append(key)
    return {
        "passed": not gaps,
        "reason": None if not gaps else "lifecycle_thresholds",
        "gaps": gaps,
    }


def evaluate_retrieval(report: dict | None) -> dict:
    """Thresholds only. p95 is recorded and is not a production SLA."""
    if not isinstance(report, dict):
        return {
            "passed": False,
            "reason": "retrieval_artifact_missing",
            "scales": [],
            "semantic_accuracy_claimed": False,
            "production_sla_claimed": False,
        }
    scales = []
    gaps = []
    choice = report.get("choice") or {}
    if choice.get("constant_matches") is not True:
        gaps.append("frozen_ranker_mismatch")
    observed_scales = [item.get("scale") for item in report.get("scales") or []]
    if (
        len(observed_scales) != len(REQUIRED_SCALES)
        or any(observed_scales.count(scale) != 1 for scale in REQUIRED_SCALES)
        or any(type(scale) is not int for scale in observed_scales)
    ):
        gaps.append("required_scales_missing_or_duplicated")
    for item in report.get("scales") or []:
        gate = item.get("acceptance_gate") or {}
        acceptance = item.get("acceptance") or {}
        budgeted = acceptance.get("budgeted") or {}
        legacy = acceptance.get("legacy_bundle") or {}
        recall = gate.get("candidate_recall_at_10")
        baseline_mrr = legacy.get("mrr")
        candidate_mrr = budgeted.get("mrr")
        p95 = budgeted.get("latency_ms_p95")
        language = gate.get("language_regression")
        scale_gaps = []
        if gate.get("passed") is not True:
            scale_gaps.append("acceptance_gate")
        if not _number(recall) or not RECALL_FLOOR - 1e-12 <= recall <= 1:
            scale_gaps.append("recall_floor")
        if language != []:
            scale_gaps.append("language_regression")
        if (
            not _number(baseline_mrr)
            or not _number(candidate_mrr)
            or not 0 <= baseline_mrr <= 1
            or not 0 <= candidate_mrr <= 1
            or candidate_mrr + 1e-12 < baseline_mrr
        ):
            scale_gaps.append("mrr_below_legacy_baseline")
        if not _number(p95) or p95 < 0:
            scale_gaps.append("p95_not_recorded")
        if scale_gaps:
            gaps.append({"scale": item.get("scale"), "gaps": scale_gaps})
        scales.append(
            {
                "scale": item.get("scale"),
                "recall_at_10": recall,
                "baseline_mrr": baseline_mrr,
                "candidate_mrr": candidate_mrr,
                "latency_ms_p95": p95,
                "language_regression": language,
                "production_sla_claimed": False,
            }
        )
    if not scales:
        gaps.append("no_scales")
    return {
        "passed": not gaps,
        "reason": None if not gaps else "retrieval_thresholds",
        "gaps": gaps,
        "scales": scales,
        "semantic_accuracy_claimed": False,
        "production_sla_claimed": False,
        "manifest": report.get("manifest"),
        "measured_head": report.get("measured_head"),
        "measured_ranker": report.get("measured_ranker"),
    }


def _sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def _head() -> str | None:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return completed.stdout.strip() or None


def _load_json(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def build_plan(artifact_dir: Path, scales: str) -> list[dict]:
    junit_lifecycle = artifact_dir / "synthetic-lifecycle.junit.xml"
    lifecycle_json = artifact_dir / "synthetic-lifecycle.json"
    regression_junit = artifact_dir / "selected-regressions.junit.xml"
    benchmark_json = artifact_dir / "frozen-retrieval.json"
    benchmark_work = artifact_dir / "frozen-retrieval-work"
    regressions = [
        "tests/test_second_brain_evaluation.py",
        "tests/test_memory_retrieval.py",
        "tests/test_memory_scope.py",
        "tests/test_knowledge_routines.py",
        "tests/test_knowledge.py",
        "tests/test_knowledge_service.py",
    ]
    return [
        {
            "id": "synthetic_lifecycle",
            "phase": "preactivation_synthetic",
            "required": True,
            "skips_allowed": False,
            "timeout_seconds": 240,
            "command": [
                "uv",
                "run",
                "--frozen",
                "pytest",
                "-p",
                "no:cacheprovider",
                "-q",
                "-ra",
                "--tb=line",
                f"--junitxml={junit_lifecycle}",
                "tests/test_second_brain_synthetic_gate.py",
            ],
            "env": {"SECOND_BRAIN_SYNTHETIC_ARTIFACT": str(lifecycle_json)},
            "junit": junit_lifecycle,
            "artifact": lifecycle_json,
            "kind": "lifecycle",
        },
        {
            "id": "frozen_retrieval_f1_v2",
            "phase": "preactivation_synthetic",
            "required": True,
            "skips_allowed": False,
            "timeout_seconds": 600,
            "command": [
                "uv",
                "run",
                "--frozen",
                "python",
                "-B",
                "scripts/second_brain_eval.py",
                "--output",
                str(benchmark_json),
                "--scales",
                scales,
                "--work",
                str(benchmark_work),
            ],
            "env": {},
            "junit": None,
            "artifact": benchmark_json,
            "kind": "retrieval",
        },
        {
            "id": "selected_regressions",
            "phase": "preactivation_synthetic",
            "required": True,
            "skips_allowed": False,
            "timeout_seconds": 300,
            "command": [
                "uv",
                "run",
                "--frozen",
                "pytest",
                "-p",
                "no:cacheprovider",
                "-q",
                "-ra",
                "--tb=line",
                f"--junitxml={regression_junit}",
                *regressions,
            ],
            "env": {},
            "junit": regression_junit,
            "artifact": regression_junit,
            "kind": "pytest",
        },
    ]


def _run(spec: dict, log_path: Path) -> dict:
    env = os.environ.copy()
    env.update(spec["env"])
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    started = time.perf_counter()
    timed_out = False
    try:
        completed = subprocess.run(
            spec["command"],
            cwd=ROOT,
            env=env,
            timeout=spec["timeout_seconds"],
            capture_output=True,
            text=True,
            check=False,
        )
        exit_code: int | None = completed.returncode
        log_path.write_text(completed.stdout + completed.stderr, encoding="utf-8")
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        exit_code = None
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", "replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", "replace")
        log_path.write_text(stdout + stderr, encoding="utf-8")
    duration = round(time.perf_counter() - started, 6)
    status = command_status(exit_code, timed_out)
    junit = None
    if spec["junit"] is not None:
        junit = classify_junit(spec["junit"])
        if status == "passed" and junit["status"] != "passed":
            status = "failed"
    evaluation = None
    if spec["kind"] == "lifecycle":
        evaluation = evaluate_lifecycle_artifact(_load_json(spec["artifact"]))
        if status == "passed" and not evaluation["passed"]:
            status = "failed"
    elif spec["kind"] == "retrieval":
        evaluation = evaluate_retrieval(_load_json(spec["artifact"]))
        if status == "passed" and not evaluation["passed"]:
            status = "failed"
    artifact = spec["artifact"]
    return {
        "id": spec["id"],
        "phase": spec["phase"],
        "required": spec["required"],
        "command": spec["command"],
        "status": status,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "duration_seconds": duration,
        "log_sha256": _sha256(log_path),
        "artifact": str(artifact),
        "artifact_sha256": _sha256(artifact),
        "junit": junit,
        "evaluation": evaluation,
    }


def assemble_receipt(
    *,
    head: str | None,
    head_after: str | None,
    plan_only: bool,
    checks: list[dict],
    scales: str,
) -> dict:
    head_stable = bool(head) and head == head_after
    required_passed = all(
        item["status"] == "passed" for item in checks if item["required"]
    )
    executed = not plan_only and bool(checks)
    eligible = executed and required_passed and head_stable
    gaps = [dict(item) for item in ACTIVATION_CONDITIONS]
    gaps.append(dict(POST_ACTIVATION))
    if not head_stable:
        gaps.append(
            {
                "id": "head_drift",
                "phase": "preactivation_synthetic",
                "status": "failed",
                "note": "HEAD changed while the gate was running.",
            }
        )
    for item in checks:
        if item["status"] != "passed":
            gaps.append(
                {
                    "id": item["id"],
                    "phase": item["phase"],
                    "status": item["status"],
                    "note": "Required synthetic check did not pass.",
                }
            )
    retrieval = next(
        (
            item.get("evaluation")
            for item in checks
            if item["id"] == "frozen_retrieval_f1_v2"
        ),
        None,
    )
    lifecycle = next(
        (
            item.get("evaluation")
            for item in checks
            if item["id"] == "synthetic_lifecycle"
        ),
        None,
    )
    return {
        "schema": "second-brain-synthetic-release-gate-v1",
        "head": head,
        "head_after": head_after,
        "head_stable": head_stable,
        "authored_against": AUTHORED_AGAINST,
        "functional_source_sha": FUNCTIONAL_SOURCE,
        "functional_source_note": (
            "Historical functional commit from the implementation status. "
            "This receipt does not replay or certify that suite."
        ),
        "scales": scales,
        "plan_only": plan_only,
        "mandatory_elapsed_wait": False,
        "semantic_accuracy_claimed": False,
        "production_sla_claimed": False,
        "human_utility_accepted": False,
        "real_pilot_accepted": False,
        "runtime_activated": False,
        "activation_performed": False,
        "global_skip_count_is_proof": False,
        "synthetic_release_gate": (
            "not_executed" if plan_only else "passed" if eligible else "failed"
        ),
        "technically_eligible_for_next_phase": eligible,
        "checks": checks,
        "gates": {"lifecycle": lifecycle, "retrieval": retrieval},
        "activation_conditions": list(ACTIVATION_CONDITIONS),
        "post_activation_smoke": dict(POST_ACTIVATION),
        "gaps": gaps,
    }


def _planned_checks(plan: list[dict]) -> list[dict]:
    planned = []
    for spec in plan:
        planned.append(
            {
                "id": spec["id"],
                "phase": spec["phase"],
                "required": True,
                "command": spec["command"],
                "status": "not_executed",
                "exit_code": None,
                "timed_out": False,
                "duration_seconds": 0,
                "log_sha256": None,
                "artifact": str(spec["artifact"]),
                "artifact_sha256": None,
                "junit": None,
                "evaluation": None,
            }
        )
    return planned


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--artifact-dir", type=Path)
    parser.add_argument("--expect-head")
    parser.add_argument("--scales", default="50,500,5000")
    parser.add_argument(
        "--plan",
        action="store_true",
        help="Write the command plan without running it and without a pass",
    )
    args = parser.parse_args(argv)
    output = args.output
    if output.exists():
        print("refusing to overwrite " + str(output), file=sys.stderr)
        return 2
    output.parent.mkdir(parents=True, exist_ok=True)
    artifact_dir = args.artifact_dir or output.parent / (output.stem + "-artifacts")
    artifact_dir.mkdir(parents=True, exist_ok=True)
    head = _head()
    if args.expect_head and head != args.expect_head:
        receipt = assemble_receipt(
            head=head,
            head_after=head,
            plan_only=True,
            checks=[],
            scales=args.scales,
        )
        receipt["synthetic_release_gate"] = "failed"
        receipt["technically_eligible_for_next_phase"] = False
        receipt["gaps"].append(
            {
                "id": "expect_head",
                "phase": "preactivation_synthetic",
                "status": "failed",
                "note": "HEAD does not match --expect-head. No checks were started.",
            }
        )
        output.write_text(
            json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return 2
    plan = build_plan(artifact_dir, args.scales)
    if args.plan:
        receipt = assemble_receipt(
            head=head,
            head_after=head,
            plan_only=True,
            checks=_planned_checks(plan),
            scales=args.scales,
        )
        output.write_text(
            json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return 0
    checks = []
    for index, spec in enumerate(plan, start=1):
        log_path = artifact_dir / f"{index:02d}-{spec['id']}.log"
        checks.append(_run(spec, log_path))
    head_after = _head()
    receipt = assemble_receipt(
        head=head,
        head_after=head_after,
        plan_only=False,
        checks=checks,
        scales=args.scales,
    )
    output.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "output": str(output),
                "synthetic_release_gate": receipt["synthetic_release_gate"],
                "technically_eligible_for_next_phase": receipt[
                    "technically_eligible_for_next_phase"
                ],
                "head": head_after,
            },
            ensure_ascii=False,
        )
    )
    return 0 if receipt["technically_eligible_for_next_phase"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
