#!/usr/bin/env python3
"""Evaluate product journeys independently of executor PASS prose.

An exit-zero scenario run is deliberately distinct from full product acceptance:
The offline verdict never claims live provider approval, merge or activation.
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def scenario_matrix(contract, cases):
    matrix = []
    for scenario in contract["scenarios"]:
        matches = [
            c for c in cases if c.attrib["name"].startswith(scenario["test_prefix"])
        ]
        passed = bool(matches) and not any(
            child.tag in {"failure", "error", "skipped"}
            for case in matches
            for child in case
        )
        matrix.append(
            dict(
                id=scenario["id"],
                requirement=scenario["requirement"],
                scenarios=[c.attrib["name"] for c in matches],
                proof="scenarios.xml",
                result="passed" if passed else "failed_or_missing",
            )
        )
    return matrix


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expect-head", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    output = args.output.resolve()
    if output.is_relative_to(ROOT):
        parser.error("use a private evidence directory outside the checkout")
    output.mkdir(parents=True, exist_ok=False)
    contract_path = ROOT / "docs/AUTOMATED_REVIEW_ACCEPTANCE.json"
    contract = json.loads(contract_path.read_bytes())
    receipt = dict(
        source_commit=args.expect_head,
        contract_sha256=hashlib.sha256(contract_path.read_bytes()).hexdigest(),
        evaluation_scope=contract["evaluation_scope"],
        scenario_result="failed",
        product_acceptance="NOT_EVALUATED",
        full_autonomy_gaps=contract["full_autonomy_gaps"],
        matrix=[],
    )

    def git(*argv):
        return subprocess.check_output(["git", *argv], cwd=ROOT, text=True).strip()

    code = 1
    try:
        if git("rev-parse", "HEAD") != args.expect_head or git("status", "--porcelain"):
            raise ValueError("clean exact source required")
        argv = [
            sys.executable,
            "-B",
            "-m",
            "pytest",
            "-q",
            *contract["tests"],
            "--junitxml=" + str(output / "scenarios.xml"),
        ]
        with (output / "scenarios.log").open("wb") as log:
            result = subprocess.run(
                argv,
                cwd=ROOT,
                stdout=log,
                stderr=subprocess.STDOUT,
                env={
                    **os.environ,
                    "PYTHONPATH": str(ROOT / "src"),
                    "PYTHONDONTWRITEBYTECODE": "1",
                },
                timeout=1500,
            )
        receipt["command"] = argv
        receipt["exit_code"] = result.returncode
        cases = list(ET.parse(output / "scenarios.xml").getroot().iter("testcase"))
        negative = any(
            any(c.tag in {"failure", "error", "skipped"} for c in case)
            for case in cases
        )
        receipt["matrix"] = scenario_matrix(contract, cases)
        if (
            result.returncode
            or negative
            or not cases
            or any(r["result"] != "passed" for r in receipt["matrix"])
        ):
            raise ValueError("product scenarios failed, skipped, or missing")
        if git("rev-parse", "HEAD") != args.expect_head or git("status", "--porcelain"):
            raise ValueError("source changed during evaluation")
        receipt["scenario_result"] = "passed"
        receipt["product_acceptance"] = "ACCEPTED_OFFLINE"
        code = 0
    except (ValueError, OSError, subprocess.SubprocessError, ET.ParseError) as exc:
        receipt["error"] = str(exc)
    finally:
        receipt["proofs"] = {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in output.iterdir()
            if p.is_file()
        }
        (output / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(
        json.dumps(
            dict(
                scenario_result=receipt["scenario_result"],
                product_acceptance=receipt["product_acceptance"],
                receipt=str(output / "receipt.json"),
            )
        )
    )
    return code


if __name__ == "__main__":
    raise SystemExit(main())
