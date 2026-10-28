"""Stopped original run + completed continuation; uncertain native launch.

Real disposable daemon/Git/SQLite/memory/admission; simulated caller-bound T3.
The host fixture reports task_status.status for the delegated task (latest
run), as observed live for a cancelled original followed by a completed run.
"""

import copy

import pytest
from test_managed_tasks import report, start
from test_managed_tasks import system as system_fixture
from test_report_prevalidation import flow as flow_fixture

from oraculo.protocol import content_hash

flow = flow_fixture
system = system_fixture


def attach(flow, task, statuses):
    """Same child thread: original run plus continuation(s); last one running."""
    child = task["child"]["childThreadId"]
    original = task["child"]["childRunId"]
    runs = [
        dict(
            runId=original if n == 1 else f"{original}-continuation-{n}",
            ordinal=n,
            status=status,
            startedAt=f"2026-10-21T18:0{n}:00Z",
            completedAt=f"2026-10-21T18:0{n}:30Z",
        )
        for n, status in enumerate(statuses, 1)
    ]
    runs[-1].update(status="running", completedAt=None)
    thread = flow.host.threads[child]
    thread.update(
        status="running",
        activeRunId=runs[-1]["runId"],
        latestRunId=runs[-1]["runId"],
        runCount=len(runs),
    )
    native = flow.host.native_status[task["child"]["taskId"]]
    native.update(status="running", workState="working", hasPendingChildRuns=True)
    call = flow.host.call

    async def observed(name, args):
        result = await call(name, args)
        if name == "t3_thread_read" and args["threadId"] == child:
            result["recentRuns"] = copy.deepcopy(runs)
        return result

    flow.host.call = observed
    return runs, thread, native


def complete(flow, task, runs, status="completed"):
    flow.host.complete(task["child"]["childThreadId"], status=status)
    runs[-1].update(status=status, completedAt=f"2026-10-21T18:0{len(runs)}:30Z")


async def submit_from_child(flow, task, good):
    flow.host.caller = task["child"]["childThreadId"]
    submitted = await flow.invoke("finish", task_id=task["id"], report=good)
    flow.host.caller = "origin"
    assert submitted["phase"] == "validation_submitted", submitted
    return submitted


def assert_refused(flow, task, result, reason=None):
    assert result["phase"] == "blocked", result
    if reason:
        assert result["blocked_reason"] == reason, result
    assert result["work_status"] != "succeeded"
    assert result["memory"]["write_state"] == "not_requested"
    assert flow.service.state.capacity()["occupied"] == 1
    assert result["child"] == task["child"]


@pytest.mark.parametrize(
    "statuses",
    [
        ["cancelled", "completed"],
        ["interrupted", "completed"],
        ["cancelled", "interrupted", "completed"],
    ],
)
async def test_stopped_original_with_completed_continuation_succeeds(flow, statuses):
    task = await flow.begin()
    runs, _, native = attach(flow, task, statuses)
    good = report(task)
    await submit_from_child(flow, task, good)
    complete(flow, task, runs)
    # Observed live: status follows the task, childRunId stays the original.
    assert native["status"] == "completed"
    assert native["childRunId"] == task["child"]["childRunId"]
    result = await flow.invoke("resume", task_id=task["id"])
    assert result["phase"] == "pr_pending", result
    assert result["work_status"] == "succeeded"
    assert result["memory"]["write_receipt"]["state"] == "confirmed"
    assert flow.service.state.capacity()["occupied"] == 0
    stored = flow.service.managed_service.store.get(task["id"])
    assert stored["child"] == task["child"]
    evidence = stored["completion_evidence"]
    assert evidence["original_run_id"] == task["child"]["childRunId"]
    assert evidence["runs"][0]["status"] == statuses[0]
    assert evidence["latest_run_id"] == runs[-1]["runId"]
    continuation = evidence["continuation"]
    assert continuation["basis"] == "stopped_original_completed_continuation"
    assert continuation["original_status"] == statuses[0]
    assert continuation["latest_run_id"] == runs[-1]["runId"]
    assert continuation["submission_run_id"] == runs[-1]["runId"]
    assert continuation["task_status"]["latestTerminalRunId"] == runs[-1]["runId"]
    assert continuation["task_status"]["childRunId"] == task["child"]["childRunId"]
    receipt = result["memory"]["write_receipt"]
    again = await flow.invoke("resume", task_id=task["id"])
    assert again["memory"]["write_receipt"] == receipt
    assert len(flow.host.created) == 1


@pytest.mark.parametrize(
    "statuses",
    [
        ["failed", "completed"],
        ["cancelled", "failed", "completed"],
        ["rolled_back", "completed"],
    ],
)
async def test_failed_run_in_chain_never_becomes_success(flow, statuses):
    task = await flow.begin()
    runs, _, _ = attach(flow, task, statuses)
    await submit_from_child(flow, task, report(task))
    complete(flow, task, runs)
    result = await flow.invoke("resume", task_id=task["id"])
    assert_refused(flow, task, result)


async def test_continuation_still_running_is_refused(flow):
    task = await flow.begin()
    attach(flow, task, ["cancelled", "completed"])
    await submit_from_child(flow, task, report(task))
    result = await flow.invoke("resume", task_id=task["id"])
    assert_refused(flow, task, result)


async def test_report_bound_to_original_run_is_refused(flow):
    task = await flow.begin()
    good = report(task)
    # Submitted while the original run was the active run of the child.
    await submit_from_child(flow, task, good)
    runs, _, _ = attach(flow, task, ["cancelled", "completed"])
    complete(flow, task, runs)
    result = await flow.invoke("resume", task_id=task["id"])
    assert_refused(flow, task, result, "latest_run_report_unproven")


@pytest.mark.parametrize("author", ["legacy_unbound", "origin"])
async def test_only_bound_executor_submission_certifies_after_stop(flow, author):
    task = await flow.begin()
    runs, _, _ = attach(flow, task, ["cancelled", "completed"])
    good = report(task)
    complete(flow, task, runs)
    if author == "legacy_unbound":
        flow.service.managed_service.store.update(
            task["id"],
            "validation_submitted",
            {"report_sha256": content_hash(good)},
            submitted_report=good,
        )
        result = await flow.invoke("resume", task_id=task["id"])
    else:
        result = await flow.invoke("finish", task_id=task["id"], report=good)
    assert_refused(flow, task, result, "latest_run_report_unproven")


@pytest.mark.parametrize(
    "divergence",
    [
        "status_is_original",
        "latest_terminal_run",
        "latest_terminal_status",
        "pending_question",
        "pending_child_runs",
        "child_run_rebound",
    ],
)
async def test_divergent_task_status_is_refused(flow, divergence):
    task = await flow.begin()
    runs, _, native = attach(flow, task, ["cancelled", "completed"])
    await submit_from_child(flow, task, report(task))
    complete(flow, task, runs)
    if divergence == "status_is_original":
        native["status"] = "cancelled"
    elif divergence == "latest_terminal_run":
        native["latestTerminalRunId"] = runs[0]["runId"]
    elif divergence == "latest_terminal_status":
        native["latestTerminalStatus"] = "cancelled"
    elif divergence == "pending_question":
        native["hasPendingQuestion"] = True
    elif divergence == "pending_child_runs":
        native["hasPendingChildRuns"] = True
    else:
        native["childRunId"] = runs[-1]["runId"]
    result = await flow.invoke("resume", task_id=task["id"])
    assert_refused(flow, task, result)


# --- uncertain native launch without a child ------------------------------


def lose_delegate_before_host(system):
    """The origin harness never executed delegate_task: no child, no ACK."""
    call = system.host.call
    state = {"armed": True}

    async def blocked(name, args):
        if name == "delegate_task" and state["armed"]:
            state["armed"] = False
            system.host.calls.append((name, copy.deepcopy(args)))
            raise TimeoutError("simulated harness block before host execution")
        return await call(name, args)

    system.host.call = blocked


async def started_without_child(system, **overrides):
    system.engine.admission = system.service.state
    first = await start(system, target={"providerInstanceId": "codex"}, **overrides)
    assert first["phase"] == "blocked" and first["launch_attempted"] is True
    assert not first.get("child")
    return first


def delegations(system):
    return [a for n, a in system.host.calls if n == "delegate_task"]


def reconciliation(task):
    return [
        e["proof"]
        for e in task["events"]
        if "launch_reconciliation" in e.get("proof", {})
    ]


async def test_resume_reissues_same_delegate_when_catalog_has_no_child(system):
    lose_delegate_before_host(system)
    first = await started_without_child(system)
    assert system.host.created == []
    assert system.service.state.capacity()["occupied"] == 1
    resumed = await system.engine.command("resume", {"task_id": first["id"]})
    assert resumed["phase"] == "executing", resumed
    calls = delegations(system)
    assert len(calls) == 2 and calls[0] == calls[1]
    assert calls[1]["clientRequestId"] == first["id"] + ":delegate"
    assert len(system.host.created) == 1
    assert resumed["child"]["childThreadId"] == system.host.created[0]
    proof = reconciliation(system.store.get(first["id"]))[-1]
    assert proof["launch_reconciliation"] == "reissue_same_key_no_candidate"
    assert proof["candidates"] == []
    assert proof["intent_sha256"] == content_hash(calls[0])
    assert system.service.state.capacity()["occupied"] == 1


async def test_resume_binds_existing_candidate_through_idempotent_replay(system):
    system.host.fail_delegate_once = True
    first = await started_without_child(system)
    assert len(system.host.created) == 1
    resumed = await system.engine.command("resume", {"task_id": first["id"]})
    assert resumed["phase"] == "executing", resumed
    assert resumed["child"]["childThreadId"] == system.host.created[0]
    assert len(system.host.created) == 1
    proof = reconciliation(system.store.get(first["id"]))[-1]
    assert proof["launch_reconciliation"] == "replay_same_key_expect_candidate"
    assert proof["candidates"] == [system.host.created[0]]


async def test_receipt_contradicting_candidate_is_never_bound(system):
    system.host.fail_delegate_once = True
    first = await started_without_child(system)
    # Broken idempotency would allocate a second child for the same key.
    system.host.delegations.clear()
    resumed = await system.engine.command("resume", {"task_id": first["id"]})
    assert resumed["blocked_reason"] == "launch_reconciliation_receipt_mismatch"
    assert not resumed.get("child")
    assert system.service.state.capacity()["occupied"] == 1


async def test_several_candidates_block_without_host_mutation(system):
    lose_delegate_before_host(system)
    first = await started_without_child(system)
    for n in (1, 2):
        system.host.threads[f"lookalike-{n}"] = dict(
            system.host.threads["origin"],
            threadId=f"lookalike-{n}",
            parentThreadId="origin",
            title=system.store.get(first["id"])["launch_arguments"]["title"],
            activeRunId=None,
            status="completed",
        )
    resumed = await system.engine.command("resume", {"task_id": first["id"]})
    assert resumed["blocked_reason"] == "launch_uncertain_multiple_candidates"
    assert len(delegations(system)) == 1
    assert system.host.created == []
    assert system.service.state.capacity()["occupied"] == 1


async def test_child_bound_to_another_task_is_not_a_candidate(system):
    system.service.config.max_executors = 2
    system.service.state.max_executors = 2
    system.engine.admission = system.service.state
    bound = await start(system, target={"providerInstanceId": "codex"})
    assert bound["child"]
    lose_delegate_before_host(system)
    second = await started_without_child(system, key="same-title-second")
    stored = [system.store.get(t["id"])["launch_arguments"] for t in (second, bound)]
    assert stored[0]["title"] == stored[1]["title"]
    resumed = await system.engine.command("resume", {"task_id": second["id"]})
    assert resumed["phase"] == "executing", resumed
    assert resumed["child"]["childThreadId"] != bound["child"]["childThreadId"]
    proof = reconciliation(system.store.get(second["id"]))[-1]
    assert proof["candidates"] == [] and proof["excluded_bound"] == 1
    assert len(system.host.created) == 2
