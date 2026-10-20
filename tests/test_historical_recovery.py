import importlib.util
import time
from pathlib import Path

import pytest

from oraculo.plans import Plans
from oraculo.state import State, StateError

spec = importlib.util.spec_from_file_location(
    "historical_recovery",
    Path(__file__).parents[1] / "scripts/reconcile-historical.py",
)
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)


@pytest.fixture
def case(tmp_path):
    state = State(tmp_path / "state.sqlite3")
    state.register_project("p", "/tmp/p")
    plan = Plans(state).submit(
        "p",
        {
            "text": "work",
            "tasks": [
                {
                    "id": "one",
                    "owner": "test",
                    "brief": "work",
                    "criteria": [{"id": "proof", "description": "verified"}],
                }
            ],
        },
        "plan",
        {"implementation": ["codex"]},
    )
    grant = state.acquire_grant(
        plan["tasks"][0]["job_id"], "codex", "m", "owner", "owner-run"
    )
    state.claim_grant(grant["id"], grant["generation"], "child", "original")
    state.mark_uncertain(grant["id"], "missing report at first observation")
    report = {
        "attempt_id": grant["id"],
        "generation": grant["generation"],
        "run_id": "original",
        "status": "succeeded",
        "evidence": {
            "criteria": [
                {"id": "proof", "status": "passed", "reference": "test output"}
            ]
        },
    }
    state.set_meta(f"execution_report:{grant['id']}", report)
    snapshot = {
        "thread": {
            "threadId": "child",
            "projectId": "p",
            "parentThreadId": "owner",
            "activeRunId": None,
            "latestRunId": "later",
            "status": "completed",
            "pendingRequestCount": 0,
            "runCount": 2,
        },
        "recentRuns": [
            {"runId": r, "status": "completed", "completedAt": "2026-10-20T17:00:00Z"}
            for r in ["original", "later"]
        ],
        "items": [],
        "hasMore": False,
        "nextPosition": None,
    }
    proof = {
        "observed_at": time.time(),
        "project_threads": {
            "projectId": "p",
            "nextCursor": None,
            "threads": [{"threadId": "child", "parentThreadId": "owner"}],
        },
        "snapshots": {"child": snapshot},
    }
    yield state, grant, report, proof
    state.close()


def recover(case, apply=True):
    state, grant, report, proof = case
    return recovery.recover(
        state,
        proof,
        grant["id"],
        grant["generation"],
        recovery.digest(report),
        apply=apply,
    )


def test_verified_original_report_recovers_without_new_attempt(case):
    state, grant, _, _ = case
    assert recover(case, apply=False)["applied"] is False
    assert state.lease(grant["id"])
    assert recover(case)["applied"] is True
    assert not state.lease(grant["id"])
    assert state.get_attempt(grant["id"])["status"] == "succeeded"
    assert state.connection.execute("SELECT count(*) FROM attempts").fetchone()[0] == 1
    with pytest.raises(StateError, match="grant_changed"):
        recover(case)


@pytest.mark.parametrize(
    "failure",
    [
        "stale",
        "missing_original",
        "active",
        "pending",
        "later_report",
        "failed_criterion",
        "changed_report",
        "incomplete_page",
        "missing_child",
        "generation",
        "operation",
    ],
)
def test_recovery_refuses_uncertainty_and_preserves_grant(case, failure):
    state, grant, report, proof = case
    root = proof["snapshots"]["child"]
    if failure == "stale":
        proof["observed_at"] -= 61
    elif failure == "missing_original":
        root["recentRuns"][0]["runId"] = "different"
    elif failure == "active":
        root["thread"]["activeRunId"] = "new-run"
    elif failure == "pending":
        root["thread"]["pendingRequestCount"] = 1
    elif failure == "later_report":
        report["run_id"] = "later"
        state.set_meta(f"execution_report:{grant['id']}", report)
    elif failure == "failed_criterion":
        report["evidence"]["criteria"][0]["status"] = "failed"
        state.set_meta(f"execution_report:{grant['id']}", report)
    elif failure == "changed_report":
        state.set_meta(f"execution_report:{grant['id']}", {})
    elif failure == "incomplete_page":
        proof["project_threads"]["nextCursor"] = 100
    elif failure == "missing_child":
        proof["project_threads"]["threads"].append(
            {"threadId": "grandchild", "parentThreadId": "child"}
        )
    elif failure == "generation":
        grant["generation"] += 1
    elif failure == "operation":
        from oraculo.operations import Operations

        Operations(state).prepare(
            "pending-launch",
            grant["id"],
            "launch",
            {"target": "test"},
            generation=grant["generation"],
        )
    with pytest.raises(StateError):
        recover(case)
    assert state.lease(grant["id"])
    assert state.get_attempt(grant["id"])["status"] == "uncertain"
