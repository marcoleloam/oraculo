#!/usr/bin/env python3
"""Run offline contract CI on the exact clean checkout, producing a bound receipt."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SELECTION = (
    "config",
    "project_registration",
    "dispatch_health",
    "dirty_source_base",
    "task_panel",
    "reservation_carry",
    "terminal_carry",
    "state",
    "routing",
    "scheduler",
    "plans",
    "plan_service",
    "task_closure",
    "pr_lifecycle",
    "operations",
    "runtime_install",
    "runtime_recovery_identity",
    "runtime_migration",
    "ci_events",
    "ci_host",
    "ci_round_memory",
    "azure_task_closure",
    "ci_azure",
    "operational_decisions",
    "local_rollout",
    "decisions",
    "decision_credentials",
    "decision_recovery",
    "decisions_eval",
    "outcomes",
    "rollout",
    "validation_deliverables",
    "review_product_acceptance",
    "review_product_eval",
    "nested_delivery_review",
    "review_adjudication",
    "review_adjudication_cli",
    "orchestration_ci",
    "service",
    "schema_barrier",
    "notification_delivery",
)


def junit_counts(path):
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    counts = {
        key: sum(int(s.attrib.get(key, 0)) for s in suites)
        for key in ("tests", "failures", "errors", "skipped")
    }
    if counts["tests"] == 0 or any(
        counts[k] for k in ("failures", "errors", "skipped")
    ):
        raise ValueError("mandatory CI selection is incomplete or negative")
    return counts


def verify_subreceipts(output, head):
    brain = json.loads((output / "second-brain").read_text())
    if (
        brain.get("synthetic_release_gate") != "passed"
        or brain.get("technically_eligible_for_next_phase") is not True
        or brain.get("plan_only") is not False
        or brain.get("head") != head
        or brain.get("head_after") != head
        or brain.get("head_stable") is not True
    ):
        raise ValueError("Second Brain receipt is not an exact-source pass")
    distribution = json.loads((output / "distribution/receipt.json").read_text())
    if (
        distribution.get("state") != "passed"
        or distribution.get("source_commit") != head
        or len(distribution.get("artifacts", [])) != 2
    ):
        raise ValueError("distribution receipt is not an exact-source pass")
    return {
        "second_brain_sha256": hashlib.sha256(
            (output / "second-brain").read_bytes()
        ).hexdigest(),
        "distribution_sha256": hashlib.sha256(
            (output / "distribution/receipt.json").read_bytes()
        ).hexdigest(),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expect-head", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-attempt", required=True, type=int)
    args = parser.parse_args()
    output = args.output.resolve()
    if output.is_relative_to(ROOT) or args.run_attempt < 1:
        parser.error("use a disposable directory outside source and a positive attempt")
    output.mkdir(parents=True, exist_ok=False)
    receipt = dict(
        source_commit=args.expect_head,
        run_id=args.run_id,
        run_attempt=args.run_attempt,
        state="failed",
        checks=[],
        fixture_host=True,
        production_activation="not_observed",
    )

    def run(argv, name):
        r = subprocess.run(
            argv,
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=1500,
        )
        (output / (name + ".log")).write_bytes(r.stdout)
        receipt["checks"].append(
            dict(
                command=argv,
                exit=r.returncode,
                log=name + ".log",
                sha256=hashlib.sha256(r.stdout).hexdigest(),
            )
        )
        if r.returncode:
            raise RuntimeError("CI check failed: " + name)
        return r.stdout.decode().strip()

    try:
        if run(["git", "rev-parse", "HEAD"], "head") != args.expect_head or run(
            ["git", "status", "--porcelain"], "clean"
        ):
            raise ValueError("clean exact source required")
        run(
            [sys.executable, "-B", "scripts/validate_ci_exports.py"],
            "ci-exports",
        )
        run(["uv", "sync", "--frozen", "--group", "dev"], "sync")
        run(
            ["uv", "run", "--frozen", "ruff", "check", "src", "tests", "scripts"],
            "lint",
        )
        run(
            [
                "uv",
                "run",
                "--frozen",
                "ruff",
                "format",
                "--check",
                "src",
                "tests",
                "scripts",
            ],
            "format",
        )
        junit = output / "pytest.xml"
        run(
            [
                "uv",
                "run",
                "--frozen",
                "pytest",
                "-q",
                *[f"tests/test_{name}.py" for name in SELECTION],
                "--junitxml=" + str(junit),
                "--durations=25",
            ],
            "pytest",
        )
        receipt["pytest"] = junit_counts(junit)
        receipt["junit_sha256"] = hashlib.sha256(junit.read_bytes()).hexdigest()
        run(
            [
                "uv",
                "run",
                "--frozen",
                "python",
                "-B",
                "scripts/second_brain_release_gate.py",
                "--expect-head",
                args.expect_head,
                "--output",
                str(output / "second-brain"),
            ],
            "second-brain",
        )
        run(
            [
                "uv",
                "run",
                "--frozen",
                "python",
                "-B",
                "scripts/decisions_eval.py",
                "--output",
                str(output / "shadow.json"),
            ],
            "shadow",
        )
        run(
            [
                "uv",
                "run",
                "--frozen",
                "python",
                "-B",
                "scripts/verify_distribution.py",
                "--expect-head",
                args.expect_head,
                "--output",
                str(output / "distribution"),
            ],
            "distribution",
        )
        if run(["git", "rev-parse", "HEAD"], "head-after") != args.expect_head or run(
            ["git", "status", "--porcelain"], "clean-after"
        ):
            raise ValueError("source changed during CI verification")
        receipt["subreceipts"] = verify_subreceipts(output, args.expect_head)
        receipt["state"] = "passed"
    finally:
        (output / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
