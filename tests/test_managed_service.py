"""Real socket/Git/memory/ledger; explicitly simulated T3 responses."""

import fcntl
import os

import pytest
from test_managed_tasks import Host, initialize, report

from oraculo import maintenance
from oraculo.cli import make_envelope, request
from oraculo.managed_service import command
from oraculo.state import State, StateError


class Caller(Host):
    async def call(self, name, args):
        result = await super().call(name, args)
        if name == "orchestrator_capabilities":
            result["inheritedProviderInstanceId"] = "codex"
        return result


async def test_service_authority_caller_relay_plan_gate_and_dedupe(running_service):
    service = running_service
    initialize(service.config.projects_root / "alpha")
    host = Caller(service.config.projects_root)
    payload = dict(
        kind="work",
        key="same",
        text="Plan only",
        deliverable_kind="plan",
        cwd=str(service.config.projects_root / "alpha"),
    )
    try:
        first = await command(service.config, "start", payload, host)
        assert first["phase"] == "executing", first
        second = await command(service.config, "start", payload, host)
        assert second["id"] == first["id"] and len(host.created) == 1
        assert service.state.capacity()["occupied"] == 1
        assert first["admission"]["state"] == "executing"
        assert first["admission_at_launch"]["state"] == "reserved"
        fd = os.open(service.config.state_dir / "managed-tasks.lock", os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)
        host.complete(first["child"]["childThreadId"])
        final = await command(
            service.config,
            "finish",
            dict(
                task_id=first["id"],
                report=dict(
                    report(first), deliverable_kind="plan", approval_state="pending"
                ),
            ),
            host,
        )
        assert final["phase"] == "awaiting_approval", final
        assert service.state.capacity()["occupied"] == 0
        assert final["admission"]["state"] == "not_reserved"
        assert final["admission_at_launch"]["state"] == "reserved"
        denied = await command(
            service.config, "publish", dict(task_id=first["id"], publication={}), host
        )
        assert denied["blocked_reason"] == "plan_not_publishable"
        read = await command(service.config, "status", {"task_id": first["id"]}, None)
        assert read["source"] == "service"
    finally:
        if service.managed_service:
            service.managed_service.close()
            service.managed_service = None


def test_cross_entry_capacity_maintenance_and_restart(tmp_path):
    path = tmp_path / "state.db"
    state = State(path, max_executors=3)
    for i in range(3):
        state.reserve_managed(str(i), "codex")
    assert state.capacity()["available"] == 0
    with pytest.raises(StateError, match="capacity_wait"):
        state.reserve_managed("fourth", "codex")
    with pytest.raises(StateError, match="active_or_uncertain"):
        maintenance.begin(state, "maintenance")
    state.close()
    state = State(path, max_executors=3)
    assert state.capacity()["occupied"] == 3
    for i in range(3):
        state.release_managed(
            str(i), {"workState": "result_available", "hasPendingChildRuns": False}
        )
    token = maintenance.begin(state, "maintenance")["token"]
    with pytest.raises(StateError, match="admission_closed"):
        state.reserve_managed("new", "codex")
    with pytest.raises(StateError, match="token_mismatch"):
        maintenance.end(state, "maintenance", "wrong")
    maintenance.end(state, "maintenance", token)
    assert state.capacity()["available"] == 3
    state.close()


async def test_maintenance_and_runtime_identity_socket(running_service):
    config = running_service.config
    response = await request(
        config, make_envelope("maintenance.begin", "*", {"operation_id": "upgrade"})
    )
    assert response["ok"], response
    receipt = response["data"]
    assert receipt["admission_closed"] and receipt["quiescent"]
    state = await request(config, make_envelope("status.get", "*", {}))
    assert state["data"]["runtime"]["pid"] == os.getpid()
    done = await request(
        config,
        make_envelope(
            "maintenance.end",
            "*",
            {"operation_id": "upgrade", "token": receipt["token"]},
        ),
    )
    assert done["ok"] and not done["data"]["admission_closed"]
    again = await request(
        config, make_envelope("maintenance.begin", "*", {"operation_id": "upgrade"})
    )
    assert again["ok"] and again["data"]["admission_closed"]
    assert again["data"]["token"] == receipt["token"]


def test_completed_maintenance_recovery_checks_new_work_and_operation(tmp_path):
    state = State(tmp_path / "state.db", max_executors=3)
    try:
        token = maintenance.begin(state, "first")["token"]
        maintenance.end(state, "first", token)
        state.reserve_managed("new-work", "codex")
        with pytest.raises(StateError, match="work_active_or_uncertain"):
            maintenance.begin(state, "first")
        assert state.get_meta("runtime_maintenance") is None
        state.release_managed(
            "new-work", {"workState": "result_available", "hasPendingChildRuns": False}
        )
        other = maintenance.begin(state, "second")["token"]
        assert other != token
        with pytest.raises(StateError, match="already_active"):
            maintenance.begin(state, "first")
        with pytest.raises(StateError, match="token_mismatch"):
            maintenance.end(state, "second", token)
        maintenance.end(state, "second", other)
        assert maintenance.begin(state, "first")["token"] != token
    finally:
        state.close()


async def test_scheduler_and_managed_share_three_slots(running_service):
    from test_operational_parallelism import reserve, startup_ceiling, submit_plan
    from test_plan_service import task

    service = running_service
    startup_ceiling(service, 3)
    service.state.provider_limits["codex"] = 3
    plan = await submit_plan(service, [task("a"), task("b")], maximum=3)
    service.state.reserve_managed("external-1", "codex")
    service.state.reserve_managed("external-2", "codex")
    first = await reserve(service, plan["tasks"][0]["job_id"])
    assert first["data"]["state"] == "reserved", first
    assert service.state.capacity()["occupied"] == 3
    next_attempt = await reserve(service, plan["tasks"][1]["job_id"])
    assert not next_attempt.get("ok") or next_attempt["data"]["state"] != "reserved", (
        next_attempt
    )
    assert service.state.capacity()["occupied"] == 3
    with pytest.raises(StateError, match="capacity_wait"):
        service.state.reserve_managed("fourth", "codex")


async def test_approval_bound_to_origin_and_immutable_plan(running_service):
    service = running_service
    initialize(service.config.projects_root / "alpha")
    host = Caller(service.config.projects_root)
    payload = dict(
        kind="work",
        key="plan",
        text="Plan",
        deliverable_kind="plan",
        cwd=str(service.config.projects_root / "alpha"),
    )
    first = await command(service.config, "start", payload, host)
    host.complete(first["child"]["childThreadId"])
    evidence = dict(report(first), deliverable_kind="plan", approval_state="pending")
    final = await command(
        service.config, "finish", dict(task_id=first["id"], report=evidence), host
    )
    assert final["phase"] == "awaiting_approval"
    wrong = await command(
        service.config,
        "approve",
        dict(task_id=first["id"], commit="wrong", evidence="User approval"),
        host,
    )
    assert wrong["phase"] == "blocked"
    approved = await command(
        service.config,
        "approve",
        dict(
            task_id=first["id"],
            commit=evidence["commit"],
            evidence="User approval for this plan",
        ),
        host,
    )
    assert approved["phase"] == "plan_approved"
    assert service.managed_service.store.get(first["id"])["report"] == evidence
    resumed = await command(service.config, "resume", {"task_id": first["id"]}, host)
    assert resumed["phase"] == "plan_approved"
    implementation = await command(
        service.config,
        "start",
        dict(
            payload,
            key="implementation",
            deliverable_kind="implementation",
            approved_plan={"task_id": first["id"], "commit": evidence["commit"]},
        ),
        host,
    )
    assert implementation["phase"] == "executing", implementation
    assert implementation["id"] != first["id"] and len(host.created) == 2


async def test_uncertain_host_launch_retains_reservation_and_same_intent(
    running_service,
):
    service = running_service
    initialize(service.config.projects_root / "alpha")
    host = Caller(service.config.projects_root)
    host.fail_delegate_once = True
    payload = dict(
        kind="work",
        key="same",
        text="Implement",
        cwd=str(service.config.projects_root / "alpha"),
    )
    first = await command(service.config, "start", payload, host)
    assert first["phase"] == "blocked"
    assert service.state.capacity()["occupied"] == 1
    second = await command(service.config, "start", payload, host)
    assert second["id"] == first["id"] and len(host.created) == 1
    assert second["phase"] == "executing", second


def test_offline_guard_unknown_and_active_state(service_config):
    config, _ = service_config
    with pytest.raises(StateError, match="state_missing"):
        with maintenance.maintenance_guard(config, "upgrade"):
            pass
    state = State(config.db_path, max_executors=3)
    state.reserve_managed("unknown", "codex")
    state.close()
    with pytest.raises(StateError, match="active_or_uncertain"):
        with maintenance.maintenance_guard(config, "upgrade"):
            pass
    state = State(config.db_path, max_executors=3)
    state.release_managed(
        "unknown", {"workState": "result_available", "hasPendingChildRuns": False}
    )
    state.close()
    with maintenance.maintenance_guard(config, "upgrade") as receipt:
        assert receipt["quiescent"] and receipt["admission_closed"]
    assert (
        maintenance.end_maintenance(config, "upgrade", receipt["token"])[
            "admission_closed"
        ]
        is False
    )
