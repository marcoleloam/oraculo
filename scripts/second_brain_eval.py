#!/usr/bin/env python3
"""Measure the synthetic second-brain corpus.

Writes a JSON report with recall@10, MRR, context size and latency. The file
contains synthetic aggregates only. Ranker choice uses the tuning split; the
acceptance split is reported afterwards and does not change the choice.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from oraculo.memory.evaluation import SCALES, run_benchmark  # noqa: E402


def _head_sha() -> str | None:
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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Path for the synthetic metrics JSON",
    )
    parser.add_argument(
        "--scales",
        default=",".join(str(scale) for scale in SCALES),
        help="Comma-separated page counts. Default: 50,500,5000",
    )
    parser.add_argument(
        "--work",
        type=Path,
        default=None,
        help="Directory for generated vaults. Default: a temporary directory",
    )
    args = parser.parse_args()
    scales = tuple(int(item) for item in args.scales.split(",") if item)
    if args.work is None:
        with tempfile.TemporaryDirectory(prefix="second-brain-eval-") as directory:
            report = run_benchmark(Path(directory), scales=scales)
            _write(args.output, report)
        return
    args.work.mkdir(parents=True, exist_ok=True)
    report = run_benchmark(args.work, scales=scales)
    _write(args.output, report)


def _write(path: Path, report: dict) -> None:
    report["measured_head"] = _head_sha()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    choice = report["choice"]
    print(
        json.dumps(
            {
                "output": str(path),
                "selected": choice["selected"],
                "constant_matches": choice["constant_matches"],
                "acceptance": [
                    {
                        "scale": item["scale"],
                        "passed": item["acceptance_gate"]["passed"],
                        "baseline_recall_at_10": item["acceptance_gate"].get(
                            "baseline_recall_at_10"
                        ),
                        "candidate_recall_at_10": item["acceptance_gate"].get(
                            "candidate_recall_at_10"
                        ),
                    }
                    for item in report["scales"]
                ],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
