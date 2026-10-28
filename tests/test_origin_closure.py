"""Origin stays active while independent siblings execute in real local services."""

import copy
from collections import Counter

import pytest
from test_managed_tasks import report
from test_report_prevalidation import flow as flow_fixture

flow = flow_fixture


async def sibling(flow):
    flow.service.state.max_executors = 3
    result = await flow.invoke(
        "start",
        kind="work",
        key="parallel",
        text="Independent parallel work",
        cwd=str(flow.service.config.projects_root / "alpha"),
    )
    assert result["phase"] == "executing", result
    return result


@pytest.mark.parametrize("independent", [False, True])
async def test_origin_finish_with_live_sibling_has_two_fresh_gates(flow, independent):
    task = await flow.begin(independent=independent)
    other = await sibling(flow)
    flow.host.complete(task["child"]["childThreadId"])
    offset = len(flow.host.calls)
    result = await flow.invoke("finish", task_id=task["id"], report=report(task))
    assert result["phase"] == "pr_pending", result
    assert result["work_status"] == "succeeded"
    assert flow.service.state.capacity()["occupied"] == 1
    assert flow.host.threads["origin"]["status"] == "running"
    assert flow.host.threads[other["child"]["childThreadId"]]["status"] == "running"
    calls = flow.host.calls[offset:]
    counts = Counter(name for name, _ in calls)
    # One proof before memory, another afterwards; no polling or third proof.
    assert counts["t3_thread_list"] == 2, counts
    assert counts["task_status"] == (0 if independent else 2), counts
    assert len(calls) <= 13, counts
    assert not any(
        name == "t3_thread_read" and args["threadId"] == other["child"]["childThreadId"]
        for name, args in calls
    )
    offset = len(flow.host.calls)
    snapshot = flow.snapshot(task)
    for _ in range(3):
        again = await flow.invoke("resume", task_id=task["id"])
        assert again["phase"] == "pr_pending", again
    assert flow.snapshot(task) == snapshot
    assert not any(
        n in {"t3_thread_list", "task_status"} for n, _ in flow.host.calls[offset:]
    )


def unstarted_cancellation(flow, task):
    child = task["child"]["childThreadId"]
    flow.host.complete(child)
    thread = flow.host.threads[child]
    thread.update(latestRunId="cancelled-before-start", status="cancelled", runCount=2)
    native = flow.host.native_status[task["child"]["taskId"]]
    native.update(
        status="completed",
        latestTerminalRunId=task["child"]["childRunId"],
        latestTerminalStatus="completed",
    )
    runs = [
        dict(
            runId=task["child"]["childRunId"],
            ordinal=1,
            status="completed",
            startedAt="2026-01-01T18:26:00Z",
            completedAt="2026-01-01T21:05:00Z",
        ),
        dict(
            runId=thread["latestRunId"],
            ordinal=2,
            status="cancelled",
            startedAt=None,
            requestedAt="2026-01-01T20:47:00Z",
            completedAt="2026-01-01T20:47:01Z",
        ),
    ]
    call = flow.host.call

    async def observed(name, args):
        result = await call(name, args)
        if name == "t3_thread_read" and args["threadId"] == child:
            result["recentRuns"] = copy.deepcopy(runs)
        return result

    flow.host.call = observed
    return runs


async def test_cancelled_request_never_started_does_not_erase_completed_execution(flow):
    task = await flow.begin()
    other = await sibling(flow)
    runs = unstarted_cancellation(flow, task)
    result = await flow.invoke("finish", task_id=task["id"], report=report(task))
    assert result["phase"] == "pr_pending", result
    stored = flow.service.managed_service.store.get(task["id"])
    assert stored["child"] == task["child"]
    assert stored["completion_evidence"]["runs"] == runs
    assert stored["completion_evidence"]["latest_run_id"] == runs[-1]["runId"]
    assert stored["completion_evidence"]["execution_run_id"] == runs[0]["runId"]
    assert flow.host.threads[other["child"]["childThreadId"]]["status"] == "running"


async def test_native_node_keeps_original_status_when_unstarted_cancel_finishes_later(
    flow,
):
    task = await flow.begin()
    runs = unstarted_cancellation(flow, task)
    runs[-1].update(
        requestedAt="2026-01-01T21:06:00Z", completedAt="2026-01-01T21:06:01Z"
    )
    flow.host.native_status[task["child"]["taskId"]].update(
        latestTerminalRunId=runs[-1]["runId"],
        latestTerminalStatus="cancelled",
    )
    result = await flow.invoke("finish", task_id=task["id"], report=report(task))
    assert result["phase"] == "pr_pending", result
    assert result["work_status"] == "succeeded"
    assert result["memory"]["write_receipt"]["state"] == "confirmed"
    assert flow.service.state.capacity()["occupied"] == 0


@pytest.mark.parametrize(
    "defect",
    ["started", "missing_requested", "missing_started", "failed", "future_end"],
)
async def test_unstarted_cancel_requires_explicit_complete_evidence(flow, defect):
    task = await flow.begin()
    runs = unstarted_cancellation(flow, task)
    if defect == "started":
        runs[-1]["startedAt"] = "2026-01-01T21:06:00Z"
        runs[-1]["completedAt"] = "2026-01-01T21:07:00Z"
        flow.host.native_status[task["child"]["taskId"]].update(
            status="cancelled",
            latestTerminalRunId=runs[-1]["runId"],
            latestTerminalStatus="cancelled",
        )
    elif defect == "missing_requested":
        runs[-1].pop("requestedAt")
    elif defect == "missing_started":
        runs[-1].pop("startedAt")
    elif defect == "failed":
        runs[-1]["status"] = "failed"
    else:
        runs[-1]["completedAt"] = "2099-01-01T21:07:00Z"
    result = await flow.invoke("finish", task_id=task["id"], report=report(task))
    assert result["phase"] == "blocked", result
    assert result["work_status"] != "succeeded"
    if defect != "started":
        assert result["memory"]["write_state"] == "not_requested"
    assert flow.service.state.capacity()["occupied"] == (
        0 if defect == "started" else 1
    )


async def test_only_cancelled_request_records_stopped_without_inventing_execution(flow):
    task = await flow.begin()
    child = task["child"]["childThreadId"]
    flow.host.complete(child, status="cancelled")
    call = flow.host.call

    async def observed(name, args):
        value = await call(name, args)
        if name == "t3_thread_read" and args["threadId"] == child:
            value["recentRuns"][0]["startedAt"] = None
        return value

    flow.host.call = observed
    result = await flow.invoke("finish", task_id=task["id"], report=report(task))
    assert result["work_status"] == "stopped", result
    assert result["memory"]["write_state"] == "saved"
    assert flow.service.state.capacity()["occupied"] == 0
    stored = flow.service.managed_service.store.get(task["id"])
    assert stored["completion_evidence"]["execution_run_id"] is None
