"""Audited origin recovery uses real socket/ledger/memory and a synthetic host."""

import copy

import pytest
from test_managed_tasks import report
from test_origin_closure import sibling
from test_report_prevalidation import flow as flow_fixture
from test_stdio_fifo import run_cli_fifo

from oraculo.protocol import content_hash
from oraculo.state import StateError

flow = flow_fixture


def independent_report(task):
    evidence = report(task)
    evidence["summary"] = (
        "Origin independently validated the delivered work after API failure"
    )
    evidence["checks"][0].update(
        result="1 passed, exit 0", proof="/private/independent-validation.log"
    )
    evidence["limitations"].append(
        "Host run failed; origin independently reran these checks"
    )
    return evidence


def completion(task):
    return dict(
        task_id=task["id"],
        reason="Provider API failed after commits; origin reran validation",
        expected_child_run_id=task["child"]["childRunId"],
        expected_latest_run_id=task["child"]["childRunId"],
        report=independent_report(task),
    )


@pytest.mark.parametrize("independent", [False, True])
async def test_origin_completes_failed_host_preserving_failure_and_live_sibling(
    flow, independent
):
    task = await flow.begin(independent=independent)
    other = await sibling(flow)
    flow.host.complete(task["child"]["childThreadId"], status="failed")
    payload = completion(task)
    result = await flow.invoke("complete-origin", **payload)
    assert result["phase"] == "pr_pending", result
    assert result["work_status"] == "succeeded"
    assert result["memory"]["write_receipt"]["state"] == "confirmed"
    stored = flow.service.managed_service.store.get(task["id"])
    assert stored["child"] == task["child"]
    assert stored["origin_completion"]["runs"][0]["status"] == "failed"
    assert stored["completion_evidence"]["runs"][0]["status"] == "failed"
    assert stored["report"] == payload["report"]
    assert stored.get("submitted_report") is None
    assert flow.host.threads[other["child"]["childThreadId"]]["status"] == "running"
    assert flow.service.state.capacity()["occupied"] == 1
    snapshot = flow.snapshot(task)
    for action in ("complete-origin", "resume", "finish"):
        result = await flow.invoke(
            action,
            **(
                payload
                if action == "complete-origin"
                else dict(
                    task_id=task["id"],
                    **({"report": payload["report"]} if action == "finish" else {}),
                )
            ),
        )
        assert result["phase"] == "pr_pending", result
    assert flow.snapshot(task) == snapshot


@pytest.mark.parametrize(
    "defect",
    [
        "child_actor",
        "negative",
        "no_proof",
        "no_result",
        "wrong_run",
        "completed",
        "active",
        "descendant",
        "dirty",
        "submitted_negative",
        "submitted_positive",
    ],
)
async def test_completion_cannot_erase_evidence_or_bypass_gates(flow, defect):
    task = await flow.begin()
    payload = completion(task)
    child = task["child"]["childThreadId"]
    flow.host.complete(child, status="failed")
    if defect == "child_actor":
        flow.host.caller = child
        flow.host.threads[child]["activeRunId"] = task["child"]["childRunId"]
    elif defect == "negative":
        payload["report"]["checks"][0]["status"] = "failed"
    elif defect == "no_proof":
        payload["report"]["checks"][0].pop("proof")
    elif defect == "no_result":
        payload["report"]["checks"][0].pop("result")
    elif defect == "wrong_run":
        payload["expected_latest_run_id"] = "unrelated"
    elif defect == "completed":
        flow.host.complete(child)
    elif defect == "active":
        flow.host.threads[child]["activeRunId"] = "still-running"
    elif defect == "descendant":
        flow.host.complete(child, pending=True, status="failed")
    elif defect == "dirty":
        from pathlib import Path

        Path(task["child_workspace"]["path"], "unfinished").write_text("work")
    else:
        # Actual executor submission through the public socket path.
        flow.host.caller = child
        flow.host.threads[child]["activeRunId"] = task["child"]["childRunId"]
        old = independent_report(task)
        if defect == "submitted_negative":
            old["checks"][0]["status"] = "failed"
        submitted = await flow.invoke("finish", task_id=task["id"], report=old)
        assert submitted["phase"] == "validation_submitted", submitted
        flow.host.complete(child, status="failed")
        flow.host.caller = "origin"
    result = await flow.invoke("complete-origin", **payload)
    assert result["phase"] == "blocked", result
    assert result["memory"]["write_state"] == "not_requested"
    assert result["work_status"] == "pending"
    assert flow.service.state.capacity()["occupied"] == 1
    assert (
        flow.service.managed_service.store.get(task["id"]).get("origin_completion")
        is None
    )


@pytest.mark.parametrize("change", ["active", "dirty", "descendant", "chain"])
async def test_origin_completion_checks_again_after_memory_and_replays_exact_intent(
    flow, change
):
    task = await flow.begin()
    child = task["child"]["childThreadId"]
    flow.host.complete(child, status="failed")
    facade = flow.service.managed_service
    factory = facade.memory
    saved_thread = copy.deepcopy(flow.host.threads[child])
    saved_native = copy.deepcopy(flow.host.native_status)
    extra = None

    def memory():
        adapter = factory()
        finish = adapter.finish

        async def changed(*args, **kwargs):
            nonlocal extra
            cycle = await finish(*args, **kwargs)
            if change == "active":
                flow.host.threads[child]["activeRunId"] = "later"
            elif change == "dirty":
                from pathlib import Path

                extra = Path(task["child_workspace"]["path"], "unfinished")
                extra.write_text("work")
            elif change == "descendant":
                flow.host.complete(child, pending=True, status="failed")
            else:
                flow.host.threads[child].update(latestRunId="new", runCount=2)
            return cycle

        adapter.finish = changed
        return adapter

    facade.memory = memory
    payload = completion(task)
    result = await flow.invoke("complete-origin", **payload)
    assert result["phase"] == "blocked", result
    assert result["memory"]["write_receipt"]["state"] == "confirmed"
    receipt = result["memory"]["write_receipt"]
    assert flow.service.state.capacity()["occupied"] == 1
    flow.host.threads[child] = saved_thread
    flow.host.native_status = saved_native
    flow.host.threads.pop("reviewer-" + child, None)
    if extra:
        extra.unlink()
    facade.memory = factory
    result = await flow.invoke("complete-origin", **payload)
    assert result["phase"] == "pr_pending", result
    assert result["memory"]["write_receipt"] == receipt
    assert flow.service.state.capacity()["occupied"] == 0


def supersession(task, other):
    negative = independent_report(task)
    negative.update(
        summary="FAIL — incomplete API run superseded by explicit replacement",
        verdict="FAIL",
    )
    return dict(
        task_id=task["id"],
        replacement_task_id=other["id"],
        reason="Replaced by a revised task",
        report=negative,
    )


@pytest.mark.parametrize("certified", [False, True])
async def test_supersede_preserves_negative_report_memory_and_live_replacement(
    flow, certified
):
    task = await flow.begin()
    other = await sibling(flow)
    flow.host.complete(task["child"]["childThreadId"], status="failed")
    payload = supersession(task, other)
    if certified:
        failed = await flow.invoke(
            "finish", task_id=task["id"], report=payload["report"]
        )
        assert failed["blocked_reason"] == "work_not_successful", failed
    result = await flow.invoke("supersede", **payload)
    assert result["phase"] == "superseded", result
    assert result["work_status"] == "failed"
    assert result["memory"]["write_state"] == "saved"
    stored = flow.service.managed_service.store.get(task["id"])
    assert stored["report"] == payload["report"]
    assert stored["supersession"]["report_sha256"] == content_hash(payload["report"])
    assert flow.service.state.capacity()["occupied"] == 1
    snapshot = flow.snapshot(task)
    for action in ("supersede", "resume"):
        result = await flow.invoke(
            action, **(payload if action == "supersede" else dict(task_id=task["id"]))
        )
        assert result["phase"] == "superseded", result
    assert flow.snapshot(task) == snapshot
    assert flow.host.threads[other["child"]["childThreadId"]]["status"] == "running"


@pytest.mark.parametrize(
    "defect",
    [
        "self",
        "wrong_origin",
        "active",
        "negative_rewritten",
        "positive_pending",
        "changed_intent",
    ],
)
async def test_supersede_refuses_invalid_bindings_or_rewriting(flow, defect):
    task = await flow.begin()
    other = await sibling(flow)
    flow.host.complete(task["child"]["childThreadId"], status="failed")
    payload = supersession(task, other)
    if defect == "self":
        payload["replacement_task_id"] = task["id"]
    elif defect == "wrong_origin":
        flow.host.caller = other["child"]["childThreadId"]
        with pytest.raises(StateError, match="managed_task_origin_mismatch"):
            await flow.invoke("supersede", **payload)
        return
    elif defect == "active":
        flow.host.threads[task["child"]["childThreadId"]]["activeRunId"] = "live"
    elif defect == "positive_pending":
        payload["report"] = independent_report(task)
    elif defect == "negative_rewritten":
        flow.host.caller = task["child"]["childThreadId"]
        flow.host.threads[flow.host.caller]["activeRunId"] = task["child"]["childRunId"]
        submitted = await flow.invoke(
            "finish", task_id=task["id"], report=payload["report"]
        )
        assert submitted["phase"] == "validation_submitted", submitted
        flow.host.complete(flow.host.caller, status="failed")
        flow.host.caller = "origin"
        payload["report"] = independent_report(task)
    else:
        done = await flow.invoke("supersede", **payload)
        assert done["phase"] == "superseded", done
        payload["reason"] = "Changed justification"
    snapshot = flow.snapshot(task)
    result = await flow.invoke("supersede", **payload)
    assert result["phase"] == "blocked", result
    after = flow.snapshot(task)
    assert after["cycle"] == snapshot["cycle"]
    assert after["task"].get("report") == snapshot["task"].get("report")
    assert after["task"].get("submitted_report") == snapshot["task"].get(
        "submitted_report"
    )
    assert after["reservation"] == snapshot["reservation"]


@pytest.mark.parametrize("action", ["complete-origin", "supersede"])
async def test_cli_stdio_origin_recovery_against_real_daemon(flow, tmp_path, action):
    task = await flow.begin()
    other = await sibling(flow)
    flow.host.complete(task["child"]["childThreadId"], status="failed")
    payload = (
        completion(task) if action == "complete-origin" else supersession(task, other)
    )
    code, result, sizes, stderr = await run_cli_fifo(
        flow, tmp_path, action, payload, pad_hex=14000, minimum=8192
    )
    assert code == 0, (result, stderr)
    assert result["source"] == "service"
    assert result["phase"] == (
        "pr_pending" if action == "complete-origin" else "superseded"
    ), result
    assert sizes and len(sizes) <= 20
    assert flow.service.state.capacity()["occupied"] == 1


@pytest.mark.parametrize("pending_child", [False, True])
async def test_supersede_deleted_checkout_still_requires_terminal_host(
    flow, pending_child
):
    import shutil

    task = await flow.begin()
    other = await sibling(flow)
    flow.host.complete(
        task["child"]["childThreadId"], status="failed", pending=pending_child
    )
    payload = supersession(task, other)
    shutil.rmtree(task["child_workspace"]["path"])
    result = await flow.invoke("supersede", **payload)
    if pending_child:
        assert result["phase"] == "blocked", result
        assert result["memory"]["write_state"] == "not_requested"
        assert flow.service.state.capacity()["occupied"] == 2
    else:
        assert result["phase"] == "superseded", result
        assert result["memory"]["write_state"] == "saved"
        assert flow.service.state.capacity()["occupied"] == 1
        assert result["presentation"]["phase"] == "Superada"


async def test_origin_completion_resume_after_memory_retry_is_idempotent(flow):
    task = await flow.begin()
    flow.host.complete(task["child"]["childThreadId"], status="failed")
    facade = flow.service.managed_service
    factory = facade.memory
    original = facade.memory

    def memory():
        adapter = original()
        finish = adapter.finish

        async def uncertain(*args, **kwargs):
            await finish(*args, **kwargs)
            raise StateError("memory_write_unconfirmed")

        adapter.finish = uncertain
        return adapter

    facade.memory = memory
    result = await flow.invoke("complete-origin", **completion(task))
    assert result["phase"] == "blocked", result
    assert flow.service.state.capacity()["occupied"] == 1
    facade.memory = factory
    result = await flow.invoke("resume", task_id=task["id"])
    assert result["phase"] == "pr_pending", result
    assert result["memory"]["write_state"] == "saved"
    assert flow.service.state.capacity()["occupied"] == 0
