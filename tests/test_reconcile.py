from oraculo.operations import Operations
from oraculo.reconcile import Reconciler
from oraculo.state import State


def snapshot(thread, run, status="running", parent=None):
    return {
        "thread": {
            "threadId": thread,
            "activeRunId": run if status == "running" else None,
            "latestRunId": run,
            "status": status,
            "pendingRequestCount": 0,
            "branch": "work",
            "projectId": "p",
        },
        "recentRuns": [{"runId": run, "status": status}],
        "items": [],
        "hasMore": False,
        "nextPosition": None,
        "parentThreadId": parent,
    }


class Remote:
    def __init__(self):
        self.snapshots = {"child": snapshot("child", "run")}
        self.interruptions = []
        self.disconnected = False

    async def list_threads(self, project_id, *, title_contains=None):
        if self.disconnected:
            raise ConnectionError("offline")
        return [
            dict(
                threadId=key,
                parentThreadId=value.get("parentThreadId"),
                title="launch-marker",
            )
            for key, value in self.snapshots.items()
        ]

    async def read_thread(self, thread_id, **kwargs):
        return self.snapshots[thread_id]

    async def interrupt(self, thread_id, run_id, client_request_id, reason=None):
        self.interruptions.append((thread_id, run_id))
        return {"accepted": True}


def prepare(tmp_path):
    state = State(tmp_path / "db")
    state.register_project("p", "/tmp/p")
    objective = state.submit_objective("p", "work")
    job = state.enqueue_job(objective["id"], "implementation", {})
    grant = state.acquire_grant(job["id"], "codex", "m", "parent", "parentrun")
    state.claim_grant(grant["id"], grant["generation"], "child", "run")
    return state, objective, grant


async def test_restart_with_live_child(tmp_path):
    state, _, grant = prepare(tmp_path)
    state.close()
    state = State(tmp_path / "db")
    remote = Remote()
    result = await Reconciler(state, remote).reconcile()
    assert result["state"] == "running"
    assert state.lease()["attempt_id"] == grant["id"]
    assert state.connection.execute("SELECT count(*) FROM attempts").fetchone()[0] == 1


async def test_launch_lost_response(tmp_path):
    state, _, grant = prepare(tmp_path)
    operations = Operations(state)
    expected = {"project_id": "p", "branch": "work", "marker": "launch-marker"}
    operations.prepare(
        "launch", grant["id"], "launch", expected, generation=grant["generation"]
    )
    operations.mark_dispatched("launch")
    operations.mark_uncertain("launch")
    remote = Remote()
    remote.snapshots["child"]["items"] = [{"text": "launch-marker"}]
    match = await Reconciler(state, remote).reconcile_launch("launch")
    assert match["threadId"] == "child"
    assert operations.get("launch")["state"] == "confirmed"
    remote.snapshots["duplicate"] = snapshot("duplicate", "other")
    remote.snapshots["duplicate"]["items"] = [{"text": "launch-marker"}]
    operations.prepare(
        "launch2", grant["id"], "launch", expected, generation=grant["generation"]
    )
    assert await Reconciler(state, remote).reconcile_launch("launch2") is None
    assert operations.get("launch2")["state"] == "uncertain"


async def test_cancel_survives_restart_and_pending_children(tmp_path):
    state, objective, grant = prepare(tmp_path)
    state.cancel_objective(objective["id"])
    state.close()
    state = State(tmp_path / "db")
    remote = Remote()
    remote.snapshots["child"] = snapshot("child", "run", "completed")
    remote.snapshots["grandchild"] = snapshot("grandchild", "grandrun", parent="child")
    reconciler = Reconciler(state, remote)
    assert (await reconciler.reconcile())["state"] == "cancel_requested"
    assert remote.interruptions == [("grandchild", "grandrun")]
    assert state.lease()
    remote.snapshots["grandchild"] = snapshot(
        "grandchild", "grandrun", "interrupted", parent="child"
    )
    assert (await reconciler.reconcile())["state"] == "stopped"
    assert state.lease() is None


async def test_disconnect_and_reconnect(tmp_path):
    state, _, grant = prepare(tmp_path)
    remote = Remote()
    reconciler = Reconciler(state, remote)
    remote.disconnected = True
    assert (await reconciler.reconcile())["state"] == "uncertain"
    assert state.lease()
    remote.disconnected = False
    assert (await reconciler.reconcile())["state"] == "running"
    remote.snapshots["child"] = snapshot("child", "run", "completed")
    assert (await reconciler.reconcile())["reason"] == "missing_execution_report"
    state.set_meta(
        f"execution_report:{grant['id']}",
        {
            "run_id": "run",
            "generation": grant["generation"],
            "status": "succeeded",
            "evidence": {"tests": "passed"},
        },
    )
    assert (await reconciler.reconcile())["released"] is True


async def test_completed_run_with_stopped_report_waits_for_children(tmp_path):
    state, _, grant = prepare(tmp_path)
    remote = Remote()
    remote.snapshots["child"] = snapshot("child", "run", "completed")
    remote.snapshots["grandchild"] = snapshot("grandchild", "grandrun", parent="child")
    state.set_meta(
        f"execution_report:{grant['id']}",
        {
            "run_id": "run",
            "generation": grant["generation"],
            "status": "stopped",
            "evidence": {"reason": "Executor stopped work on interruption request"},
        },
    )
    reconciler = Reconciler(state, remote)
    assert (await reconciler.reconcile())["state"] == "running"
    assert state.lease()
    remote.snapshots["grandchild"] = snapshot(
        "grandchild", "grandrun", "interrupted", parent="child"
    )
    assert await reconciler.reconcile() == {"state": "stopped", "released": True}
    assert state.lease() is None
    assert state.get_attempt(grant["id"])["status"] == "stopped"


async def test_cancel_before_claim_binds_identity_without_authorizing_effects(tmp_path):
    import pytest

    from oraculo.state import StaleGrant

    state = State(tmp_path / "db")
    state.register_project("p", "/tmp/p")
    objective = state.submit_objective("p", "work")
    job = state.enqueue_job(objective["id"], "implementation", {})
    grant = state.acquire_grant(job["id"], "codex", "m", "parent", "parentrun")
    state.cancel_objective(objective["id"])
    state.bind_cancelled_execution(grant["id"], grant["generation"], "child", "run")
    with pytest.raises(StaleGrant):
        state.validate_grant(grant["id"], grant["generation"])
    with pytest.raises(StaleGrant):
        state.bind_cancelled_execution(
            grant["id"], grant["generation"], "other", "other-run"
        )
    remote = Remote()
    assert (await Reconciler(state, remote).reconcile())["state"] == "cancel_requested"
    assert remote.interruptions == [("child", "run")]
    assert state.lease()
    remote.snapshots["child"] = snapshot("child", "run", "interrupted")
    assert (await Reconciler(state, remote).reconcile())["state"] == "stopped"
    assert state.lease() is None


async def test_launch_rejects_other_branch_in_thread_snapshot(tmp_path):
    state, _, grant = prepare(tmp_path)
    operations = Operations(state)
    expected = {"project_id": "p", "branch": "work", "marker": "launch-marker"}
    operations.prepare(
        "launch", grant["id"], "launch", expected, generation=grant["generation"]
    )
    remote = Remote()
    remote.snapshots["child"]["items"] = [{"text": "launch-marker"}]
    remote.snapshots["child"]["thread"]["branch"] = "unrelated"
    assert await Reconciler(state, remote).reconcile_launch("launch") is None
    assert operations.get("launch")["state"] == "uncertain"
