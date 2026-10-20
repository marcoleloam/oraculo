#!/usr/bin/env python3
"""Operator-only recovery of a completed original run with later completed turns.

Requires explicit authorization, fresh complete T3 observations and the existing
immutable report hash. Does not contact T3, dispatch work, retry, cancel, run a
daemon, migrate the database or change the normal reconciler's conservative rule.
Default is a dry run against an in-memory consistent copy. --apply uses the
existing State.record_execution transition inside one SQLite transaction.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import time
from pathlib import Path

from oraculo.plans import Plans
from oraculo.protocol import canonical_json
from oraculo.state import State, StateError
from oraculo.t3.contracts import ThreadRead


def digest(value):
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


class ExistingState(State):
    """Attach the existing state API without running schema migrations."""

    def __init__(self, connection):
        self.connection = connection
        self.max_executors = 1  # Unused: this procedure never admits execution.
        self.provider_limits = {}
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")


def recover(state, proof, attempt_id, generation, report_hash, *, apply=False):
    def require(condition, reason):
        if not condition:
            raise StateError(reason)

    require(0 <= time.time() - proof["observed_at"] <= 60, "stale_remote_proof")
    with state.transaction():
        attempt = state.get_attempt(attempt_id)
        lease = state.lease(attempt_id)
        require(
            attempt
            and lease
            and attempt["generation"] == generation == lease["generation"]
            and attempt["status"] == lease["state"] == "uncertain",
            "grant_changed",
        )
        job = state.get_job(attempt["job_id"])
        objective = state.get_objective(job["objective_id"])
        require(job["state"] == "uncertain", "job_changed")
        require(
            objective["status"] not in {"succeeded", "stopped", "cancel_requested"},
            "objective_changed",
        )
        report = state.get_meta(f"execution_report:{attempt_id}", {})
        require(digest(report) == report_hash, "report_changed")
        require(
            report.get("attempt_id") == attempt_id
            and report.get("generation") == generation
            and report.get("run_id") == attempt["run_id"]
            and report.get("status") == "succeeded"
            and report.get("evidence"),
            "report_not_bound_to_original_success",
        )
        page = proof["project_threads"]
        require(
            page["projectId"] == objective["project_id"] and page["nextCursor"] is None,
            "incomplete_project_tree",
        )
        rows = page["threads"]
        require(
            len({r["threadId"] for r in rows}) == len(rows)
            and any(r["threadId"] == attempt["thread_id"] for r in rows),
            "ambiguous_project_tree",
        )
        ids = {attempt["thread_id"]}
        while True:
            children = {r["threadId"] for r in rows if r.get("parentThreadId") in ids}
            if children <= ids:
                break
            ids |= children
        require(set(proof["snapshots"]) == ids, "incomplete_execution_tree")
        for thread_id, snapshot in proof["snapshots"].items():
            ThreadRead.model_validate(snapshot)
            thread = snapshot["thread"]
            runs = snapshot["recentRuns"]
            require(
                thread["threadId"] == thread_id
                and thread["projectId"] == objective["project_id"]
                and thread["status"] == "completed"
                and thread["activeRunId"] is None
                and thread["pendingRequestCount"] == 0
                and not snapshot.get("hasPendingChildRuns", False),
                "execution_tree_not_quiet",
            )
            require(
                len(runs) == thread["runCount"]
                and len({r["runId"] for r in runs}) == len(runs)
                and all(
                    r["status"] == "completed" and r.get("completedAt") for r in runs
                )
                and any(r["runId"] == thread["latestRunId"] for r in runs),
                "incomplete_or_ambiguous_run_history",
            )
        root = proof["snapshots"][attempt["thread_id"]]
        require(
            root["thread"]["parentThreadId"] == attempt["owner_thread_id"]
            and root["thread"]["latestRunId"] != attempt["run_id"]
            and any(r["runId"] == attempt["run_id"] for r in root["recentRuns"]),
            "original_run_not_proven",
        )
        require(
            not state._one(
                "SELECT id FROM operations WHERE attempt_id=? AND state!='confirmed'",
                (attempt_id,),
            ),
            "operation_not_confirmed",
        )
        # Existing criteria checks, including verified delivery operations.
        plans = Plans(state)
        require(plans.get(objective["id"]) is not None, "planned_job_required")
        plans.validate_report(attempt, report)
        evidence = {
            "reconciliation": "operator_verified_original_success",
            "attempt_id": attempt_id,
            "generation": generation,
            "run_id": attempt["run_id"],
            "report_sha256": report_hash,
            "remote_proof_sha256": digest(proof),
            "observed_at": proof["observed_at"],
            "tree_terminal": True,
        }
        if apply:
            require(
                state.record_execution(
                    attempt_id,
                    generation,
                    attempt["run_id"],
                    "succeeded",
                    pending_children=False,
                    tree_confirmed=True,
                    evidence=evidence,
                ),
                "transition_not_applied",
            )
        return dict(evidence, applied=apply, criteria_validated=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--proof", type=Path, required=True)
    parser.add_argument("--attempt", required=True)
    parser.add_argument("--generation", type=int, required=True)
    parser.add_argument("--report-sha256", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    path = args.database.resolve()
    if args.apply:
        connection = sqlite3.connect(
            path.as_uri() + "?mode=rw", uri=True, isolation_level=None
        )
    else:
        connection = sqlite3.connect(":memory:", isolation_level=None)
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as source:
            source.backup(connection)
    state = ExistingState(connection)
    try:
        result = recover(
            state,
            json.loads(args.proof.read_text()),
            args.attempt,
            args.generation,
            args.report_sha256,
            apply=args.apply,
        )
        print(json.dumps(result))
    finally:
        state.close()


if __name__ == "__main__":
    main()
