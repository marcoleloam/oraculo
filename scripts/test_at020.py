"""Run the AT-020 local battery and retain a timestamped receipt (no live T3)."""

import argparse
import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

CASES = [
    "tests/test_continuity.py",
    "tests/test_dispatch_history.py",
    "tests/test_memory_transport.py",
    "tests/test_lifecycle.py::test_singleton_restart_preserves_state_and_socket_permissions",
    "tests/test_reconcile.py",
    "tests/test_reconcile_cancelled.py",
    "tests/test_service.py::test_submit_dispatch_execute_reconcile_queues_memory",
    "tests/test_service.py::test_memory_dispatch_replays_persisted_payload_after_restart",
    "tests/test_service.py::test_dispatch_retry_after_restart_is_bounded_and_preserves_payload",
    "tests/test_service.py::test_dispatch_does_not_retry_unproven_terminal_run",
    "tests/test_service.py::test_dispatch_rechecks_admission_after_terminal_read",
    "tests/test_service.py::test_dispatch_retry_lost_response_replays_same_key",
    "tests/test_service.py::test_execution_report_is_immutable_and_replayable",
    "tests/test_service.py::test_cancel_and_status_bypass_occupied_executor",
    "tests/test_t3_contracts.py::test_old_run_does_not_complete_current",
]


def main():
    root = Path(__file__).resolve().parent.parent
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    output = (args.output_dir or root / ".claude/sdd/reports/AT020" / stamp).resolve()
    output.mkdir(parents=True, exist_ok=False)
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "--tb=short",
        *CASES,
        f"--junitxml={output / 'junit.xml'}",
    ]
    result = subprocess.run(command, cwd=root, text=True, capture_output=True)
    (output / "pytest.txt").write_text(result.stdout + result.stderr)
    cases = []
    if (output / "junit.xml").exists():
        for case in ET.parse(output / "junit.xml").iter("testcase"):
            status = next(
                (
                    tag
                    for tag in ("failure", "error", "skipped")
                    if case.find(tag) is not None
                ),
                "pass",
            )
            cases.append(
                {
                    "name": case.get("name"),
                    "class": case.get("classname"),
                    "status": status,
                }
            )
    receipt = {
        "at": stamp,
        "scope": "Local components and real daemon lifecycle; T3 transport simulated. Does not attest UI closure or subscription.",
        "exit_code": result.returncode,
        "command": command,
        "cases": cases,
        "counts": {
            status: sum(c["status"] == status for c in cases)
            for status in ("pass", "failure", "error", "skipped")
        },
    }
    (output / "result.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(
        json.dumps(
            {
                "output_dir": str(output),
                "exit_code": result.returncode,
                "counts": receipt["counts"],
            },
            indent=2,
        )
    )
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
