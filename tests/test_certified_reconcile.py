"""Certified results stay put; routing never opens an implicit top-level.

Real socket memory, Git, SQLite and the daemon's background batch; simulated
caller-bound T3 (test_managed_tasks.Host). Reproduces the fa2d0346 pattern: a
top-level plan task certified once, then reconciled again by the daemon.
"""

import json
from types import SimpleNamespace

import pytest
from test_managed_tasks import Host, git, initialize, report, start
from test_managed_tasks import system as system_fixture

from oraculo.managed_tasks import ManagedTasks
from oraculo.presentation import return_message
from oraculo.task_store import TaskStore
from oraculo.task_transport import SocketMemory

system = system_fixture

MUTATING = {
    "delegate_task",
    "t3_thread_launch",
    "t3_thread_send",
    "t3_thread_fork",
    "create_threads",
}


def names(host, since=0):
    return [name for name, _ in host.calls[since:]]


async def certified_plan(system):
    """Top-level plan, executor-submitted, certified by daemon reconciliation."""
    task = await start(
        system, key="plan", independent=True, deliverable_kind="plan", text="Plan it"
    )
    assert task["transport_mode"] == "managed_thread", task
    child = task["child"]["childThreadId"]
    system.host.caller = child
    evidence = dict(report(task), deliverable_kind="plan", approval_state="pending")
    submitted = await system.engine.command(
        "finish", {"task_id": task["id"], "report": evidence}
    )
    assert submitted["phase"] == "validation_submitted", submitted
    system.host.complete(child)
    system.host.caller = "origin"
    await system.engine.reconcile(task["id"])
    certified = system.store.get(task["id"])
    assert certified["phase"] == "awaiting_approval", certified["phase"]
    assert certified["approval_state"] == "pending"
    return certified, evidence


def snapshot(system, task_id):
    task = system.store.get(task_id)
    return (
        len(task["events"]),
        task["phase"],
        task["approval_state"],
        task["snapshot_revision"],
        len(system.host.sends),
    )


async def test_repeated_reconcile_of_certified_task_emits_nothing(system):
    task, _ = await certified_plan(system)
    before = snapshot(system, task["id"])
    calls = len(system.host.calls)
    for _ in range(3):
        await system.engine.reconcile(task["id"])
    assert snapshot(system, task["id"]) == before
    # No observation, re-certification or wake-up of the origin.
    assert names(system.host, calls) == []
    assert system.store.pending_returns(task["id"]) == []


async def test_plan_approval_survives_reconcile_and_resume(system):
    task, evidence = await certified_plan(system)
    approved = await system.engine.command(
        "approve",
        {
            "task_id": task["id"],
            "commit": evidence["commit"],
            "evidence": "User approved this plan",
        },
    )
    assert approved["phase"] == "plan_approved"
    assert approved["approval_state"] == "approved"
    before = snapshot(system, task["id"])
    for _ in range(3):
        await system.engine.reconcile(task["id"])
    resumed = await system.engine.command("resume", {"task_id": task["id"]})
    assert resumed["phase"] == "plan_approved"
    assert snapshot(system, task["id"]) == before
    # Replaying the same certified report through the origin is a no-op too.
    replay = await system.engine.command(
        "finish", {"task_id": task["id"], "report": evidence}
    )
    assert replay["phase"] == "plan_approved"
    assert replay["approval_state"] == "approved"
    assert snapshot(system, task["id"]) == before


async def test_different_report_on_certified_task_is_rejected_without_regression(
    system,
):
    task, evidence = await certified_plan(system)
    before = snapshot(system, task["id"])
    other = dict(evidence, summary="A different plan")
    rejected = await system.engine.command(
        "finish", {"task_id": task["id"], "report": other}
    )
    assert rejected["rejection"]["persisted"] is False
    assert rejected["rejection"]["reason"] == "terminal_report_reused"
    assert snapshot(system, task["id"]) == before
    assert system.store.get(task["id"])["report"] == evidence


async def test_new_information_emits_exactly_once_and_replay_is_idempotent(system):
    task, evidence = await certified_plan(system)
    sends = len(system.host.sends)
    system.host.fail_send_once = True  # host accepted; ACK lost
    approved = await system.engine.command(
        "approve",
        {
            "task_id": task["id"],
            "commit": evidence["commit"],
            "evidence": "User approved this plan",
        },
    )
    assert approved["pending_returns"] == 1
    pending = system.store.pending_returns(task["id"])
    event = system.store.get(task["id"])["events"][-1]
    assert pending[0]["id"] == task["id"] + ":" + event["id"]
    assert event["phase"] == "plan_approved"
    first = system.host.sends[pending[0]["id"]]["arguments"]
    for _ in range(3):
        await system.engine.reconcile(task["id"])
    # Replay used the same event key and byte-identical message (Host asserts
    # argument equality per key); the new information was delivered once.
    assert len(system.host.sends) == sends + 1
    assert system.host.sends[pending[0]["id"]]["arguments"] == first
    assert first["clientRequestId"] == pending[0]["id"]
    assert system.store.pending_returns(task["id"]) == []
    sent = [a for n, a in system.host.calls if n == "t3_thread_send"]
    assert [a["clientRequestId"] for a in sent].count(pending[0]["id"]) == 2


@pytest.mark.parametrize("return_format", ["json", "readable"])
async def test_repeated_cycle_after_upgrade_is_semantically_suppressed(
    system, return_format
):
    """Legacy events already emitted by an old daemon do not wake the origin."""
    task, _ = await certified_plan(system)
    store = system.store
    payload = store.get(task["id"])
    payload["return_format"] = return_format
    store.db.execute(
        "UPDATE managed_tasks SET payload=? WHERE id=?",
        (json.dumps(payload), task["id"]),
    )
    sends = len(system.host.sends)
    # The old reconcile cycle: observation phases, then the same final state.
    for phase in ("workspace_ready", "validating", "memory_pending"):
        store.update(task["id"], phase, {"legacy_cycle": phase})
    store.update(task["id"], "awaiting_approval", {"legacy_cycle": "final"})
    await system.engine.flush_returns(task["id"])
    assert len(system.host.sends) == sends
    assert store.pending_returns(task["id"]) == []
    assert store.describe(task["id"])["pending_returns"] == 0
    # A real change afterwards is new information: exactly one notification.
    store.update(task["id"], "blocked", {"reason": "x"}, blocked_reason="x")
    await system.engine.flush_returns(task["id"])
    await system.engine.flush_returns(task["id"])
    assert len(system.host.sends) == sends + 1


async def test_negative_certified_result_is_not_recertified(system):
    task = await start(system, key="negative", independent=True)
    child = task["child"]["childThreadId"]
    system.host.caller = child
    evidence = report(task)
    evidence["checks"] = [{"command": "pytest", "status": "failed"}]
    await system.engine.command("finish", {"task_id": task["id"], "report": evidence})
    system.host.complete(child, status="failed")
    system.host.caller = "origin"
    await system.engine.reconcile(task["id"])
    failed = system.store.get(task["id"])
    assert failed["work_status"] == "failed"
    assert failed["blocked_reason"] == "work_not_successful"
    before = snapshot(system, task["id"])
    calls = len(system.host.calls)
    for _ in range(3):
        await system.engine.reconcile(task["id"])
    assert snapshot(system, task["id"]) == before
    assert names(system.host, calls) == []


def commits(path):
    first = git(path, "rev-parse", "HEAD")
    git(
        path,
        "-c",
        "user.name=Example",
        "-c",
        "user.email=example@example.org",
        "commit",
        "--allow-empty",
        "-m",
        "Second commit",
    )
    return first, git(path, "rev-parse", "HEAD")


async def test_base_different_from_head_is_refused_before_any_effect(system):
    alpha = system.service.config.projects_root / "alpha"
    base, head = commits(alpha)
    state = system.service.state
    engine = ManagedTasks(
        system.service.config,
        system.store,
        system.host,
        system.memory,
        admission=state,
    )
    cycles = len(system.service.memory_cycles.list())
    occupied = state.capacity()["occupied"]
    result = await engine.command(
        "start",
        {
            "kind": "work",
            "key": "old-base",
            "cwd": str(alpha),
            "text": "Plan from the requested base.",
            "base": base,
        },
    )
    assert result["created"] is False
    assert result["state"] == "launch_rejected"
    assert result["blocked_reason"] == "native_child_base_not_head"
    rejection = result["rejection"]
    assert rejection["persisted"] is False and rejection["host_effects"] == "none"
    assert "independent=true" in rejection["hint"]
    assert "worktree" in rejection["hint"]
    assert rejection["observed"]["base_sha"] == base
    assert rejection["observed"]["head"] == head
    # Read-only host traffic only: no thread, child, launch or send.
    assert not MUTATING & set(names(system.host))
    assert set(names(system.host)) <= {
        "orchestrator_capabilities",
        "t3_project_list",
        "t3_thread_read",
    }
    assert system.host.created == []
    assert (
        system.store.db.execute("SELECT count(*) FROM managed_tasks").fetchone()[0] == 0
    )
    assert state.capacity()["occupied"] == occupied
    assert len(system.service.memory_cycles.list()) == cycles
    # Aligning the origin checkout with the base makes the same key native.
    git(alpha, "switch", "-c", "at-base", base)
    aligned = await engine.command(
        "start",
        {
            "kind": "work",
            "key": "old-base",
            "cwd": str(alpha),
            "text": "Plan from the requested base.",
            "base": base,
        },
    )
    assert aligned["transport_mode"] == "native_child", aligned
    assert aligned["base_sha"] == base
    child = system.host.threads[aligned["child"]["childThreadId"]]
    assert child["parentThreadId"] == "origin"
    assert "t3_thread_launch" not in names(system.host)


@pytest.mark.parametrize("independent", ["true", 1, None, {"x": True}])
async def test_non_boolean_independent_is_refused(system, independent):
    result = await start(system, key="flag", independent=independent)
    assert result["created"] is False
    assert result["blocked_reason"] == "invalid_independent_flag"
    assert not MUTATING & set(names(system.host))


async def test_native_child_keeps_parent(system):
    result = await start(system)
    assert result["transport_mode"] == "native_child"
    child = system.host.threads[result["child"]["childThreadId"]]
    assert child["parentThreadId"] == "origin"
    assert result["origin"]["thread_id"] == "origin"
    assert "t3_thread_launch" not in names(system.host)
    assert system.store.history(result["id"])["events"][-1]["phase"] in {
        "executing",
        "workspace_ready",
    }
    launched = next(
        e
        for e in system.store.get(result["id"])["events"]
        if e["phase"] == "launch_acknowledged"
    )
    assert launched["proof"]["native_relationship"] == "subagent"


async def test_explicit_independent_still_opens_top_level_at_other_base(system):
    alpha = system.service.config.projects_root / "alpha"
    base, head = commits(alpha)
    result = await start(system, key="independent", independent=True, base=base)
    assert result["transport_mode"] == "managed_thread", result
    assert result["base_sha"] == base != head
    child = system.host.threads[result["child"]["childThreadId"]]
    assert child["parentThreadId"] is None
    assert "t3_thread_launch" in names(system.host)
    assert "delegate_task" not in names(system.host)


async def test_persisted_implicit_top_level_intent_is_never_sent(system):
    actor = {"thread_id": "origin", "run_id": "origin-run", "project_id": "alpha"}
    request = {
        "kind": "work",
        "key": "legacy-intent",
        "cwd": str(system.service.config.projects_root / "alpha"),
        "text": "Legacy intent persisted by an older release.",
    }
    task = system.store.create(actor, "legacy-intent", request)
    system.store.update(
        task["id"],
        "launch_pending",
        {"legacy": True},
        transport_mode="managed_thread",
        launch_arguments={"title": "Legacy [x]"},
    )
    result = await system.engine.command("resume", {"task_id": task["id"]})
    assert result["rejection"]["reason"] == "implicit_top_level_intent_refused"
    assert not MUTATING & set(names(system.host))
    assert system.store.get(task["id"]).get("launch_attempted") is None


async def test_control_event_never_creates_task_or_approves(system):
    readable_task, evidence = await certified_plan(system)
    raw = {
        "marker": "ORACULO_CONTROL_V1",
        "task_id": readable_task["id"],
        "phase": "awaiting_approval",
    }
    count = system.store.db.execute("SELECT count(*) FROM managed_tasks").fetchone()[0]
    calls = len(system.host.calls)
    for text in (
        json.dumps(raw),
        return_message(
            dict(raw, return_format="readable", presentation=_view(readable_task))
        ),
    ):
        result = await start(system, key="control-" + str(len(text)), text=text)
        assert result == {"state": "control_event_not_work", "created": False}
    assert (
        system.store.db.execute("SELECT count(*) FROM managed_tasks").fetchone()[0]
        == count
    )
    assert not MUTATING & set(names(system.host, calls))
    refused = await system.engine.command(
        "approve",
        {
            "task_id": readable_task["id"],
            "commit": evidence["commit"],
            "evidence": json.dumps(raw),
        },
    )
    assert refused["blocked_reason"] == "plan_approval_binding_required"
    assert system.store.get(readable_task["id"])["approval_state"] == "pending"


def _view(task):
    from oraculo.presentation import task_view

    return task_view(task)


@pytest.fixture
async def daemon(running_service):
    """The daemon's own ledger, reconciled by its real background batch."""
    service = running_service
    initialize(service.config.projects_root / "alpha")
    path = service.config.state_dir / "managed-tasks.sqlite3"
    store = TaskStore(path)
    host = Host(service.config.projects_root)
    engine = ManagedTasks(service.config, store, host, SocketMemory(service.config))
    try:
        yield SimpleNamespace(
            service=service, store=store, host=host, engine=engine, path=path
        )
    finally:
        store.close()


async def test_daemon_batch_skips_certified_and_keeps_pending_work(daemon):
    system = daemon
    certified, _ = await certified_plan(system)
    pending = await start(system, key="pending", independent=True)
    child = pending["child"]["childThreadId"]
    system.host.caller = child
    await system.engine.command(
        "finish", {"task_id": pending["id"], "report": report(pending)}
    )
    system.host.complete(child)
    system.host.caller = "origin"
    assert system.engine.background_work(system.store.get(certified["id"])) is None
    assert system.engine.background_work(system.store.get(pending["id"])) == "observe"
    before = snapshot(system, certified["id"])
    calls = len(system.host.calls)
    for _ in range(2):
        await system.service.reconcile_managed_tasks(system.host)
        await system.service.managed_flush
    reread = TaskStore(system.path)
    try:
        assert reread.get(pending["id"])["phase"] == "pr_pending"
        task = reread.get(certified["id"])
        assert (
            len(task["events"]),
            task["phase"],
            task["approval_state"],
            task["snapshot_revision"],
        ) == before[:4]
    finally:
        reread.close()
    touched = [
        a
        for _, a in system.host.calls[calls:]
        if certified["id"] in json.dumps(a)
        or certified["child"]["childThreadId"] in json.dumps(a)
    ]
    assert touched == []


async def test_leftover_reservation_is_released_without_regressing_approval(system):
    task, evidence = await certified_plan(system)
    await system.engine.command(
        "approve",
        {
            "task_id": task["id"],
            "commit": evidence["commit"],
            "evidence": "User approved this plan",
        },
    )
    state = system.service.state
    state.reserve_managed(task["id"], "codex")  # interrupted certification
    engine = ManagedTasks(
        system.service.config, system.store, system.host, system.memory, admission=state
    )
    assert engine.background_work(system.store.get(task["id"])) == "release"
    before = snapshot(system, task["id"])
    await engine.reconcile(task["id"])
    assert state.capacity()["occupied"] == 0
    assert engine.background_work(system.store.get(task["id"])) is None
    after = system.store.get(task["id"])
    assert after["phase"] == "plan_approved"
    assert after["approval_state"] == "approved"
    assert after["report"] == evidence
    assert snapshot(system, task["id"])[4] == before[4]  # nothing sent
    calls = len(system.host.calls)
    await engine.reconcile(task["id"])
    assert names(system.host, calls) == []
