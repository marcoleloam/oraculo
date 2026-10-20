import json
import subprocess
import sys

import pytest
from test_memory_schema import proposal
from test_reconcile import Remote
from test_reconcile import snapshot as unit_snapshot

from oraculo.cli import make_envelope, request
from oraculo.reconcile import Reconciler


def snapshot(*args, **kwargs):
    result = unit_snapshot(*args, **kwargs)
    result["thread"]["projectId"] = "alpha"
    return result


async def command(service, kind, payload, *, origin=None, request_id=None):
    envelope = make_envelope(kind, "alpha", payload)
    envelope["origin"] = origin or {}
    if request_id:
        envelope["request_id"] = request_id
    return await request(service.config, envelope)


async def acquire(service):
    submitted = await command(
        service, "objective.submit", {"text": "Implement isolated change"}
    )
    granted = await command(
        service,
        "grant.acquire",
        {"job_id": submitted["data"]["job"]},
        origin={"thread_id": "coordinator", "run_id": "coordinator-run"},
    )
    assert granted["ok"], granted
    attempt = granted["data"]["attempt"]
    claimed = await command(
        service,
        "grant.claim",
        {
            "attempt_id": attempt["id"],
            "generation": attempt["generation"],
            "thread_id": "child",
            "run_id": "run",
            "task_id": "task",
        },
        origin={"thread_id": "coordinator", "run_id": "coordinator-run"},
    )
    assert claimed["ok"], claimed
    return submitted["data"], attempt


async def test_billing_options_follow_selected_model_catalog(running_service):
    service = running_service
    service.catalog = {
        "providers": [
            {
                "providerInstanceId": "custom-claude",
                "driverKind": "claudeAgent",
                "models": [
                    {"id": "sonnet", "options": [{"id": "effort", "type": "select"}]},
                    {
                        "id": "opus",
                        "options": [
                            {"id": "fastMode", "type": "boolean", "currentValue": True}
                        ],
                    },
                ],
            },
            {
                "providerInstanceId": "custom-codex",
                "driverKind": "codex",
                "models": [
                    {
                        "id": "current",
                        "options": [
                            {
                                "id": "serviceTier",
                                "type": "select",
                                "currentValue": "priority",
                                "options": [{"id": "default"}, {"id": "priority"}],
                            }
                        ],
                    }
                ],
            },
        ]
    }
    assert service.target_options("custom-claude", "sonnet") == {}
    assert service.target_options("custom-claude", "opus") == {"fastMode": False}
    assert service.target_options("custom-codex", "current") == {
        "serviceTier": "default"
    }


async def test_submit_dispatch_execute_reconcile_queues_memory(running_service):
    service = running_service
    submitted = await command(
        service,
        "objective.submit",
        {"text": "Implement isolated change"},
        request_id="human-1",
    )

    class DispatchRemote(Remote):
        def __init__(self):
            super().__init__()
            self.sent = []

        async def send(self, thread, message, key, **kwargs):
            self.sent.append((thread, json.loads(message), key))

    remote = DispatchRemote()
    await service.dispatch(remote)
    await service.dispatch(remote)
    assert len(remote.sent) == 1
    assert remote.sent[0][1]["job_id"] == submitted["data"]["job"]
    grant = await command(
        service,
        "grant.acquire",
        {"job_id": submitted["data"]["job"]},
        origin={"thread_id": "coordinator", "run_id": "coordinator-run"},
    )
    attempt = grant["data"]["attempt"]
    assert grant["data"]["target"]["model"] == "current"
    claim = {
        "attempt_id": attempt["id"],
        "generation": attempt["generation"],
        "thread_id": "child",
        "run_id": "run",
        "task_id": "task",
    }
    assert (
        await command(
            service,
            "grant.claim",
            claim,
            origin={"thread_id": "coordinator", "run_id": "coordinator-run"},
        )
    )["ok"]
    report = {
        "attempt_id": attempt["id"],
        "generation": attempt["generation"],
        "run_id": "run",
        "status": "succeeded",
        "evidence": {"checks": "passed", "commit": "fixture-commit"},
    }
    receipt = await command(
        service,
        "execution.report",
        report,
        origin={"thread_id": "child", "run_id": "run"},
        request_id="completed-report",
    )
    assert receipt["ok"]
    assert (
        await command(
            service,
            "execution.report",
            report,
            origin={"thread_id": "child", "run_id": "run"},
            request_id="completed-report",
        )
        == receipt
    )
    assert service.state.lease()
    remote.snapshots["child"] = snapshot("child", "run", "completed")
    assert (await Reconciler(service.state, remote).reconcile())["released"]
    service.after_execution()
    service.after_execution()
    memory = [j for j in service.state.list_jobs() if j["kind"] == "memory"]
    assert len(memory) == 1
    evidence = json.loads(memory[0]["payload"])
    assert evidence["provenance"]["run_id"] == "run"
    assert evidence["execution_evidence"]["evidence"]["checks"] == "passed"


async def test_request_dedupe_and_reused_id_rejection(running_service):
    service = running_service
    one = await command(service, "objective.submit", {"text": "one"}, request_id="same")
    assert (
        await command(service, "objective.submit", {"text": "one"}, request_id="same")
        == one
    )
    assert not (
        await command(
            service, "objective.submit", {"text": "different"}, request_id="same"
        )
    )["ok"]
    assert len(service.state.list_objectives()) == 1


async def test_memory_dispatch_replays_persisted_payload_after_restart(running_service):
    from oraculo.service import Service

    service = running_service
    submitted = await command(
        service,
        "objective.submit",
        {
            "text": "Preserve execution evidence",
            "kind": "memory",
            "memory_operation": "ingest",
            "execution_evidence": {"status": "succeeded", "checks": "passed"},
        },
    )
    assert submitted["ok"]

    class LostResponse:
        def __init__(self):
            self.calls = []

        async def send(self, thread, message, key, **kwargs):
            self.calls.append((thread, message, key, kwargs))
            if len(self.calls) == 1:
                raise ConnectionError("response lost after remote acceptance")

    remote = LostResponse()
    with pytest.raises(ConnectionError):
        await service.dispatch(remote)
    key = remote.calls[0][2]
    assert service.state._one("SELECT state FROM outbox WHERE id=?", (key,)) == {
        "state": "pending"
    }
    restarted = Service(service.config, service.config_path)
    restarted.catalog = service.catalog
    try:
        await restarted.dispatch(remote)
        assert remote.calls[1] == remote.calls[0]
        assert restarted.state._one("SELECT state FROM outbox WHERE id=?", (key,)) == {
            "state": "sent"
        }
        await restarted.dispatch(remote)
        assert len(remote.calls) == 2
    finally:
        restarted.state.close()


async def test_old_catalog_model_cannot_acquire_grant(running_service):
    service = running_service
    submitted = await command(service, "objective.submit", {"text": "one"})
    service.catalog = {
        "providers": [{"providerInstanceId": "codex", "models": ["retired"]}]
    }
    result = await command(
        service,
        "grant.acquire",
        {"job_id": submitted["data"]["job"]},
        origin={"thread_id": "coordinator", "run_id": "r"},
    )
    assert not result["ok"]
    assert service.state.lease() is None


@pytest.mark.parametrize(
    "field,value",
    [("attempt_id", "unknown"), ("run_id", "old-run"), ("generation", 999)],
)
async def test_execution_report_rejects_wrong_identity(running_service, field, value):
    service = running_service
    _, attempt = await acquire(service)
    report = {
        "attempt_id": attempt["id"],
        "generation": attempt["generation"],
        "run_id": "run",
        "status": "succeeded",
        "evidence": {"checks": "passed"},
    }
    report[field] = value
    assert not (
        await command(
            service,
            "execution.report",
            report,
            origin={"thread_id": "child", "run_id": "run"},
        )
    )["ok"]
    assert service.state.get_meta(f"execution_report:{attempt['id']}") is None
    assert service.state.lease()


async def test_cancel_and_status_bypass_occupied_executor(running_service):
    service = running_service
    objective, attempt = await acquire(service)
    cancelled = await command(
        service, "objective.cancel", {"objective_id": objective["objective"]}
    )
    assert cancelled["ok"]
    status = await command(service, "status.get", {})
    assert status["ok"] and status["data"]["health"] == "disconnected"
    assert service.state.lease()["attempt_id"] == attempt["id"]
    assert (
        service.state.get_objective(objective["objective"])["status"]
        == "cancel_requested"
    )
    stale = await command(
        service,
        "grant.claim",
        {
            "attempt_id": attempt["id"],
            "generation": attempt["generation"],
            "thread_id": "child",
            "run_id": "run",
        },
    )
    assert not stale["ok"]


async def test_review_offline_imports_once_without_network(running_service):
    service = running_service
    service.publisher.publish(proposal())
    opened = await command(service, "review.open", {}, request_id="review-open")
    assert opened["ok"], opened
    assert await command(service, "review.open", {}, request_id="review-open") == opened
    data = opened["data"]
    target = next(
        (service.config.vault_root / data["wiki_folder"]).glob("concepts/*.md")
    )
    target.write_text(target.read_text() + "\nHuman review addition.\n")
    closed = await command(
        service,
        "review.close",
        {"session_id": data["session_id"]},
        request_id="review-close",
    )
    assert closed["data"]["state"] == "imported", closed
    head = service.publisher.git.head()
    assert (
        await command(
            service,
            "review.close",
            {"session_id": data["session_id"]},
            request_id="review-close",
        )
        == closed
    )
    assert service.publisher.git.head() == head
    canonical = service.config.vault_root / proposal()["changes"][0]["path"]
    assert "Human review addition." in canonical.read_text()


@pytest.mark.parametrize("checks_pass", [True, False])
async def test_delivery_checks_and_real_destination_verifier(
    running_service, checks_pass
):
    service = running_service
    _, attempt = await acquire(service)
    root = service.config.projects_root / "alpha"
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    destination = root / "destination.json"
    destination.write_text(json.dumps({"commit": "old"}))
    payload = {
        "operation_id": "deploy-test",
        "attempt_id": attempt["id"],
        "generation": attempt["generation"],
        "kind": "deploy",
        "expected_state": {
            "commit": "new",
            "target": "local-test",
            "artifact": "fixture",
        },
        "cwd": str(root),
        "checks_argv": [
            [sys.executable, "-c", f"import sys; sys.exit({0 if checks_pass else 1})"]
        ],
        "verifier_argv": [
            sys.executable,
            "-c",
            "from pathlib import Path; print(Path('destination.json').read_text())",
        ],
    }
    result = await command(
        service,
        "operation.prepare",
        payload,
        origin={"thread_id": "child", "run_id": "run"},
    )
    assert result["ok"] is checks_pass, result
    if not checks_pass:
        assert (
            service.state.connection.execute(
                "SELECT count(*) FROM operations"
            ).fetchone()[0]
            == 0
        )
        return
    report = {"operation_id": "deploy-test", "attempt_id": attempt["id"]}
    mismatch = await command(service, "operation.report", report)
    assert mismatch["data"]["state"] != "confirmed"
    for health in ({}, {"healthy": False}):
        destination.write_text(
            json.dumps(
                {
                    "commit": "new",
                    "target": "local-test",
                    "artifact": "fixture",
                    **health,
                }
            )
        )
        assert (await command(service, "operation.report", report))["data"][
            "state"
        ] != "confirmed"
    destination.write_text(
        json.dumps(
            {
                "commit": "new",
                "target": "local-test",
                "artifact": "fixture",
                "healthy": True,
            }
        )
    )
    confirmed = await command(service, "operation.report", report)
    assert confirmed["data"]["state"] == "confirmed"
    assert not (
        await command(
            service,
            "operation.prepare",
            payload,
            origin={"thread_id": "child", "run_id": "run"},
        )
    )["ok"]


@pytest.mark.parametrize(
    "origin",
    [
        {},
        {"thread_id": "intruder", "run_id": "run"},
        {"thread_id": "child", "run_id": "old"},
    ],
)
async def test_execution_report_rejects_unowned_origin(running_service, origin):
    service = running_service
    _, attempt = await acquire(service)
    report = {
        "attempt_id": attempt["id"],
        "generation": attempt["generation"],
        "run_id": "run",
        "status": "succeeded",
        "evidence": {"checks": "passed"},
    }
    result = await command(service, "execution.report", report, origin=origin)
    assert not result["ok"], result
    assert service.state.get_meta(f"execution_report:{attempt['id']}") is None


async def test_execution_report_is_immutable_and_replayable(running_service):
    service = running_service
    _, attempt = await acquire(service)
    report = {
        "attempt_id": attempt["id"],
        "generation": attempt["generation"],
        "run_id": "run",
        "status": "succeeded",
        "evidence": {"checks": "passed"},
    }
    origin = {"thread_id": "child", "run_id": "run"}
    assert (await command(service, "execution.report", report, origin=origin))["ok"]
    assert (await command(service, "execution.report", report, origin=origin))["ok"]
    conflict = dict(report, status="failed", evidence={"checks": "failed"})
    assert not (await command(service, "execution.report", conflict, origin=origin))[
        "ok"
    ]
    assert service.state.get_meta(f"execution_report:{attempt['id']}") == report


async def test_concurrent_operation_prepare_authorizes_effect_only_once(
    running_service, monkeypatch
):
    import asyncio

    service = running_service
    _, attempt = await acquire(service)
    root = service.config.projects_root / "alpha"
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    original = service.run_command
    checks_entered = 0
    both_entered = asyncio.Event()
    check = [sys.executable, "-c", "print('checks passed')"]

    async def synchronized_check(argv, cwd):
        nonlocal checks_entered
        if argv == check:
            checks_entered += 1
            if checks_entered == 2:
                both_entered.set()
            await asyncio.wait_for(both_entered.wait(), 2)
        return await original(argv, cwd)

    monkeypatch.setattr(service, "run_command", synchronized_check)
    payload = {
        "operation_id": "contended-deploy",
        "attempt_id": attempt["id"],
        "generation": attempt["generation"],
        "kind": "deploy",
        "expected_state": {"commit": "new", "target": "test", "artifact": "a"},
        "cwd": str(root),
        "checks_argv": [check],
        "verifier_argv": [sys.executable, "-c", "print('{}')"],
    }
    origin = {"thread_id": "child", "run_id": "run"}
    ids = ["prepare-one", "prepare-two"]
    responses = await asyncio.gather(
        *(
            command(
                service, "operation.prepare", payload, origin=origin, request_id=key
            )
            for key in ids
        )
    )
    authorized = [
        response
        for response in responses
        if response.get("data", {}).get("effect_authorized_once")
    ]
    assert checks_entered == 2
    assert len(authorized) == 1, responses
    assert (
        service.state.connection.execute("SELECT count(*) FROM operations").fetchone()[
            0
        ]
        == 1
    )
    replay = await command(
        service,
        "operation.prepare",
        payload,
        origin=origin,
        request_id=authorized[0]["request_id"],
    )
    assert not replay.get("data", {}).get("effect_authorized_once"), replay
    assert checks_entered == 2


async def reserve(service, payload=None):
    submitted = await command(service, "objective.submit", payload or {"text": "task"})
    result = await command(
        service,
        "grant.acquire",
        {"job_id": submitted["data"]["job"]},
        origin={"thread_id": "coordinator", "run_id": "coordinator-run"},
    )
    assert result["ok"], result
    return submitted["data"], result["data"]["attempt"]


async def test_cancel_before_claim_binds_child_without_authorizing(running_service):
    service = running_service
    objective, attempt = await reserve(service)
    assert (
        await command(
            service, "objective.cancel", {"objective_id": objective["objective"]}
        )
    )["ok"]
    bound = await command(
        service,
        "grant.claim",
        {
            "attempt_id": attempt["id"],
            "generation": attempt["generation"],
            "thread_id": "child",
            "run_id": "run",
            "task_id": "task",
        },
        origin={"thread_id": "coordinator", "run_id": "coordinator-run"},
    )
    assert bound["ok"], bound
    assert bound["data"]["effect_authorized"] is False
    assert bound["state"] == "cancel_requested"
    assert service.state.get_attempt(attempt["id"])["thread_id"] == "child"
    remote = Remote()
    remote.snapshots["child"] = snapshot("child", "run", "running")
    assert (await Reconciler(service.state, remote).reconcile())[
        "state"
    ] == "cancel_requested"
    assert remote.interruptions == [("child", "run")]
    assert service.state.lease()
    remote.snapshots["child"] = snapshot("child", "run", "interrupted")
    assert (await Reconciler(service.state, remote).reconcile())["released"]


async def test_definitive_unstarted_report_releases_grant(running_service):
    service = running_service
    _, attempt = await reserve(service)
    payload = {
        "attempt_id": attempt["id"],
        "generation": attempt["generation"],
        "status": "not_started",
        "evidence": {
            "definitive_no_child": True,
            "error_kind": "validation",
            "client_request_id": "delegate-one",
            "tool_error": "model not supported; rejected before creation",
        },
    }
    origin = {"thread_id": "coordinator", "run_id": "coordinator-run"}
    wrong = await command(
        service,
        "execution.report",
        payload,
        origin={"thread_id": "intruder", "run_id": "r"},
    )
    assert not wrong["ok"]
    assert service.state.lease()
    result = await command(service, "execution.report", payload, origin=origin)
    assert result["ok"] and result["state"] == "failed", result
    assert service.state.lease() is None


@pytest.mark.parametrize(
    "kind,scope,source,allowed",
    [
        ("memory", "shared", "Generalized learning without private data.", True),
        ("memory", "alpha", "Generalized learning without private data.", False),
        (
            "implementation",
            "shared",
            "Generalized learning without private data.",
            False,
        ),
        ("memory", "shared", "Copied [[projects/alpha/wiki/private]]", False),
    ],
)
async def test_scoped_shared_promotion(
    running_service, tmp_path, kind, scope, source, allowed
):
    import hashlib

    from test_memory_schema import page

    from oraculo.cli import stage_proposal

    service = running_service
    _, attempt = await reserve(
        service,
        {
            "text": "promote learning",
            "kind": kind,
            "memory_scope": scope,
            "memory_operation": "ingest",
            "execution_evidence": {"learning": source},
        },
    )
    assert (
        await command(
            service,
            "grant.claim",
            {
                "attempt_id": attempt["id"],
                "generation": attempt["generation"],
                "thread_id": "child",
                "run_id": "run",
            },
            origin={"thread_id": "coordinator", "run_id": "coordinator-run"},
        )
    )["ok"]
    sha = hashlib.sha256(source.encode()).hexdigest()
    raw_path = f"shared/raw/{sha}.md"
    inner = proposal(
        page(project="shared", sources=[f"[[{raw_path}]]"]), project="shared"
    )
    inner["source_hashes"] = {raw_path: sha}
    bundle = tmp_path / "shared.json"
    bundle.write_text(
        json.dumps({"scope": "shared", "sanitized_source": source, "proposal": inner})
    )
    payload = dict(
        stage_proposal(service.config, bundle),
        attempt_id=attempt["id"],
        generation=attempt["generation"],
    )
    result = await command(
        service,
        "memory.propose",
        payload,
        origin={"thread_id": "child", "run_id": "run"},
    )
    assert result["ok"] is allowed, result
    canonical = service.config.vault_root / inner["changes"][0]["path"]
    if allowed:
        assert canonical.exists()
        assert (service.config.vault_root / raw_path).read_text() == source
    else:
        assert not canonical.exists()
        assert not (service.config.vault_root / raw_path).exists()


async def test_shared_review_via_service_without_registered_shared_project(
    running_service,
):
    from test_memory_schema import page

    from oraculo.protocol import Envelope

    service = running_service
    inner = proposal(page(project="shared"), project="shared")
    service.publisher.publish(inner)
    assert service.state.get_project("shared") is None
    opened = await service.handle(
        Envelope.model_validate(make_envelope("review.open", "shared", {}))
    )
    target = next(
        (service.config.vault_root / opened["data"]["wiki_folder"]).glob(
            "concepts/*.md"
        )
    )
    target.write_text(target.read_text() + "\nGeneralized reviewed learning.\n")
    closed = await service.handle(
        Envelope.model_validate(
            make_envelope(
                "review.close", "shared", {"session_id": opened["data"]["session_id"]}
            )
        )
    )
    assert closed["data"]["state"] == "imported", closed
    assert (
        "Generalized reviewed learning."
        in (service.config.vault_root / inner["changes"][0]["path"]).read_text()
    )


class DispatchLifecycleRemote:
    def __init__(self):
        self.calls = []
        self.snapshot = None
        self.on_read = None

    async def send(self, thread, message, key, **kwargs):
        self.calls.append((thread, message, key))
        run = f"dispatch-run-{len(self.calls)}"
        self.snapshot = snapshot(thread, run, "completed")
        return {"runId": run}

    async def read_thread(self, thread):
        if self.on_read:
            self.on_read()
        return self.snapshot


async def test_dispatch_retry_after_restart_is_bounded_and_preserves_payload(
    running_service,
):
    from oraculo.service import Service

    service = running_service
    await command(service, "objective.submit", {"text": "first"})
    remote = DispatchLifecycleRemote()
    await service.dispatch(remote)
    restarted = Service(service.config, service.config_path)
    restarted.catalog = service.catalog
    try:
        for _ in range(service.config.max_dispatch_attempts + 2):
            await restarted.dispatch(remote)
        assert len(remote.calls) == service.config.max_dispatch_attempts
        assert len({call[1] for call in remote.calls}) == 1
        assert [call[2] for call in remote.calls] == [
            remote.calls[0][2],
            remote.calls[0][2] + ":retry:2",
            remote.calls[0][2] + ":retry:3",
        ]
        assert restarted.health == "dispatch_attempts_exhausted"
        await command(service, "objective.submit", {"text": "second"})
        await restarted.dispatch(remote)
        assert len(remote.calls) == service.config.max_dispatch_attempts + 1
        assert json.loads(remote.calls[-1][1])["payload"]["text"] == "second"
    finally:
        restarted.state.close()


@pytest.mark.parametrize(
    "condition", ["active", "pending", "new_run", "children", "legacy"]
)
async def test_dispatch_does_not_retry_unproven_terminal_run(
    running_service, condition
):
    service = running_service
    await command(service, "objective.submit", {"text": "first"})
    remote = DispatchLifecycleRemote()
    await service.dispatch(remote)
    thread = remote.snapshot["thread"]
    if condition == "active":
        thread["activeRunId"] = thread["latestRunId"]
    elif condition == "pending":
        thread["pendingRequestCount"] = 1
    elif condition == "new_run":
        thread["latestRunId"] = "unrelated-run"
    elif condition == "children":
        remote.snapshot["hasPendingChildRuns"] = True
    else:
        service.state.set_meta(f"dispatch_receipt:{remote.calls[0][2]}", {})
    await service.dispatch(remote)
    assert len(remote.calls) == 1


@pytest.mark.parametrize("change", ["claim", "cancel", "completed_attempt"])
async def test_dispatch_rechecks_admission_after_terminal_read(running_service, change):
    service = running_service
    submitted = await command(service, "objective.submit", {"text": "first"})
    job_id = submitted["data"]["job"]
    remote = DispatchLifecycleRemote()
    await service.dispatch(remote)

    def race():
        if change == "cancel":
            service.state.cancel_objective(submitted["data"]["objective"])
            return
        grant = service.state.acquire_grant(
            job_id, "codex", "current", "coordinator", "r"
        )
        if change == "completed_attempt":
            service.state.claim_grant(grant["id"], grant["generation"], "child", "run")
            service.state.record_execution(
                grant["id"],
                grant["generation"],
                "run",
                "failed",
                pending_children=False,
                tree_confirmed=True,
            )
            service.after_execution()

    remote.on_read = race
    await service.dispatch(remote)
    assert len(remote.calls) == 1
    assert (
        service.state.connection.execute(
            "SELECT count(*) FROM outbox WHERE id LIKE 'dispatch:%'"
        ).fetchone()[0]
        == 1
    )


async def test_dispatch_retry_lost_response_replays_same_key(running_service):
    service = running_service
    await command(service, "objective.submit", {"text": "first"})

    class LostRetry(DispatchLifecycleRemote):
        async def send(self, *args, **kwargs):
            receipt = await super().send(*args, **kwargs)
            if len(self.calls) == 2:
                raise ConnectionError("lost retry receipt")
            return receipt

    remote = LostRetry()
    await service.dispatch(remote)
    with pytest.raises(ConnectionError):
        await service.dispatch(remote)
    remote.snapshot["thread"]["activeRunId"] = remote.snapshot["thread"]["latestRunId"]
    await service.dispatch(remote)
    assert len(remote.calls) == 3
    assert remote.calls[1] == remote.calls[2]
    assert remote.calls[0][2] != remote.calls[1][2]
