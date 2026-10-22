"""Three-turn continuity with real disposable daemon/Git/memory/admission."""

import copy
from datetime import datetime, timezone

import pytest
from test_managed_tasks import report
from test_report_prevalidation import flow as flow_fixture

from oraculo.protocol import content_hash
from oraculo.run_continuity import terminal_chain
from oraculo.state import StateError

flow = flow_fixture


def attach_chain(flow, task, *, active=False):
    child = task["child"]["childThreadId"]
    original = task["child"]["childRunId"]
    runs = [
        dict(
            runId=original if n == 1 else f"{original}-followup-{n}",
            ordinal=n,
            status="completed",
            startedAt=f"2026-10-21T18:0{n}:00Z",
            completedAt=f"2026-10-21T18:0{n}:30Z",
        )
        for n in range(1, 4)
    ]
    flow.host.complete(child)
    thread = flow.host.threads[child]
    thread.update(latestRunId=runs[-1]["runId"], runCount=3)
    native = flow.host.native_status[task["child"]["taskId"]]
    native.update(
        latestTerminalRunId=runs[-1]["runId"], latestTerminalStatus="completed"
    )
    call = flow.host.call

    async def observed(name, args):
        result = await call(name, args)
        if name == "t3_thread_read" and args["threadId"] == child:
            result["recentRuns"] = copy.deepcopy(runs)
        return result

    flow.host.call = observed
    if active:
        thread.update(activeRunId=runs[-1]["runId"], status="running")
        runs[-1].update(status="running", completedAt=None)
        native.update(hasPendingChildRuns=True, latestTerminalRunId=runs[-2]["runId"])
    return runs, thread, native


async def submit_last(flow, task, *, legacy=False):
    runs, thread, native = attach_chain(flow, task, active=True)
    good = report(task)
    flow.host.caller = task["child"]["childThreadId"]
    if legacy:
        flow.service.managed_service.store.update(
            task["id"],
            "validation_submitted",
            {"report_sha256": content_hash(good)},
            submitted_report=good,
        )
    else:
        submitted = await flow.invoke("finish", task_id=task["id"], report=good)
        assert submitted["phase"] == "validation_submitted", submitted
    flow.host.complete(task["child"]["childThreadId"])
    runs[-1].update(status="completed", completedAt="2026-10-21T18:03:30Z")
    flow.host.caller = "origin"
    return good, runs, thread, native


@pytest.mark.parametrize("legacy", [False, True])
async def test_three_terminal_runs_preserve_original_and_confirm_memory(flow, legacy):
    task = await flow.begin()
    good, runs, _, _ = await submit_last(flow, task, legacy=legacy)
    result = await flow.invoke("resume", task_id=task["id"])
    assert result["phase"] == "pr_pending", result
    assert result["work_status"] == "succeeded"
    assert result["memory"]["write_receipt"]["state"] == "confirmed"
    assert flow.service.state.capacity()["occupied"] == 0
    stored = flow.service.managed_service.store.get(task["id"])
    assert stored["child"] == task["child"]
    assert stored["completion_evidence"]["runs"] == runs
    assert stored["completion_evidence"]["latest_run_id"] == runs[-1]["runId"]
    assert stored["completion_evidence"]["legacy_submission"] is legacy
    assert stored["report"] == good
    receipt = result["memory"]["write_receipt"]
    again = await flow.invoke("resume", task_id=task["id"])
    assert again["memory"]["write_receipt"] == receipt
    assert len(flow.host.created) == 1


@pytest.mark.parametrize(
    "defect",
    [
        "active",
        "nonterminal",
        "missing",
        "truncated",
        "duplicate",
        "old_report",
        "wrong_native",
        "wrong_latest",
        "parent",
        "descendant",
    ],
)
async def test_uncertain_continuations_keep_reservation_and_memory_pristine(
    flow, defect
):
    task = await flow.begin()
    good, runs, thread, native = await submit_last(flow, task)
    if defect == "active":
        thread["activeRunId"] = "new-run"
        native["hasPendingChildRuns"] = True
    elif defect == "nonterminal":
        runs[1]["status"] = "running"
    elif defect == "missing":
        runs.pop(1)
    elif defect == "truncated":
        thread["runCount"] = 4
    elif defect == "duplicate":
        runs[1]["runId"] = runs[0]["runId"]
    elif defect == "old_report":
        # Do not edit evidence: build the later run AFTER the stored submission.
        future = datetime.fromtimestamp(
            datetime.now(timezone.utc).timestamp() + 60, timezone.utc
        ).isoformat()
        runs[-1].update(startedAt=future, completedAt=future)
    elif defect == "wrong_native":
        native["childRunId"] = "unrelated"
    elif defect == "wrong_latest":
        native["latestTerminalRunId"] = runs[1]["runId"]
    elif defect == "parent":
        thread["parentThreadId"] = "unrelated"
    else:
        flow.host.threads["descendant"] = dict(
            thread,
            threadId="descendant",
            parentThreadId=task["child"]["childThreadId"],
            activeRunId="descendant-run",
            latestRunId="descendant-run",
            status="running",
        )
    result = await flow.invoke("resume", task_id=task["id"])
    assert result["phase"] == "blocked", result
    assert result["memory"]["write_state"] == "not_requested"
    assert flow.service.state.capacity()["occupied"] == 1
    assert result["child"] == task["child"]


@pytest.mark.parametrize(
    "change", ["active", "terminal_fourth", "descendant", "dirty", "identity"]
)
async def test_revalidate_after_memory_and_replay_without_rewriting_memory(
    flow, change
):
    task = await flow.begin()
    good, runs, thread, native = await submit_last(flow, task)
    facade = flow.service.managed_service
    memory_factory = facade.memory
    old_thread = copy.deepcopy(thread)
    old_runs = copy.deepcopy(runs)
    old_native = copy.deepcopy(native)
    extra = None

    def memory():
        adapter = memory_factory()
        finish = adapter.finish

        async def changed(*args, **kwargs):
            nonlocal extra
            cycle = await finish(*args, **kwargs)
            if change == "active":
                thread["activeRunId"] = "fourth"
                native["hasPendingChildRuns"] = True
            elif change == "terminal_fourth":
                runs.append(
                    dict(
                        runId="fourth",
                        ordinal=4,
                        status="completed",
                        startedAt="2026-10-21T18:04:00Z",
                        completedAt="2026-10-21T18:04:30Z",
                    )
                )
                thread.update(latestRunId="fourth", runCount=4)
                native.update(latestTerminalRunId="fourth")
            elif change == "descendant":
                flow.host.threads["descendant"] = dict(
                    thread,
                    threadId="descendant",
                    parentThreadId=task["child"]["childThreadId"],
                    status="running",
                    activeRunId="d-run",
                    latestRunId="d-run",
                )
            elif change == "dirty":
                from pathlib import Path

                extra = Path(task["child_workspace"]["path"]) / "new-file"
                extra.write_text("unfinished work")
            else:
                thread["parentThreadId"] = "wrong"
            return cycle

        adapter.finish = changed
        return adapter

    facade.memory = memory
    result = await flow.invoke("resume", task_id=task["id"])
    assert result["phase"] == "blocked", result
    assert result["memory"]["write_receipt"]["state"] == "confirmed"
    assert flow.service.state.capacity()["occupied"] == 1
    receipt = result["memory"]["write_receipt"]
    # Restore only the simulated host/checkout, never change the report or ledger.
    thread.clear()
    thread.update(old_thread)
    native.clear()
    native.update(old_native)
    runs[:] = old_runs
    flow.host.threads.pop("descendant", None)
    if extra:
        extra.unlink()
    facade.memory = memory_factory
    result = await flow.invoke("resume", task_id=task["id"])
    assert result["phase"] == "pr_pending", result
    assert result["memory"]["write_receipt"] == receipt
    assert flow.service.state.capacity()["occupied"] == 0


def test_missing_history_does_not_prove_terminal():
    with pytest.raises(StateError, match="chain_incomplete"):
        terminal_chain({"thread": {"runCount": 3}, "recentRuns": []})


async def test_recover_malformed_report_on_third_run_preserves_original_audit(flow):
    task = await flow.begin()
    runs, thread, native = attach_chain(flow, task)
    bad = report(task)
    bad["checks"].append({"command": "not executed", "status": "not_run"})
    store = flow.service.managed_service.store
    store.update(
        task["id"],
        "validation_submitted",
        {"report_sha256": content_hash(bad)},
        submitted_report=bad,
    )
    good = report(task)
    good["limitations"].append("not executed")
    result = await flow.invoke(
        "recover-report",
        task_id=task["id"],
        report=good,
        expected_report_sha256=content_hash(bad),
        reason="Move unexecuted check into limitations",
    )
    assert result["phase"] == "validation_recovered", result
    assert store.get(task["id"])["report_recovery"]["original_report"] == bad
    result = await flow.invoke("resume", task_id=task["id"])
    assert result["phase"] == "pr_pending", result
    assert result["child"] == task["child"]
    replay = await flow.invoke(
        "recover-report",
        task_id=task["id"],
        report=good,
        expected_report_sha256=content_hash(bad),
        reason="Move unexecuted check into limitations",
    )
    assert not replay.get("rejection"), replay
    assert replay["memory"]["write_receipt"] == result["memory"]["write_receipt"]


@pytest.mark.parametrize("catalog_size", [0, 24])
async def test_resume_e2e_file_ack_and_identical_trace_byte_measurement(
    flow, tmp_path, catalog_size
):
    import json
    import os
    from pathlib import Path

    from test_report_prevalidation import run_cli

    task = await flow.begin()
    await submit_last(flow, task)
    if catalog_size:
        # Explicitly simulated full catalog/status payloads, like a busy project.
        # Both encodings consume exactly the same response bytes in this trace.
        child_thread = flow.host.threads[task["child"]["childThreadId"]]
        for n in range(catalog_size):
            flow.host.threads[f"unrelated-{n}"] = dict(
                child_thread,
                threadId=f"unrelated-{n}",
                parentThreadId=None,
                title=f"Fixture thread {n}",
            )
        flow.host.native_status[task["child"]["taskId"]]["summary"] = (
            "Fixture validation detail. " * 500
        )
        original_call = flow.host.call

        async def full_host(name, args):
            value = await original_call(name, args)
            if name == "orchestrator_capabilities":
                value["providers"][0]["models"] = [
                    dict(
                        id=f"model-{n}",
                        label=f"Model {n}",
                        options=[
                            dict(
                                id="reasoning",
                                label="Reasoning",
                                type="select",
                                options=[
                                    dict(
                                        id=level,
                                        label=level,
                                        description="Fixture option description",
                                    )
                                    for level in ["low", "medium", "high", "max"]
                                ],
                            )
                        ],
                    )
                    for n in range(32)
                ]
            return value

        flow.host.call = full_host
    wire = []
    code, result, error = await run_cli(
        flow, tmp_path, "resume", {"task_id": task["id"]}, wire=wire, file_ack=True
    )
    assert code == 0, (result, error)
    assert result["phase"] == "pr_pending", result
    assert result["memory"]["write_receipt"]["state"] == "confirmed"
    assert flow.service.state.capacity()["occupied"] == 0
    assert result["child"] == task["child"]
    measurement = dict(
        scenario=f"one three-run native child, one report, {catalog_size} unrelated threads, same resume trace",
        layer="disposable CLI/daemon/memory; simulated T3; V1 encoding versus actual V2 bytes",
        frames=len(wire),
        before_bytes=sum(r["before"] for r in wire),
        after_bytes=sum(r["after"] for r in wire),
        tools=wire,
    )
    if catalog_size:
        assert measurement["after_bytes"] < measurement["before_bytes"], measurement
    if os.environ.get("ORACULO_ACK_MEASUREMENT"):
        Path(
            os.environ["ORACULO_ACK_MEASUREMENT"] + f".{catalog_size}.json"
        ).write_text(json.dumps(measurement, indent=2))
    print(json.dumps(measurement))


@pytest.mark.parametrize("legacy", [False, True])
async def test_report_from_original_run_cannot_close_later_terminal_runs(
    flow, monkeypatch, legacy
):
    task = await flow.begin()
    good = report(task)
    store = flow.service.managed_service.store
    if legacy:
        # Create the original event under an isolated historical service clock;
        # no already-written event, report or signature is edited.
        import time

        with monkeypatch.context() as patch:
            patch.setattr(
                time,
                "time",
                lambda: datetime(
                    2026, 10, 21, 18, 1, 15, tzinfo=timezone.utc
                ).timestamp(),
            )
            store.update(
                task["id"],
                "validation_submitted",
                {"report_sha256": content_hash(good)},
                submitted_report=good,
            )
    else:
        flow.host.caller = task["child"]["childThreadId"]
        result = await flow.invoke("finish", task_id=task["id"], report=good)
        assert result["phase"] == "validation_submitted"
        flow.host.caller = "origin"
    attach_chain(flow, task)
    result = await flow.invoke("resume", task_id=task["id"])
    assert result["blocked_reason"] == "latest_run_report_unproven", result
    assert result["memory"]["write_state"] == "not_requested"
    assert flow.service.state.capacity()["occupied"] == 1
