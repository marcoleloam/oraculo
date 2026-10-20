"""Recovery after T3 restarts an executor with a different run identity."""

import pytest
from test_reconcile import Remote, prepare, snapshot

from oraculo.reconcile import Reconciler
from oraculo.state import State


def restarted_executor(status="cancelled"):
    current = snapshot("child", "later-run", "completed")
    current["recentRuns"].append({"runId": "run", "status": status})
    return current


@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize("original_status", ["cancelled", "interrupted"])
async def test_historical_stop_releases_original_identity_once(
    tmp_path, restart, cancel, original_status
):
    state, objective, grant = prepare(tmp_path)
    identity = state.get_attempt(grant["id"])
    if cancel:
        state.cancel_objective(objective["id"])
    if restart:
        state.close()
        state = State(tmp_path / "db")
    remote = Remote()
    remote.snapshots["child"] = restarted_executor(original_status)
    # Even a success report from the newer run cannot turn recovery into success.
    state.set_meta(
        f"execution_report:{grant['id']}",
        {
            "run_id": "later-run",
            "generation": grant["generation"],
            "status": "succeeded",
            "evidence": {"checks": "passed"},
        },
    )
    reconciler = Reconciler(state, remote)
    assert await reconciler.reconcile() == {"state": "stopped", "released": True}
    assert state.lease() is None
    assert state.get_objective(objective["id"])["status"] == "stopped"
    recorded = state.get_attempt(grant["id"])
    for key in ("id", "generation", "thread_id", "run_id", "job_id"):
        assert recorded[key] == identity[key]
    assert recorded["status"] == "stopped"
    assert await reconciler.reconcile() == {"state": "idle"}
    assert state.connection.execute("SELECT count(*) FROM attempts").fetchone()[0] == 1
    assert remote.interruptions == []
    state.close()


@pytest.mark.parametrize(
    "condition",
    [
        "original_missing",
        "latest_missing",
        "original_contradiction",
        "latest_contradiction",
        "latest_active",
        "root_pending",
        "child_pending",
        "descendant_active",
        "descendant_pending",
        "root_wrong_identity",
        "descendant_wrong_identity",
        "original_completed",
        "original_failed",
        "latest_identity_unknown",
        "original_id_unknown",
    ],
)
async def test_recovery_fails_closed(tmp_path, condition):
    state, _, grant = prepare(tmp_path)
    remote = Remote()
    root = restarted_executor()
    remote.snapshots["child"] = root
    if condition == "original_missing":
        root["recentRuns"] = root["recentRuns"][:1]
    elif condition == "latest_missing":
        root["recentRuns"] = root["recentRuns"][1:]
    elif condition == "original_contradiction":
        root["recentRuns"].append({"runId": "run", "status": "completed"})
    elif condition == "latest_contradiction":
        root["recentRuns"].append({"runId": "later-run", "status": "failed"})
    elif condition == "latest_active":
        root["thread"]["activeRunId"] = "later-run"
    elif condition == "root_pending":
        root["thread"]["pendingRequestCount"] = 1
    elif condition == "child_pending":
        root["hasPendingChildRuns"] = True
    elif condition.startswith("descendant_"):
        descendant = snapshot("grandchild", "descendant-run", "completed", "child")
        remote.snapshots["grandchild"] = descendant
        if condition == "descendant_active":
            descendant["thread"]["activeRunId"] = "descendant-run"
        elif condition == "descendant_pending":
            descendant["hasPendingChildRuns"] = True
        else:
            descendant["thread"]["threadId"] = "wrong-thread"
    elif condition == "root_wrong_identity":
        root["thread"]["threadId"] = "wrong-thread"
    elif condition in {"original_completed", "original_failed"}:
        root["recentRuns"][1]["status"] = condition.removeprefix("original_")
    elif condition == "latest_identity_unknown":
        root["thread"]["latestRunId"] = "unknown"
    elif condition == "original_id_unknown":
        root["recentRuns"][1]["runId"] = "unknown"
    state.set_meta(
        f"execution_report:{grant['id']}",
        {
            "run_id": "run",
            "generation": grant["generation"],
            "status": "succeeded",
            "evidence": {"checks": "passed"},
        },
    )
    result = await Reconciler(state, remote).reconcile()
    assert result["state"] in {"running", "uncertain"}
    assert state.lease()["attempt_id"] == grant["id"]
    assert state.get_attempt(grant["id"])["status"] != "succeeded"
    state.close()


async def test_cancel_waits_for_descendant_and_rechecks_current_state(tmp_path):
    state, objective, grant = prepare(tmp_path)
    remote = Remote()
    remote.snapshots["child"] = restarted_executor()
    remote.snapshots["grandchild"] = snapshot(
        "grandchild", "nested-run", parent="child"
    )
    original_read = remote.read_thread

    async def read_and_cancel(thread_id, **kwargs):
        state.cancel_objective(objective["id"])
        return await original_read(thread_id, **kwargs)

    remote.read_thread = read_and_cancel
    reconciler = Reconciler(state, remote)
    assert (await reconciler.reconcile())["state"] == "cancel_requested"
    assert remote.interruptions == [("grandchild", "nested-run")]
    assert state.lease()["attempt_id"] == grant["id"]
    remote.snapshots["grandchild"] = snapshot(
        "grandchild", "nested-run", "cancelled", "child"
    )
    assert await reconciler.reconcile() == {"state": "stopped", "released": True}
    state.close()


async def test_historical_stop_does_not_schedule_memory(running_service):
    state = running_service.state
    objective = state.submit_objective("alpha", "Interrupted fixture")
    job = state.enqueue_job(objective["id"], "implementation", {})
    grant = state.acquire_grant(job["id"], "codex", "m", "parent", "parent-run")
    state.claim_grant(grant["id"], grant["generation"], "child", "run")
    remote = Remote()
    remote.snapshots["child"] = restarted_executor()
    remote.snapshots["child"]["thread"]["projectId"] = "alpha"
    assert await Reconciler(state, remote).reconcile() == {
        "state": "stopped",
        "released": True,
    }
    for _ in range(3):
        running_service.after_execution()
    assert not [job for job in state.list_jobs() if job["kind"] == "memory"]
    assert state.lease() is None


async def test_recovery_does_not_act_after_lease_changes_during_read(tmp_path):
    state, _, grant = prepare(tmp_path)
    remote = Remote()
    remote.snapshots["child"] = restarted_executor()
    original_read = remote.read_thread

    async def read_and_release(thread_id, **kwargs):
        assert state.record_execution(
            grant["id"],
            grant["generation"],
            "run",
            "stopped",
            pending_children=False,
            tree_confirmed=True,
        )
        return await original_read(thread_id, **kwargs)

    remote.read_thread = read_and_release
    assert await Reconciler(state, remote).reconcile() == {
        "state": "uncertain",
        "reason": "lease_changed",
    }
    assert remote.interruptions == []
    assert state.lease() is None
    state.close()
