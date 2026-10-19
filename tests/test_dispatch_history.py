import pytest
from test_reconcile import snapshot
from test_service import DispatchLifecycleRemote

from oraculo.t3.contracts import dispatch_retry_safe, execution_terminal


def idle_after_dispatch():
    current = snapshot("coordinator", "later-run", "completed")
    current["recentRuns"].append({"runId": "dispatch-run", "status": "completed"})
    return current


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled", "interrupted"])
def test_terminal_dispatch_history_allows_retry_without_completing_old_work(status):
    current = idle_after_dispatch()
    current["recentRuns"][1]["status"] = status
    assert dispatch_retry_safe(current, "dispatch-run")
    assert not execution_terminal(current, "dispatch-run")


@pytest.mark.parametrize(
    "condition",
    [
        "missing_dispatch",
        "unfinished_dispatch",
        "missing_latest",
        "unknown_latest",
        "unfinished_latest",
        "active",
        "nonterminal_thread",
        "pending_request",
        "pending_children",
        "conflicting_dispatch",
        "conflicting_latest",
    ],
)
def test_history_retry_requires_both_terminal_proofs_and_idle_coordinator(condition):
    current = idle_after_dispatch()
    thread = current["thread"]
    if condition == "missing_dispatch":
        current["recentRuns"].pop()
    elif condition == "unfinished_dispatch":
        current["recentRuns"][1]["status"] = "running"
    elif condition == "missing_latest":
        thread["latestRunId"] = None
    elif condition == "unknown_latest":
        thread["latestRunId"] = "unobserved-run"
    elif condition == "unfinished_latest":
        current["recentRuns"][0]["status"] = "running"
    elif condition == "active":
        thread["activeRunId"] = "active-run"
    elif condition == "nonterminal_thread":
        thread["status"] = "running"
    elif condition == "pending_request":
        thread["pendingRequestCount"] = 1
    elif condition == "pending_children":
        current["hasPendingChildRuns"] = True
    elif condition.startswith("conflicting_"):
        run_id = "dispatch-run" if condition == "conflicting_dispatch" else "later-run"
        current["recentRuns"].append({"runId": run_id, "status": "running"})
    assert not dispatch_retry_safe(current, "dispatch-run")


async def test_dispatch_history_from_another_thread_does_not_authorize_retry(
    running_service,
):
    service = running_service
    objective = service.state.submit_objective("alpha", "Queued work")
    service.state.enqueue_job(objective["id"], "implementation", {})

    class WrongThread(DispatchLifecycleRemote):
        async def read_thread(self, thread):
            current = snapshot("another-coordinator", "later-run", "completed")
            current["recentRuns"].append(
                {"runId": "dispatch-run-1", "status": "completed"}
            )
            return current

    remote = WrongThread()
    await service.dispatch(remote)
    await service.dispatch(remote)
    assert len(remote.calls) == 1
    assert service.health == "awaiting_coordinator"
    assert (
        service.state.connection.execute("SELECT count(*) FROM outbox").fetchone()[0]
        == 1
    )
