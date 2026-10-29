"""Legacy control dispatches do not authorize execution or prove work complete."""

import asyncio
import json

import pytest
from test_service import DispatchLifecycleRemote, Remote, command, snapshot

from oraculo.cli import make_envelope
from oraculo.reconcile import Reconciler
from oraculo.service import Service


async def exhaust(service, remote=None):
    submitted = await command(
        service, "objective.submit", {"text": "Synthetic queued work"}
    )
    remote = remote or DispatchLifecycleRemote()
    for _ in range(service.config.max_dispatch_attempts + 1):
        await service.dispatch(remote)
    return submitted["data"], remote


async def job_status(service, job_id):
    response = await command(service, "status.get", {})
    assert response["ok"], response
    return response["data"], next(
        j for j in response["data"]["jobs"] if j["id"] == job_id
    )


async def test_exhaustion_is_actionable_without_grant_or_work_success(running_service):
    service = running_service
    submitted, remote = await exhaust(service)
    status, job = await job_status(service, submitted["job"])
    assert status["health"] == "dispatch_attempts_exhausted"
    assert job["state"] == "queued"
    assert not job["admission"]["eligible"]
    assert job["admission"]["grant_eligible"]
    assert job["admission"]["blockers"][-1]["reason"] == "dispatch_attempts_exhausted"
    dispatch = job["admission"]["dispatch"]
    assert dispatch["reason"] == "dispatch_attempts_exhausted"
    assert dispatch["dispatch_attempts"] == service.config.max_dispatch_attempts
    assert dispatch["automatic_retry"] is False
    assert dispatch["observation"]["run_id"] == remote.snapshot["thread"]["latestRunId"]
    assert dispatch["observation"]["observed_at"] > 0
    assert "grant.acquire" in dispatch["action"]
    assert service.state.leases() == []
    assert (
        service.state.connection.execute("SELECT count(*) FROM attempts").fetchone()[0]
        == 0
    )
    assert service.state.get_objective(submitted["objective"])["status"] != "succeeded"


async def test_exhaustion_survives_capacity_drain_and_restart(running_service):
    service = running_service
    submitted, remote = await exhaust(service)
    other = await command(service, "objective.submit", {"text": "Other synthetic work"})
    service.state.set_capacity(1)
    assert service.state.acquire_grant(
        other["data"]["job"], "codex", "current", "coordinator", "other-run"
    )
    before = len(remote.calls)
    service.health = "online"  # A normal scheduler tick resets connection health.
    await service.dispatch(remote)
    status, job = await job_status(service, submitted["job"])
    assert status["capacity"]["available"] == 0
    assert status["health"] == "dispatch_attempts_exhausted"
    assert job["admission"]["dispatch"]["reason"] == "dispatch_attempts_exhausted"
    restarted = Service(service.config, service.config_path)
    restarted.catalog = service.catalog
    try:
        restarted.health = "online"
        assert restarted.health == "dispatch_attempts_exhausted"
        await restarted.dispatch(remote)
        assert len(remote.calls) == before
        # Read the reopened service over its real Unix API, not a cached dict.
        path = str(service.config.state_dir / "restarted.sock")
        server = await asyncio.start_unix_server(restarted.connection, path=path)
        async with server:
            reader, writer = await asyncio.open_unix_connection(path)
            writer.write(
                json.dumps(make_envelope("status.get", "alpha", {})).encode() + b"\n"
            )
            await writer.drain()
            observed = json.loads(await reader.readline())
            writer.close()
            await writer.wait_closed()
        assert observed["ok"]
        assert observed["data"]["health"] == "dispatch_attempts_exhausted"
    finally:
        restarted.state.close()


async def test_explicit_same_job_grant_recovers_without_replay_or_memory_duplication(
    running_service,
):
    service = running_service
    submitted, remote = await exhaust(service)
    outbox = [dict(r) for r in service.state.connection.execute("SELECT * FROM outbox")]
    cycles = service.memory_cycles.list("alpha")
    origin = {"thread_id": "coordinator", "run_id": "recovery-run"}
    grant = await command(
        service,
        "grant.acquire",
        {"job_id": submitted["job"]},
        origin=origin,
        request_id="same-job-recovery",
    )
    assert grant["ok"] and grant["data"]["state"] == "reserved", grant
    replay = await command(
        service,
        "grant.acquire",
        {"job_id": submitted["job"]},
        origin=origin,
        request_id="same-job-recovery",
    )
    assert replay == grant
    denied_duplicate = await command(
        service, "grant.acquire", {"job_id": submitted["job"]}, origin=origin
    )
    assert denied_duplicate["data"]["state"] == "busy"
    assert grant["data"]["attempt"]["job_id"] == submitted["job"]
    assert grant["data"]["attempt"]["number"] == 1
    assert len(service.state.leases()) == 1
    assert len(service.memory_cycles.list("alpha")) == len(cycles)
    assert grant["data"]["memory"]["id"] == cycles[0]["id"]
    assert (
        grant["data"]["context"]["artifact"]
        == json.loads(remote.calls[0][1])["context"]["artifact"]
    )
    await service.dispatch(remote)
    assert len(remote.calls) == service.config.max_dispatch_attempts
    assert [
        dict(r) for r in service.state.connection.execute("SELECT * FROM outbox")
    ] == outbox
    status, job = await job_status(service, submitted["job"])
    assert status["health"] == "online"
    assert job["state"] == "admitted"
    assert job["admission"]["dispatch"] is None

    attempt = grant["data"]["attempt"]
    claim = {
        "attempt_id": attempt["id"],
        "generation": attempt["generation"],
        "thread_id": "synthetic-child",
        "run_id": "synthetic-child-run",
        "task_id": "synthetic-task",
    }
    claimed = await command(
        service, "grant.claim", claim, origin=origin, request_id="recovery-claim"
    )
    assert claimed["ok"], claimed
    assert (
        await command(
            service, "grant.claim", claim, origin=origin, request_id="recovery-claim"
        )
        == claimed
    )
    report = {
        "attempt_id": attempt["id"],
        "generation": attempt["generation"],
        "run_id": claim["run_id"],
        "status": "succeeded",
        "evidence": {"checks": "synthetic success"},
    }
    receipt = await command(
        service,
        "execution.report",
        report,
        origin={"thread_id": claim["thread_id"], "run_id": claim["run_id"]},
        request_id="recovery-report",
    )
    assert receipt["ok"], receipt
    tree = Remote()
    tree.snapshots[claim["thread_id"]] = snapshot(
        claim["thread_id"], claim["run_id"], "completed"
    )
    assert (await Reconciler(service.state, tree).reconcile())["released"]
    service.after_execution()
    await service.memory_io("publish_pending")
    cycle = service.memory_cycles.get(cycles[0]["id"])
    assert cycle["write_receipt"]["state"] == "confirmed"
    head = service.publisher.git.head()
    service.after_execution()
    await service.memory_io("publish_pending")
    assert service.publisher.git.head() == head
    assert (
        service.memory_cycles.get(cycles[0]["id"])["write_receipt"]
        == cycle["write_receipt"]
    )
    assert (
        service.state.connection.execute("SELECT count(*) FROM attempts").fetchone()[0]
        == 1
    )
    assert len(service.memory_cycles.list("alpha")) == 1
    status, job = await job_status(service, submitted["job"])
    assert job["state"] == "succeeded"
    assert status["health"] == "online"


@pytest.mark.parametrize(
    "cause",
    ["wrong_owner", "wrong_project", "capacity", "maintenance", "scope", "provider"],
)
async def test_recovery_does_not_bypass_admission(running_service, cause):
    service = running_service
    submitted, remote = await exhaust(service)
    origin = {"thread_id": "coordinator", "run_id": "recovery-run"}
    payload = {"job_id": submitted["job"]}
    if cause == "wrong_owner":
        origin["thread_id"] = "unrelated"
    elif cause == "wrong_project":
        service.state.register_project(
            "beta", str(service.config.projects_root / "beta")
        )
        other = service.state.submit_objective("beta", "Other project")
        payload["job_id"] = service.state.enqueue_job(
            other["id"], "implementation", {}
        )["id"]
    elif cause in {"capacity", "scope"}:
        other = await command(service, "objective.submit", {"text": "Active writer"})
        assert service.state.acquire_grant(
            other["data"]["job"], "codex", "current", "coordinator", "other-run"
        )
        if cause == "capacity":
            service.state.set_capacity(1)
    elif cause == "maintenance":
        service.state.set_meta("runtime_maintenance", {"operation_id": "synthetic"})
    elif cause == "provider":
        service.catalog = {"providers": []}
    before = service.state.connection.execute(
        "SELECT count(*) FROM attempts"
    ).fetchone()[0]
    denied = await command(service, "grant.acquire", payload, origin=origin)
    assert not denied["ok"] or denied["data"]["state"] == "busy", denied
    assert (
        service.state.connection.execute("SELECT count(*) FROM attempts").fetchone()[0]
        == before
    )
    service.health = "online"
    assert service.health == "dispatch_attempts_exhausted"
    assert len(remote.calls) == service.config.max_dispatch_attempts


async def test_unknown_dispatch_cannot_be_masked_by_exhausted_job(running_service):
    service = running_service
    _, remote = await exhaust(service)
    second = await command(service, "objective.submit", {"text": "Second work"})
    await service.dispatch(remote)
    service.state.set_meta(f"dispatch_receipt:{remote.calls[-1][2]}", {})
    service.health = (
        "dispatch_attempts_exhausted"  # Last writer must not decide aggregate.
    )
    assert service.health == "dispatch_identity_unknown"
    status, job = await job_status(service, second["data"]["job"])
    assert status["health"] == "dispatch_identity_unknown"
    assert job["admission"]["dispatch"]["reason"] == "dispatch_identity_unknown"


@pytest.mark.parametrize("maximum", [1, 2, 3])
async def test_supported_dispatch_budget_is_never_reset(running_service, maximum):
    service = running_service
    service.config.max_dispatch_attempts = maximum
    submitted, remote = await exhaust(service)
    before = list(remote.calls)
    for _ in range(3):
        service.health = "online"
        await service.dispatch(remote)
    assert remote.calls == before
    assert len(remote.calls) == maximum
    _, job = await job_status(service, submitted["job"])
    assert job["admission"]["dispatch"]["dispatch_attempts"] == maximum
    assert job["admission"]["dispatch"]["automatic_retry"] is False


async def test_terminal_observation_is_historical_and_refresh_can_reveal_activity(
    running_service,
):
    service = running_service
    submitted, remote = await exhaust(service)
    remote.snapshot["thread"]["activeRunId"] = remote.snapshot["thread"]["latestRunId"]
    remote.snapshot["thread"]["status"] = "running"
    await service.dispatch(remote)
    status, job = await job_status(service, submitted["job"])
    assert status["health"] == "awaiting_coordinator"
    assert (
        job["admission"]["dispatch"]["observation"]["reason"] == "awaiting_coordinator"
    )
    assert len(remote.calls) == service.config.max_dispatch_attempts


async def test_cancelled_job_keeps_history_without_current_dispatch_failure(
    running_service,
):
    service = running_service
    submitted, _ = await exhaust(service)
    before = [dict(r) for r in service.state.connection.execute("SELECT * FROM outbox")]
    cancelled = await command(
        service, "objective.cancel", {"objective_id": submitted["objective"]}
    )
    assert cancelled["ok"]
    assert service.health == "online"
    assert [
        dict(r) for r in service.state.connection.execute("SELECT * FROM outbox")
    ] == before
    assert (
        service.state.get_meta(f"dispatch_observation:{before[-1]['id']}")["reason"]
        == "dispatch_attempts_exhausted"
    )


@pytest.mark.parametrize("receipt", [{}, {"run_id": 42}])
async def test_exhaustion_observation_cannot_cover_missing_or_changed_identity(
    running_service, receipt
):
    service = running_service
    submitted, remote = await exhaust(service)
    service.state.set_meta(f"dispatch_receipt:{remote.calls[-1][2]}", receipt)
    status, job = await job_status(service, submitted["job"])
    assert status["health"] == "dispatch_identity_unknown"
    assert job["admission"]["dispatch"]["observation"] is None
    assert job["admission"]["dispatch"]["automatic_retry"] is False
    await service.dispatch(remote)
    assert len(remote.calls) == service.config.max_dispatch_attempts


async def test_legacy_sent_dispatch_requires_fresh_terminal_observation(
    running_service,
):
    service = running_service
    submitted = await command(service, "objective.submit", {"text": "Legacy work"})
    remote = DispatchLifecycleRemote()
    # Before the last terminal read, all sends exist but exhaustion is unproven.
    for _ in range(service.config.max_dispatch_attempts):
        await service.dispatch(remote)
    service.health = "online"
    status, job = await job_status(service, submitted["data"]["job"])
    assert status["health"] == "awaiting_coordinator"
    assert job["admission"]["dispatch"]["observation"] is None
    await service.dispatch(remote)
    status, job = await job_status(service, submitted["data"]["job"])
    assert status["health"] == "dispatch_attempts_exhausted"
    assert job["admission"]["dispatch"]["observation"]["basis"] == "coordinator_read"


@pytest.mark.parametrize(
    "health",
    [
        "disconnected",
        "auth_required",
        "blocked_eligibility",
        "uncertain_coordinator_launch",
    ],
)
async def test_core_failure_takes_precedence_over_dispatch_exhaustion(
    running_service, health
):
    service = running_service
    submitted, _ = await exhaust(service)
    service.health = health
    status, job = await job_status(service, submitted["job"])
    assert status["health"] == health
    assert job["admission"]["dispatch"]["reason"] == "dispatch_attempts_exhausted"


@pytest.mark.parametrize(
    "reason",
    [
        "dispatch_attempts_exhausted",
        "awaiting_coordinator",
        "dispatch_identity_unknown",
    ],
)
def test_maintenance_requires_modern_exhaustion_observation(reason):
    from oraculo.runtime import RuntimeError
    from oraculo.service_control import legacy_dispatch_evidence

    dispatch = {
        "id": "dispatch:synthetic:0:retry:3",
        "state": "sent",
        "receipt": {"run_id": "synthetic-run"},
        "reason": reason,
        "observation": {"run_id": "synthetic-run", "basis": "coordinator_read"},
    }
    data = {"jobs": [{"id": "synthetic", "admission": {"dispatch": dispatch}}]}
    if reason == "dispatch_attempts_exhausted":
        assert len(legacy_dispatch_evidence(data, 3)) == 1
    else:
        with pytest.raises(RuntimeError, match="legacy_dispatch_unproven"):
            legacy_dispatch_evidence(data, 3)
    dispatch["reason"] = "dispatch_attempts_exhausted"
    dispatch["observation"]["run_id"] = "different-run"
    with pytest.raises(RuntimeError, match="legacy_dispatch_unproven"):
        legacy_dispatch_evidence(data, 3)


@pytest.mark.parametrize("terminal", [True, False])
async def test_legacy_restart_can_observe_sent_controls_at_zero_capacity(
    running_service, terminal
):
    service = running_service
    submitted = await command(
        service, "objective.submit", {"text": "Legacy queued work"}
    )
    remote = DispatchLifecycleRemote()
    for _ in range(service.config.max_dispatch_attempts):
        await service.dispatch(remote)
    key = remote.calls[-1][2]
    assert service.state.get_meta(f"dispatch_observation:{key}") is None
    other = await command(service, "objective.submit", {"text": "Active other writer"})
    assert service.state.acquire_grant(
        other["data"]["job"], "codex", "current", "coordinator", "other-run"
    )
    service.state.set_capacity(1)
    if not terminal:
        remote.snapshot["thread"]["activeRunId"] = remote.snapshot["thread"][
            "latestRunId"
        ]
        remote.snapshot["thread"]["status"] = "running"
    before = [
        dict(row) for row in service.state.connection.execute("SELECT * FROM outbox")
    ]
    restarted = Service(service.config, service.config_path)
    restarted.catalog = service.catalog
    try:
        restarted.health = "online"
        await restarted.dispatch(remote)
        expected = "dispatch_attempts_exhausted" if terminal else "awaiting_coordinator"
        assert restarted.health == expected
        assert (
            restarted.state.get_meta(f"dispatch_observation:{key}")["reason"]
            == expected
        )
        assert [
            dict(row)
            for row in restarted.state.connection.execute("SELECT * FROM outbox")
        ] == before
        assert len(remote.calls) == service.config.max_dispatch_attempts
        assert restarted.state.capacity()["available"] == 0
        assert (
            restarted.state.connection.execute(
                "SELECT count(*) FROM attempts"
            ).fetchone()[0]
            == 1
        )
    finally:
        restarted.state.close()
    status, job = await job_status(service, submitted["data"]["job"])
    assert job["state"] == "queued"
    assert job["admission"]["dispatch"]["observation"]["reason"] == expected


async def test_zero_capacity_observation_rechecks_job_after_remote_read(
    running_service,
):
    service = running_service
    submitted = await command(service, "objective.submit", {"text": "Queued work"})
    remote = DispatchLifecycleRemote()
    for _ in range(service.config.max_dispatch_attempts):
        await service.dispatch(remote)
    key = remote.calls[-1][2]
    other = await command(service, "objective.submit", {"text": "Active writer"})
    assert service.state.acquire_grant(
        other["data"]["job"], "codex", "current", "coordinator", "other-run"
    )
    service.state.set_capacity(1)
    remote.on_read = lambda: service.state.cancel_objective(
        submitted["data"]["objective"]
    )
    service.health = "online"
    await service.dispatch(remote)
    assert service.state.get_meta(f"dispatch_observation:{key}") is None
    assert service.health == "online"
    assert len(remote.calls) == service.config.max_dispatch_attempts
    assert service.state.capacity()["available"] == 0
