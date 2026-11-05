"""Regression tests for managed lifecycle states that had no exit.

Finding ids: A2, A5, N-shared-diff-3, N-review-adjudication-2,
N-review-adjudication-3, N-decisions-3, B5.
"""

import copy
import time

import pytest
from test_managed_tasks import publication_setup, report
from test_managed_tasks import system as system_fixture
from test_operational_decisions import ChoiceTransport, install
from test_report_prevalidation import flow as flow_fixture
from test_stopped_original_continuation import attach, complete, submit_from_child

from oraculo.review_adjudication import coverage, read_private
from oraculo.state import StateError

flow = flow_fixture
system = system_fixture


def test_n_review_adjudication_2_missing_child_workspace_is_state_error():
    """N-review-adjudication-2: absent child_workspace is not a KeyError."""
    task = {
        "project": {"project_path": "/tmp/project"},
        "parent_workspace": {"path": "/tmp/parent"},
    }
    with pytest.raises(StateError, match="child_workspace_not_ready"):
        coverage(task, "a" * 40)
    with pytest.raises(StateError, match="child_workspace_not_ready"):
        read_private(
            {"path": "/tmp/evidence", "sha256": "a" * 64}, task, limit=32
        )


async def test_a2_incomplete_native_receipt_stays_reserved_until_cancel(flow):
    """A2: task_launch_ack_incomplete must not bind the reservation."""
    original = flow.host.call

    async def host(name, args):
        if name == "delegate_task":
            flow.host.calls.append((name, copy.deepcopy(args)))
            return {"childRunId": "ghost-run"}
        return await original(name, args)

    flow.host.call = host
    started = await flow.invoke(
        "start",
        kind="work",
        key="a2-incomplete-ack",
        text="Bounded work",
        cwd=str(flow.service.config.projects_root / "alpha"),
    )
    assert started["blocked_reason"] == "task_launch_ack_incomplete", started
    assert started.get("child") is None
    assert started["admission"]["state"] == "reserved"
    assert started["launch_attempted"] is True
    replayed = await flow.invoke("resume", task_id=started["id"])
    assert replayed["blocked_reason"] == "task_launch_ack_incomplete", replayed
    assert replayed.get("child") is None
    assert replayed["admission"]["state"] == "reserved"
    cancelled = await flow.invoke("cancel", task_id=started["id"])
    assert cancelled["work_status"] == "cancelled", cancelled
    assert cancelled["admission"]["state"] == "not_reserved"
    assert flow.service.state.capacity()["occupied"] == 0


async def test_a5_resume_of_merged_task_does_not_republish(system):
    """A5: resume must not relink, open memory, or regress a merged task."""
    task, spec, _github = await publication_setup(system)
    published = await system.engine.command(
        "publish", {"task_id": task["id"], "publication": spec}
    )
    assert published["phase"] == "pr_open", published
    system.store.update(
        task["id"],
        "blocked",
        {"reason": "final_memory_write_unconfirmed"},
        blocked_reason="final_memory_write_unconfirmed",
        merged=True,
        publication_request=dict(spec, repository="example/corrected"),
    )
    links = set(system.host.links)
    cycles = len(system.service.memory_cycles.list())
    resumed = await system.engine.command("resume", {"task_id": task["id"]})
    assert resumed["phase"] == "blocked", resumed
    assert resumed["blocked_reason"] == "final_memory_write_unconfirmed"
    assert resumed["blocked_reason"] != "pr_not_open_requires_reconciliation"
    assert resumed["blocked_reason"] != "publication_intent_reused"
    assert system.host.links == links
    assert len(system.service.memory_cycles.list()) == cycles


async def test_a5_explicit_publish_of_merged_task_only_reobserves(system):
    """A5: a recorded merged publication is not relinked or given a new cycle."""
    task, spec, github = await publication_setup(system)
    published = await system.engine.command(
        "publish", {"task_id": task["id"], "publication": spec}
    )
    assert published["phase"] == "pr_open", published
    system.store.update(
        task["id"],
        "merge_pending",
        {"test": "a5-reobserve"},
        merged=True,
        publication=dict(published["publication"], state="merged"),
    )
    github.rows[0]["state"] = "closed"
    github.rows[0]["merged_at"] = "2026-11-05T00:00:00Z"
    links = set(system.host.links)
    cycles = len(system.service.memory_cycles.list())
    again = await system.engine.command(
        "publish", {"task_id": task["id"], "publication": spec}
    )
    assert again["phase"] == "merge_pending", again
    assert not again.get("blocked_reason")
    assert system.host.links == links
    assert len(system.service.memory_cycles.list()) == cycles


async def test_a5_resume_prefers_recorded_publication_spec(system):
    """A5: a corrected publication_request must not replace the recorded spec."""
    task, spec, github = await publication_setup(system)
    github.lose_response = True
    github.visible = False
    first = await system.engine.command(
        "publish", {"task_id": task["id"], "publication": spec}
    )
    assert first["blocked_reason"] == "publication_uncertain", first
    assert first.get("publication_attempted") is True
    assert not first.get("publication")
    system.store.update(
        task["id"],
        first["phase"],
        {"test": "a5-spec"},
        publication_request=dict(spec, repository="example/corrected"),
    )
    github.lose_response = False
    github.visible = True
    resumed = await system.engine.command("resume", {"task_id": task["id"]})
    assert resumed["phase"] == "pr_open", resumed
    assert resumed.get("blocked_reason") != "publication_intent_reused"


async def test_n_shared_diff_3_diagnose_delivery_leaves_executing_task_unchanged(
    flow,
):
    """N-shared-diff-3: diagnose-delivery on a live child does not block it."""
    task = await flow.begin()
    before = flow.snapshot(task)
    result = await flow.invoke("diagnose-delivery", task_id=task["id"])
    diagnosis = result["legacy_delivery_diagnosis"]
    assert diagnosis["eligible"] is False, result
    assert diagnosis["missing_evidence"] == (
        "terminal_execution_or_descendants_unproven"
    )
    assert result["phase"] == "executing"
    assert flow.snapshot(task) == before


async def test_n_review_adjudication_3_diagnose_review_timeout_is_not_persisted(
    flow,
):
    """N-review-adjudication-3: a host timeout during diagnose-review is a read.

    The service relay reports the timeout as host_response_uncertain. The
    diagnosis stays in the response and the task snapshot is unchanged.
    """
    task = await flow.begin()
    before = flow.snapshot(task)
    original = flow.host.call
    child = task["child"]["childThreadId"]

    async def host(name, args):
        if name == "t3_thread_read" and args.get("threadId") == child:
            raise TimeoutError("relay")
        return await original(name, args)

    flow.host.call = host
    result = await flow.invoke("diagnose-review", task_id=task["id"])
    assert result["phase"] == "executing", result
    assert result["review_diagnosis"]["reason"] == "host_response_uncertain"
    assert result["review_diagnosis"]["certification"] == "not_granted_by_diagnosis"
    assert flow.snapshot(task) == before


async def test_n_review_adjudication_2_resume_repairs_missing_child_workspace(flow):
    """N-review-adjudication-2: resume re-observes a missing child workspace."""
    task = await flow.begin()
    good = report(task)
    store = flow.service.managed_service.store
    store.update(
        task["id"],
        task["phase"],
        {"test": "n-review-adjudication-2"},
        child_workspace=None,
    )
    assert store.get(task["id"]).get("child_workspace") is None
    flow.host.caller = task["child"]["childThreadId"]
    submitted = await flow.invoke("finish", task_id=task["id"], report=good)
    assert submitted["phase"] == "validation_submitted", submitted
    flow.host.complete(task["child"]["childThreadId"])
    flow.host.caller = "origin"
    result = await flow.invoke("resume", task_id=task["id"])
    assert result["phase"] == "pr_pending", result
    assert result["child_workspace"]["path"]
    assert not result.get("blocked_reason")


async def test_n_decisions_3_expired_route_is_cleared_for_the_next_resume(system):
    """N-decisions-3: advice expiry before launch must not pin the same route."""
    service = system.service
    transport = ChoiceTransport()
    install(service, transport)
    system.engine.admission = service.state
    system.engine.decisions = service.decisions
    original = system.host.call
    seen = {"n": 0}

    async def host(name, arguments):
        if name == "orchestrator_capabilities":
            seen["n"] += 1
            if seen["n"] == 3:
                raise TimeoutError("ack")
        result = await original(name, arguments)
        if name == "orchestrator_capabilities":
            result["inheritedModel"] = None
            result["providers"][0]["models"] = [
                {"id": "current"},
                {"id": "alternative"},
            ]
        if name == "delegate_task" and arguments.get("target"):
            result.update(arguments["target"])
        return result

    system.host.call = host
    from test_managed_tasks import start

    first = await start(system)
    assert first["blocked_reason"] == "TimeoutError", first
    stored = system.store.get(first["id"])
    assert stored.get("decision_route")
    assert stored.get("launch_arguments")
    assert not stored.get("launch_attempted")
    system.engine.decisions.clock = lambda: time.time() + 1000
    blocked = await system.engine.command("resume", {"task_id": first["id"]})
    assert blocked["blocked_reason"] == "managed_route_effect_revalidation_failed", (
        blocked
    )
    cleared = system.store.get(first["id"])
    assert cleared.get("decision_route") is None
    assert cleared.get("launch_arguments") is None
    calls = len(transport.calls)
    launched = await system.engine.command("resume", {"task_id": first["id"]})
    assert launched["phase"] == "executing", launched
    assert len(transport.calls) > calls


async def test_b5_identical_resubmission_from_the_executed_run_certifies(flow):
    """B5: the continuation's own identical report certifies the latest run."""
    task = await flow.begin()
    good = report(flow.service.managed_service.store.get(task["id"]))
    await submit_from_child(flow, task, good)
    runs, _thread, _native = attach(flow, task, ["cancelled", "completed"])
    await submit_from_child(flow, task, good)
    complete(flow, task, runs)
    result = await flow.invoke("resume", task_id=task["id"])
    assert result["phase"] == "pr_pending", result
    assert result["work_status"] == "succeeded"
    evidence = flow.service.managed_service.store.get(task["id"])[
        "completion_evidence"
    ]
    assert evidence["submission_actor"]["run_id"] == runs[-1]["runId"]
