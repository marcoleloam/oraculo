"""Plan acceptance through the public service protocol and T3 reconciliation."""

import json

import pytest
from test_reconcile import Remote, snapshot
from test_service import command

from oraculo.reconcile import Reconciler
from oraculo.service import Service


def task(identifier, *, stage="implementation", dependencies=(), paths=None):
    return {
        "id": identifier,
        "owner": "assigned executor",
        "brief": identifier,
        "kind": "memory"
        if stage == "memory"
        else "tests"
        if stage == "review"
        else "implementation",
        "stage": stage,
        "depends_on": list(dependencies),
        "write_paths": paths if paths is not None else [identifier],
        "criteria": [{"id": "proof", "description": "observable result"}],
        "payload": {"memory_operation": "lint"} if stage == "memory" else {},
    }


async def submit(service, tasks):
    response = await command(
        service,
        "plan.submit",
        {"dedupe_key": "plan", "plan": {"text": "Bounded delivery", "tasks": tasks}},
    )
    assert response["ok"], response
    return response["data"]


async def grant(service, job_id, suffix):
    response = await command(
        service,
        "grant.acquire",
        {"job_id": job_id},
        origin={"thread_id": "coordinator", "run_id": "coordinator-run"},
    )
    assert response["ok"], response
    attempt = response["data"]["attempt"]
    response = await command(
        service,
        "grant.claim",
        {
            "attempt_id": attempt["id"],
            "generation": attempt["generation"],
            "thread_id": "child-" + suffix,
            "run_id": "run-" + suffix,
            "task_id": "task-" + suffix,
        },
        origin={"thread_id": "coordinator", "run_id": "coordinator-run"},
    )
    assert response["ok"], response
    return service.state.get_attempt(attempt["id"])


async def report(service, attempt, *, status="succeeded", evidence=None):
    return await command(
        service,
        "execution.report",
        {
            "attempt_id": attempt["id"],
            "generation": attempt["generation"],
            "run_id": attempt["run_id"],
            "status": status,
            "evidence": evidence
            or {
                "criteria": [
                    {"id": "proof", "status": "passed", "reference": "test result"}
                ]
            },
        },
        origin={"thread_id": attempt["thread_id"], "run_id": attempt["run_id"]},
    )


class Dispatch:
    def __init__(self):
        self.sent = []

    async def send(self, thread, message, key, **kwargs):
        self.sent.append((key, json.loads(message)))
        return {"runId": "dispatch-run"}

    async def read_thread(self, thread):
        return snapshot(thread, "dispatch-run", "running")


async def finish(service, attempt):
    remote = Remote()
    remote.snapshots[attempt["thread_id"]] = snapshot(
        attempt["thread_id"], attempt["run_id"], "completed"
    )
    remote.snapshots[attempt["thread_id"]]["thread"]["projectId"] = "alpha"
    assert (await Reconciler(service.state, remote).reconcile())["released"]
    service.after_execution()


async def test_plan_memory_is_not_delivery_and_successors_dispatch_after_restart(
    running_service,
):
    service = running_service
    plan = await submit(
        service,
        [
            task("memory", stage="memory"),
            task("build", dependencies=["memory"]),
            task("review", stage="review", dependencies=["build"], paths=[]),
        ],
    )
    memory = await grant(service, plan["tasks"][0]["job_id"], "memory")
    assert (await report(service, memory))["ok"]
    await finish(service, memory)
    status = service.plans.describe(plan["objective_id"])
    assert status["status"] != "succeeded"
    assert status["stages"]["memory"] == "succeeded"
    assert status["stages"]["implementation"] == "pending"
    assert status["ready"] == ["build"]
    restarted = Service(service.config, service.config_path)
    restarted.catalog = service.catalog
    try:
        dispatch = Dispatch()
        await restarted.dispatch(dispatch)
        await restarted.dispatch(dispatch)
        assert len(dispatch.sent) == 1
        assert dispatch.sent[0][1]["job_id"] == plan["tasks"][1]["job_id"]
        assert (
            dispatch.sent[0][1]["task_context"]["dependency_evidence"][0]["task_id"]
            == "task-memory"
        )
    finally:
        restarted.state.close()
    build = await grant(service, plan["tasks"][1]["job_id"], "build")
    assert (await report(service, build))["ok"]
    await finish(service, build)
    dispatch = Dispatch()
    await service.dispatch(dispatch)
    assert dispatch.sent[0][1]["job_id"] == plan["tasks"][2]["job_id"]
    review = await grant(service, plan["tasks"][2]["job_id"], "review")
    assert (await report(service, review))["ok"]
    await finish(service, review)
    status = service.plans.describe(plan["objective_id"])
    assert status["status"] == "succeeded"
    assert len({t["attempts"][0]["thread_id"] for t in status["tasks"]}) == 3
    assert (
        len(service.state.list_jobs()) == 3
    )  # no implicit memory that could mask work


async def test_plan_report_requires_each_receipt_and_matching_project(running_service):
    service = running_service
    plan = await submit(service, [task("build")])
    attempt = await grant(service, plan["tasks"][0]["job_id"], "build")
    rejected = await report(service, attempt, evidence={"summary": "done"})
    assert not rejected["ok"]
    assert service.state.get_meta(f"execution_report:{attempt['id']}") is None
    assert service.state.lease(attempt["id"])
    service.state.register_project("beta", str(service.config.projects_root / "beta"))
    from oraculo.cli import make_envelope
    from oraculo.protocol import Envelope

    cross = make_envelope(
        "task.retry", "beta", {"objective_id": plan["objective_id"], "task_id": "build"}
    )
    from oraculo.state import StateError

    with pytest.raises(StateError, match="objective_project_mismatch"):
        await service.handle(Envelope.model_validate(cross))


@pytest.mark.parametrize(
    "cancel,blocked", [(True, False), (False, True), (False, False)]
)
async def test_failure_cancel_and_blocker_do_not_dispatch_successor(
    running_service, cancel, blocked
):
    service = running_service
    service.config.max_correction_rounds = 1
    plan = await submit(
        service, [task("build"), task("review", stage="review", dependencies=["build"])]
    )
    attempt = await grant(service, plan["tasks"][0]["job_id"], "build")
    if cancel:
        assert (
            await command(
                service, "objective.cancel", {"objective_id": plan["objective_id"]}
            )
        )["ok"]
    evidence = {"summary": "failed verification"}
    if blocked:
        evidence["blocker"] = {
            "reason": "missing artifact",
            "owner": "operator",
            "action": "provide artifact",
        }
    assert (await report(service, attempt, status="failed", evidence=evidence))["ok"]
    await finish(service, attempt)
    dispatch = Dispatch()
    await service.dispatch(dispatch)
    assert all(
        body["job_id"] != plan["tasks"][1]["job_id"] for _, body in dispatch.sent
    )
    if cancel or blocked:
        assert not dispatch.sent
    else:
        assert len(dispatch.sent) == 1
        retry = await grant(service, plan["tasks"][0]["job_id"], "retry")
        assert retry["number"] == 2
        assert (await report(service, retry, status="failed", evidence=evidence))["ok"]
        await finish(service, retry)
        await service.dispatch(dispatch)
        assert len(dispatch.sent) == 1
        assert service.plans.describe(plan["objective_id"])["status"] == "blocked"


async def test_lost_dispatch_response_reuses_bytes_and_id_after_restart(
    running_service,
):
    service = running_service
    await submit(service, [task("build"), task("review", dependencies=["build"])])

    class Lost(Dispatch):
        async def send(self, thread, message, key, **kwargs):
            await super().send(thread, message, key, **kwargs)
            raise ConnectionError("accepted remotely, reply lost")

    lost = Lost()
    with pytest.raises(ConnectionError):
        await service.dispatch(lost)
    restarted = Service(service.config, service.config_path)
    restarted.catalog = service.catalog
    try:
        recovered = Dispatch()
        await restarted.dispatch(recovered)
        assert recovered.sent == lost.sent
        assert len(restarted.state.list_jobs()) == 2
    finally:
        restarted.state.close()


async def test_dispatch_skips_conflicting_writer_for_independent_work(running_service):
    service = running_service
    service.config.max_executors = 2
    service.state.max_executors = 2
    plan = await submit(
        service,
        [
            task("active", paths=["src"]),
            task("conflict", paths=["src/child"]),
            task("independent", paths=["docs"]),
        ],
    )
    await grant(service, plan["tasks"][0]["job_id"], "active")
    dispatch = Dispatch()
    await service.dispatch(dispatch)
    assert len(dispatch.sent) == 1
    assert dispatch.sent[0][1]["job_id"] == plan["tasks"][2]["job_id"]


async def test_planned_executor_cannot_escape_followup_or_stage_budget(running_service):
    service = running_service
    plan = await submit(service, [task("build")])
    attempt = await grant(service, plan["tasks"][0]["job_id"], "build")
    identity = {"thread_id": attempt["thread_id"], "run_id": attempt["run_id"]}
    for kind, payload in [
        ("objective.submit", {"text": "escape"}),
        ("plan.submit", {"plan": {"text": "escape", "tasks": [task("new")]}}),
    ]:
        response = await command(service, kind, payload, origin=identity)
        assert not response["ok"]
        assert "bounded_plan_extend" in response["error"]["message"]
    for kind in ["push", "pr", "merge", "deploy", "arbitrary"]:
        response = await command(
            service,
            "operation.prepare",
            {
                "attempt_id": attempt["id"],
                "generation": attempt["generation"],
                "operation_id": "unauthorized-" + kind,
                "kind": kind,
                "expected_state": {},
            },
            origin=identity,
        )
        assert not response["ok"]
        assert "operation_outside_planned_stage" in response["error"]["message"]
    assert (
        service.state.connection.execute("SELECT count(*) FROM operations").fetchone()[
            0
        ]
        == 0
    )


async def test_oversized_new_dispatch_is_explicit_unsent_blocker(running_service):
    service = running_service
    oversized = task("large")
    oversized["brief"] = "x" * 16000
    oversized["criteria"] = [
        {"id": f"c{i}", "description": "🔍" * 2000} for i in range(32)
    ]
    plan = await submit(service, [oversized])
    dispatch = Dispatch()
    await service.dispatch(dispatch)
    assert not dispatch.sent
    status = service.plans.describe(plan["objective_id"])
    assert status["status"] == "blocked"
    assert status["ready"] == []
    assert status["tasks"][0]["blocker"] == {
        "reason": "task_dispatch_too_large",
        "owner": "assigned executor",
        "action": "Cancel and resubmit a bounded plan using source references and concise criterion/dependency receipts",
    }
    assert (
        service.state.connection.execute("SELECT count(*) FROM outbox").fetchone()[0]
        == 0
    )
    assert service.state.leases() == []


async def test_existing_coordinator_receives_current_plan_contract(running_service):
    service = running_service
    await submit(service, [task("build")])
    dispatch = Dispatch()
    await service.dispatch(dispatch)
    body = dispatch.sent[0][1]
    assert "plan.submit" in body["coordinator_instructions"]
    assert body["task_context"]["isolation"] == "shared"
    assert "executors" in body["executor_instructions"]


@pytest.mark.parametrize(
    "blocker", ["help", {}, {"reason": "failed", "owner": "", "action": "fix"}]
)
async def test_planned_blocker_requires_action_and_owner(running_service, blocker):
    service = running_service
    plan = await submit(service, [task("build")])
    attempt = await grant(service, plan["tasks"][0]["job_id"], "build")
    response = await report(
        service, attempt, status="failed", evidence={"blocker": blocker}
    )
    assert not response["ok"]
    assert (
        "planned_blocker_requires_reason_owner_action" in response["error"]["message"]
    )
