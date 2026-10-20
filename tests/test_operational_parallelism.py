"""Isolated protocol regressions with simulated T3; not evidence of real overlap."""

import json

import pytest
from test_plan_service import Dispatch, grant, report, task
from test_reconcile import Remote, snapshot
from test_service import command

from oraculo.config import ProviderConfig
from oraculo.reconcile import Reconciler
from oraculo.service import Service

COORDINATOR = {"thread_id": "coordinator", "run_id": "coordinator-run"}


def startup_ceiling(service, maximum=2):
    """Fixture-only equivalent of constructing the daemon with this ceiling."""
    service.config.max_executors = maximum
    service.state.max_executors = maximum


async def submit_plan(service, tasks, maximum=2):
    response = await command(
        service,
        "plan.submit",
        {
            "dedupe_key": "operational-plan",
            "plan": {
                "text": "Isolated simulated concurrency",
                "max_parallel": maximum,
                "tasks": tasks,
            },
        },
    )
    assert response["ok"], response
    return response["data"]


async def reserve(service, job_id, **kwargs):
    return await command(
        service, "grant.acquire", {"job_id": job_id}, origin=COORDINATOR, **kwargs
    )


async def current_status(service):
    response = await command(service, "status.get", {})
    assert response["ok"], response
    return response["data"]


def simulated_remote(attempts, completed=()):
    remote = Remote()
    remote.snapshots = {}
    for attempt in attempts:
        remote.snapshots[attempt["thread_id"]] = snapshot(
            attempt["thread_id"],
            attempt["run_id"],
            "completed" if attempt["id"] in completed else "running",
        )
        remote.snapshots[attempt["thread_id"]]["thread"]["projectId"] = "alpha"
    return remote


@pytest.mark.parametrize("maximum,allowed", [(1, False), (2, True)])
async def test_explicit_plan_serial_or_parallel_admission(
    running_service, maximum, allowed
):
    service = running_service
    startup_ceiling(service)
    plan = await submit_plan(service, [task("a"), task("b")], maximum)
    first = await grant(service, plan["tasks"][0]["job_id"], "a")
    second = await reserve(service, plan["tasks"][1]["job_id"])
    assert second["ok"], second
    assert (second["data"]["state"] == "reserved") is allowed
    assert service.state.lease(first["id"])
    assert len(service.state.leases()) == (2 if allowed else 1)
    if not allowed:
        status = await current_status(service)
        pending = status["plans"][0]["tasks"][1]
        assert "plan_capacity" in {
            b["reason"] for b in pending["admission"]["blockers"]
        }


async def test_capacity_reduction_drains_without_revoking_and_persists(
    running_service,
):
    service = running_service
    startup_ceiling(service)
    plan = await submit_plan(service, [task("a"), task("b"), task("c")])
    attempts = [
        await grant(service, entry["job_id"], entry["id"])
        for entry in plan["tasks"][:2]
    ]
    original = service.state.leases()
    reduced = await command(service, "capacity.set", {"max_executors": 1})
    assert reduced["ok"], reduced
    assert reduced["data"]["effective"] == 1
    assert reduced["data"]["occupied"] == 2
    assert reduced["data"]["available"] == 0
    assert reduced["data"]["draining"] is True
    assert service.state.leases() == original
    for attempt in attempts:
        assert service.state.validate_grant(attempt["id"], attempt["generation"])
    restarted = Service(service.config, service.config_path)
    try:
        assert restarted.state.capacity()["effective"] == 1
        assert restarted.state.capacity()["source"] == "persisted"
        assert restarted.state.leases() == original
        assert (
            restarted.state.acquire_grant(
                plan["tasks"][2]["job_id"], "codex", "current", "coordinator", "run"
            )
            is None
        )
    finally:
        restarted.state.close()
    assert (await report(service, attempts[0]))["ok"]
    remote = simulated_remote(attempts, completed={attempts[0]["id"]})
    await Reconciler(service.state, remote).reconcile()
    assert service.state.capacity()["occupied"] == 1
    assert service.state.capacity()["available"] == 0
    assert service.state.lease(attempts[1]["id"])
    assert (await reserve(service, plan["tasks"][2]["job_id"]))["data"][
        "state"
    ] == "busy"


async def test_capacity_replay_is_receipt_not_new_policy_mutation(running_service):
    service = running_service
    startup_ceiling(service)
    original = await command(
        service, "capacity.set", {"max_executors": 1}, request_id="capacity-original"
    )
    assert original["ok"], original
    assert (await command(service, "capacity.set", {"max_executors": 2}))["ok"]
    replay = await command(
        service, "capacity.set", {"max_executors": 1}, request_id="capacity-original"
    )
    assert replay == original
    fresh = await command(service, "capacity.get", {})
    assert fresh["data"]["effective"] == 2
    conflict = await command(
        service, "capacity.set", {"max_executors": 2}, request_id="capacity-original"
    )
    assert not conflict["ok"]
    assert service.state.capacity()["effective"] == 2


@pytest.mark.parametrize("maximum", [0, 3, True, 1.5, "2"])
async def test_capacity_rejects_invalid_or_above_startup_ceiling(
    running_service, maximum
):
    service = running_service
    startup_ceiling(service)
    rejected = await command(service, "capacity.set", {"max_executors": maximum})
    assert not rejected["ok"]
    assert service.state.capacity()["effective"] == 2
    assert service.state.capacity()["source"] == "configuration"


async def test_coordinator_cannot_raise_global_capacity(running_service):
    service = running_service
    startup_ceiling(service)
    await command(service, "capacity.set", {"max_executors": 1})
    rejected = await command(
        service, "capacity.set", {"max_executors": 2}, origin=COORDINATOR
    )
    assert not rejected["ok"]
    assert service.state.capacity()["effective"] == 1


async def test_existing_plan_configuration_preserves_submission_identity(
    running_service,
):
    service = running_service
    startup_ceiling(service)
    tasks = [task("a"), task("b")]
    plan = await submit_plan(service, tasks, maximum=1)
    configured = await command(
        service,
        "plan.configure",
        {"objective_id": plan["objective_id"], "max_parallel": 2},
        origin=COORDINATOR,
    )
    assert configured["ok"], configured
    assert configured["data"]["concurrency"]["effective"] == 2
    replay = await submit_plan(service, tasks, maximum=1)
    assert replay["objective_id"] == plan["objective_id"]
    assert replay["concurrency"]["requested"] == 2
    assert len(service.state.list_jobs()) == 2
    rejected = await command(
        service,
        "plan.configure",
        {"objective_id": plan["objective_id"], "max_parallel": 1},
        origin={"thread_id": "unrelated", "run_id": "unrelated-run"},
    )
    assert not rejected["ok"]


async def test_plan_configuration_rejects_run_without_thread_identity(
    running_service,
):
    service = running_service
    startup_ceiling(service)
    plan = await submit_plan(service, [task("a"), task("b")], maximum=1)
    rejected = await command(
        service,
        "plan.configure",
        {"objective_id": plan["objective_id"], "max_parallel": 2},
        origin={"run_id": "unidentified-run"},
    )
    assert not rejected["ok"]
    assert service.plans.describe(plan["objective_id"])["concurrency"]["requested"] == 1


@pytest.mark.parametrize("strict_alias_first", [False, True])
async def test_provider_aliases_share_the_strictest_instance_ceiling(
    running_service, strict_alias_first
):
    service = running_service
    startup_ceiling(service)
    plan = await submit_plan(service, [task("a"), task("b")])
    alias = ProviderConfig(
        instance_id="codex", models=["current"], billing_verified=True, max_executors=1
    )
    original = service.config.providers["codex"]
    entries = [("strict-alias", alias), ("codex", original)]
    if not strict_alias_first:
        entries.reverse()
    config = service.config.model_copy(deep=True, update={"providers": dict(entries)})
    restarted = Service(config, service.config_path)
    try:
        assert restarted.state.provider_capacity("codex")["limit"] == 1
        first = restarted.state.acquire_grant(
            plan["tasks"][0]["job_id"], "codex", "current", "coordinator", "run"
        )
        assert first
        assert (
            restarted.state.acquire_grant(
                plan["tasks"][1]["job_id"], "codex", "current", "coordinator", "run"
            )
            is None
        )
        assert restarted.state.capacity()["available"] == 1
        assert restarted.state.provider_capacity("codex")["available"] == 0
    finally:
        restarted.state.close()


async def test_plan_request_exposes_effective_single_slot_without_overpromising(
    running_service,
):
    service = running_service
    plan = await submit_plan(service, [task("a"), task("b")])
    assert plan["concurrency"]["requested"] == 2
    assert plan["concurrency"]["effective"] == 1
    assert plan["concurrency"]["limited"] is True
    remote = Dispatch()
    await service.dispatch(remote)
    assert not remote.sent[0][1].get("parallel_dispatches")


async def test_status_explains_scope_dependency_capacity_and_provider(
    running_service,
):
    service = running_service
    startup_ceiling(service)
    plan = await submit_plan(
        service,
        [
            task("a", paths=["src"]),
            task("conflict", paths=["src/child"]),
            task("independent", paths=["docs"]),
            task("review", dependencies=["a"], paths=[]),
        ],
    )
    first = await grant(service, plan["tasks"][0]["job_id"], "a")
    status = await current_status(service)
    described = status["plans"][0]
    by_id = {entry["id"]: entry for entry in described["tasks"]}
    assert set(described["dag_ready"]) == {"conflict", "independent"}
    assert described["ready"] == ["independent"]
    assert "scope_conflict" in {
        b["reason"] for b in by_id["conflict"]["admission"]["blockers"]
    }
    dependency = next(
        b
        for b in by_id["review"]["admission"]["blockers"]
        if b["reason"] == "dependencies_pending"
    )
    assert dependency["job_ids"] == [first["job_id"]]
    await command(service, "capacity.set", {"max_executors": 1})
    status = await current_status(service)
    assert not status["plans"][0]["ready"]
    blockers = status["plans"][0]["tasks"][2]["admission"]["blockers"]
    assert "service_capacity" in {b["reason"] for b in blockers}
    assert all(b["action"] for b in blockers)
    await command(service, "capacity.set", {"max_executors": 2})
    service.state.provider_limits["codex"] = 1
    status = await current_status(service)
    blockers = status["plans"][0]["tasks"][2]["admission"]["blockers"]
    assert "provider_capacity" in {b["reason"] for b in blockers}
    assert status["capacity"]["available"] == 1
    assert status["provider_capacity"]["codex"]["available"] == 0
    rejected = await reserve(service, plan["tasks"][2]["job_id"])
    assert not rejected["ok"] or rejected["data"]["state"] == "busy"
    assert len(service.state.leases()) == 1
    # Direct acquisition must retain the provider gate too, not only routing.
    assert (
        service.state.acquire_grant(
            plan["tasks"][2]["job_id"], "codex", "current", "coordinator", "run"
        )
        is None
    )


async def test_batch_two_then_review_only_after_both_reconciled_criteria(
    running_service,
):
    service = running_service
    startup_ceiling(service)
    plan = await submit_plan(
        service,
        [
            task("a"),
            task("b"),
            task("review", stage="review", dependencies=["a", "b"], paths=[]),
        ],
    )
    remote_dispatch = Dispatch()
    await service.dispatch(remote_dispatch)
    body = remote_dispatch.sent[0][1]
    assert body["job_id"] == plan["tasks"][0]["job_id"]
    assert [d["job_id"] for d in body["parallel_dispatches"]] == [
        plan["tasks"][1]["job_id"]
    ]
    attempts = [
        await grant(service, entry["job_id"], entry["id"])
        for entry in plan["tasks"][:2]
    ]
    assert (await report(service, attempts[0]))["ok"]
    invalid = await report(service, attempts[1], evidence={"summary": "done"})
    assert not invalid["ok"]
    remote = simulated_remote(attempts, completed={a["id"] for a in attempts})
    await Reconciler(service.state, remote).reconcile()
    assert service.state.lease(attempts[0]["id"]) is None
    assert service.state.lease(attempts[1]["id"])
    await service.dispatch(remote_dispatch)
    assert len(remote_dispatch.sent) == 1
    assert (await report(service, attempts[1]))["ok"]
    await Reconciler(service.state, remote).reconcile()
    service.after_execution()
    await service.dispatch(remote_dispatch)
    review = remote_dispatch.sent[-1][1]
    assert review["job_id"] == plan["tasks"][2]["job_id"]
    assert {p["task_id"] for p in review["task_context"]["dependency_evidence"]} == {
        "task-a",
        "task-b",
    }


async def test_batch_skips_writer_conflict_for_independent_companion(running_service):
    service = running_service
    startup_ceiling(service)
    plan = await submit_plan(
        service,
        [task("a", paths=["src"]), task("conflict", paths=["src/sub"]), task("b")],
    )
    remote = Dispatch()
    await service.dispatch(remote)
    assert [d["job_id"] for d in remote.sent[0][1]["parallel_dispatches"]] == [
        plan["tasks"][2]["job_id"]
    ]


async def test_batch_respects_provider_capacity_before_any_claim(running_service):
    service = running_service
    startup_ceiling(service)
    service.state.provider_limits["codex"] = 1
    await submit_plan(service, [task("a"), task("b")])
    remote = Dispatch()
    await service.dispatch(remote)
    assert len(remote.sent) == 1
    assert not remote.sent[0][1].get("parallel_dispatches")
    assert not service.state.leases()


async def test_status_exposes_dispatch_receipt_without_inventing_executor(
    running_service,
):
    service = running_service
    startup_ceiling(service)
    plan = await submit_plan(service, [task("a"), task("b")])
    remote = Dispatch()
    await service.dispatch(remote)
    status = await current_status(service)
    primary = status["plans"][0]["tasks"][0]
    assert primary["job_id"] == plan["tasks"][0]["job_id"]
    receipt = primary["admission"]["dispatch"]
    assert receipt["id"] == remote.sent[0][0]
    assert receipt["state"] == "sent"
    assert receipt["receipt"]["run_id"] == "dispatch-run"
    assert receipt["action"]
    assert not status["executors"]
    assert not primary["attempts"]


@pytest.mark.parametrize("claim_primary", [False, True])
async def test_batch_companion_shares_intent_and_never_dispatches_while_root_active(
    running_service, claim_primary
):
    service = running_service
    startup_ceiling(service)
    plan = await submit_plan(service, [task("a"), task("b")])

    class TraceDispatch(Dispatch):
        def __init__(self):
            super().__init__()
            self.raw = []
            self.terminal = False
            self.pending_children = False

        async def send(self, thread, message, key, **kwargs):
            self.raw.append((thread, message, key, kwargs))
            return await super().send(thread, message, key, **kwargs)

        async def read_thread(self, thread):
            result = snapshot(
                thread, "dispatch-run", "completed" if self.terminal else "running"
            )
            result["hasPendingChildRuns"] = self.pending_children
            return result

    remote = TraceDispatch()
    await service.dispatch(remote)
    primary_key = remote.sent[0][0]
    assert (
        remote.sent[0][1]["parallel_dispatches"][0]["job_id"]
        == plan["tasks"][1]["job_id"]
    )
    await service.dispatch(remote)
    assert len(remote.sent) == 1
    status = await current_status(service)
    companion = status["plans"][0]["tasks"][1]["admission"]["dispatch"]
    assert companion["id"] == primary_key
    assert companion["receipt"]["run_id"] == "dispatch-run"
    if claim_primary:
        await grant(service, plan["tasks"][0]["job_id"], "a")
    await service.dispatch(remote)
    assert len(remote.sent) == 1
    # Persistent membership must suppress a separate B dispatch after restart too.
    restarted = Service(service.config, service.config_path)
    restarted.catalog = service.catalog
    try:
        await restarted.dispatch(remote)
        assert len(remote.sent) == 1
        remote.terminal = True
        remote.pending_children = True
        await restarted.dispatch(remote)
        assert len(remote.sent) == 1
        remote.pending_children = False
        await restarted.dispatch(remote)
        assert len(remote.sent) == 2
        assert remote.raw[1][1] == remote.raw[0][1]
        assert remote.raw[1][2] == primary_key + ":retry:2"
    finally:
        restarted.state.close()
    companion = await grant(service, plan["tasks"][1]["job_id"], "b")
    duplicate = await reserve(service, companion["job_id"])
    assert duplicate["ok"], duplicate
    assert duplicate["data"]["state"] == "busy"
    assert (
        service.state.connection.execute(
            "SELECT count(*) FROM attempts WHERE job_id=?", (companion["job_id"],)
        ).fetchone()[0]
        == 1
    )


async def test_oversized_companion_does_not_block_small_primary(running_service):
    from oraculo.memory.transport import MAX_MESSAGE_UNITS, utf16_units

    service = running_service
    startup_ceiling(service)
    huge = task("huge")
    huge["brief"] = "x" * 16000
    huge["criteria"] = [
        {"id": f"criterion{i}", "description": "🔎" * 2000} for i in range(32)
    ]
    plan = await submit_plan(service, [task("small"), huge])

    class Measuring(Dispatch):
        async def send(self, thread, message, key, **kwargs):
            self.message_units = utf16_units(message)
            return await super().send(thread, message, key, **kwargs)

    remote = Measuring()
    await service.dispatch(remote)
    assert remote.sent[0][1]["job_id"] == plan["tasks"][0]["job_id"]
    assert not remote.sent[0][1].get("parallel_dispatches")
    assert remote.message_units <= MAX_MESSAGE_UNITS
    assert service.state.get_job(plan["tasks"][0]["job_id"])["state"] == "queued"


async def test_lost_batch_response_replays_identical_bytes_after_restart(
    running_service,
):
    service = running_service
    startup_ceiling(service)
    await submit_plan(service, [task("a"), task("b")])

    class Lost(Dispatch):
        async def send(self, thread, message, key, **kwargs):
            self.raw = (thread, message, key, kwargs)
            raise ConnectionError("simulated acceptance with lost reply")

    class Recovered(Dispatch):
        async def send(self, thread, message, key, **kwargs):
            self.raw = (thread, message, key, kwargs)
            return await super().send(thread, message, key, **kwargs)

    lost = Lost()
    with pytest.raises(ConnectionError):
        await service.dispatch(lost)
    restarted = Service(service.config, service.config_path)
    restarted.catalog = service.catalog
    try:
        recovered = Recovered()
        await restarted.dispatch(recovered)
        assert recovered.raw == lost.raw
        assert len(json.loads(recovered.raw[1])["parallel_dispatches"]) == 1
        assert len(restarted.state.list_jobs()) == 2
        assert restarted.state.leases() == []
    finally:
        restarted.state.close()


async def test_parallel_cancel_restart_retains_grants_until_every_run_stops(
    running_service,
):
    service = running_service
    startup_ceiling(service)
    plan = await submit_plan(
        service,
        [task("a"), task("b"), task("review", dependencies=["a", "b"], paths=[])],
    )
    attempts = [
        await grant(service, entry["job_id"], entry["id"])
        for entry in plan["tasks"][:2]
    ]
    cancelled = await command(
        service, "objective.cancel", {"objective_id": plan["objective_id"]}
    )
    assert cancelled["ok"], cancelled
    restarted = Service(service.config, service.config_path)
    restarted.catalog = service.catalog
    try:
        remote = simulated_remote(attempts)
        await Reconciler(restarted.state, remote).reconcile()
        assert len(restarted.state.leases()) == 2
        assert set(remote.interruptions) == {
            (a["thread_id"], a["run_id"]) for a in attempts
        }
        remote = simulated_remote(attempts, completed={attempts[0]["id"]})
        await Reconciler(restarted.state, remote).reconcile()
        assert len(restarted.state.leases()) == 1
        remote = simulated_remote(attempts, completed={a["id"] for a in attempts})
        await Reconciler(restarted.state, remote).reconcile()
        assert not restarted.state.leases()
        assert (
            restarted.state.get_objective(plan["objective_id"])["status"] == "stopped"
        )
        dispatch = Dispatch()
        await restarted.dispatch(dispatch)
        assert not dispatch.sent
    finally:
        restarted.state.close()


async def test_retry_cannot_duplicate_one_of_two_unreconciled_attempts(
    running_service,
):
    service = running_service
    startup_ceiling(service)
    plan = await submit_plan(service, [task("a"), task("b")])
    first, second = [
        await grant(service, entry["job_id"], entry["id"]) for entry in plan["tasks"]
    ]
    assert (
        await report(
            service,
            first,
            status="failed",
            evidence={
                "blocker": {
                    "reason": "test_failure",
                    "owner": "executor",
                    "action": "retry",
                }
            },
        )
    )["ok"]
    retry_payload = {"objective_id": plan["objective_id"], "task_id": "a"}
    rejected = await command(service, "task.retry", retry_payload)
    assert not rejected["ok"]
    assert len(service.state.leases()) == 2
    remote = simulated_remote([first, second], completed={first["id"]})
    await Reconciler(service.state, remote).reconcile()
    service.after_execution()
    assert service.state.lease(second["id"])
    accepted = await command(service, "task.retry", retry_payload)
    assert accepted["ok"], accepted
    replacement = await grant(service, first["job_id"], "retry-a")
    assert replacement["number"] == 2
    assert replacement["id"] != first["id"]
    assert service.state.lease(second["id"])
    assert len(service.state.leases()) == 2
