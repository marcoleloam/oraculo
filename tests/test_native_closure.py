"""Daemon closure of native children and the origin's batch resume.

Real service socket, memory, Git, SQLite, admission and the daemon's own
background batch. T3 is simulated: the origin uses the caller-bound fake and
the daemon uses a client that has no parent identity.
"""

import json
from pathlib import Path

import pytest
from test_managed_service import Caller
from test_managed_tasks import git, initialize

from oraculo import native_closure
from oraculo.cli import parser
from oraculo.managed_service import ScopeBusy, command
from oraculo.managed_tasks import ManagedTasks
from oraculo.state import StateError
from oraculo.task_transport import SocketMemory

PARENT_ONLY = {
    "task_status",
    "delegate_task",
    "t3_thread_launch",
    "orchestrator_capabilities",
}


class DaemonClient:
    """The daemon's own T3 session: no parent identity, no launch."""

    def __init__(self, host):
        self.host = host
        self.calls = []

    async def call(self, name, arguments):
        assert name not in PARENT_ONLY, name
        self.calls.append(name)
        return await self.host.call(name, arguments)


@pytest.fixture
async def world(running_service, monkeypatch):
    service = running_service
    service.state.max_executors = 8
    initialize(service.config.projects_root / "alpha")
    # Every failed attempt is due again at once; backoff has its own test.
    monkeypatch.setattr(native_closure, "BACKOFF_BASE_SECONDS", 0)
    host = Caller(service.config.projects_root)
    return service, host, DaemonClient(host)


async def submitted(service, host, key, *, complete=True, **status):
    """A native child that delivered its report through its own bridge."""
    task = await command(
        service.config,
        "start",
        dict(
            kind="work",
            key=key,
            text="Implement the bounded change " + key,
            title="Bounded " + key,
            cwd=str(service.config.projects_root / "alpha"),
        ),
        host,
    )
    assert task["transport_mode"] == "native_child", task
    child = task["child"]["childThreadId"]
    host.caller = child
    report = {
        "summary": "Implemented " + key,
        "checks": [{"command": "pytest", "status": "passed"}],
        "commit": git(task["child_workspace"]["path"], "rev-parse", "HEAD"),
    }
    result = await command(
        service.config, "finish", {"task_id": task["id"], "report": report}, host
    )
    assert result["phase"] == "validation_submitted", result
    host.caller = "origin"
    if complete:
        host.complete(child, **status)
    return task["id"], child


async def tick(service, client):
    await service.reconcile_managed_tasks(client)
    if service.managed_flush is not None:
        await service.managed_flush
    # One pass per tick: do not let the scan cursor skip the next one.
    service.managed_service.store.state.set_meta("managed_reconcile_cursor", 0)


def stored(service, task_id):
    return service.managed_service.store.get(task_id)


def held(service, task_id):
    return bool(
        service.state._one("SELECT id FROM managed_reservations WHERE id=?", (task_id,))
    )


def releases(service, task_id):
    return [
        json.loads(row["payload"])
        for row in service.state.connection.execute(
            "SELECT payload FROM audit_events WHERE kind='managed.release'"
        )
        if json.loads(row["payload"])["task_id"] == task_id
    ]


def notices(host, task_id):
    return [v["arguments"]["message"] for k, v in host.sends.items() if task_id in k]


async def test_daemon_closes_submitted_native_child_without_task_status(world):
    service, host, daemon = world
    task_id, _ = await submitted(service, host, "ok")
    before = len(notices(host, task_id))
    assert held(service, task_id)
    await tick(service, daemon)
    task = stored(service, task_id)
    assert task["phase"] == "pr_pending", (task["phase"], task["blocked_reason"])
    assert task["work_status"] == "succeeded"
    assert task["memory"]["write_receipt"]["state"] == "confirmed"
    closure = task["completion_evidence"]["closure"]
    assert closure["mode"] == "daemon_reconciled_without_native_task_status"
    assert closure["skipped_read"] == "task_status"
    assert (
        task["completion_evidence"]["submission_actor"]["thread_id"]
        == (task["child"]["childThreadId"])
    )
    # Reservation released exactly once, with the path recorded in the audit.
    assert not held(service, task_id)
    released = releases(service, task_id)
    assert len(released) == 1
    assert released[0]["proof"]["closure"] == closure["mode"]
    # One final-state notice, and it says the closure was automatic.
    sent = notices(host, task_id)
    assert len(sent) == before + 1
    assert "Fechamento automático pelo serviço" in sent[-1]
    # A certified task is never observed again.
    calls, events = len(daemon.calls), len(task["events"])
    for _ in range(2):
        await tick(service, daemon)
    assert len(daemon.calls) == calls
    assert len(stored(service, task_id)["events"]) == events
    assert len(releases(service, task_id)) == 1
    assert len(notices(host, task_id)) == before + 1


async def test_live_descendant_is_retried_without_blocking(world):
    service, host, daemon = world
    task_id, child = await submitted(service, host, "descendant", pending=True)
    before = len(notices(host, task_id))
    await tick(service, daemon)
    task = stored(service, task_id)
    assert task["phase"] == "validation_submitted"
    assert task["blocked_reason"] is None and task["work_status"] == "pending"
    assert held(service, task_id)
    state = service.managed_service.store.state.get_meta(
        native_closure.META_PREFIX + task_id
    )
    assert state["attempts"] == 1 and state["notified"] is False
    assert state["definitive"] is False
    assert len(notices(host, task_id)) == before
    host.threads["reviewer-" + child].update(status="completed", activeRunId=None)
    await tick(service, daemon)
    task = stored(service, task_id)
    assert task["work_status"] == "succeeded" and task["phase"] == "pr_pending"
    assert len(releases(service, task_id)) == 1
    assert len(notices(host, task_id)) == before + 1


async def test_dirty_worktree_sends_one_actionable_notice_then_closes(world):
    service, host, daemon = world
    task_id, child = await submitted(service, host, "dirty")
    stray = Path(host.threads[child]["worktreePath"]) / "stray.txt"
    stray.write_text("uncommitted")
    before = len(notices(host, task_id))
    for _ in range(3):
        await tick(service, daemon)
    task = stored(service, task_id)
    assert task["phase"] == "validation_submitted" and task["blocked_reason"] is None
    assert held(service, task_id)
    assert task["daemon_closure"]["missing"] == (
        "validated_commit_or_clean_workspace_required"
    )
    sent = notices(host, task_id)
    assert len(sent) == before + 1  # one notice for three failed attempts
    assert "oraculo task resume --all-submitted" in sent[-1]
    assert "Fechamento automático pendente" in sent[-1]
    event = next(e for e in task["events"] if e["proof"].get("daemon_closure"))
    assert event["proof"]["command"] == "oraculo task resume --all-submitted"
    assert event["proof"]["single_task_payload"] == {"task_id": task_id}
    assert event["proof"]["definitive"] is True
    stray.unlink()
    await tick(service, daemon)
    assert stored(service, task_id)["work_status"] == "succeeded"
    assert len(notices(host, task_id)) == before + 2
    assert len(releases(service, task_id)) == 1


async def test_missing_original_run_never_certifies(world):
    service, host, daemon = world
    task_id, child = await submitted(service, host, "missing-run")
    host.threads[child]["latestRunId"] = "unrelated-run"
    await tick(service, daemon)
    task = stored(service, task_id)
    assert task["phase"] == "validation_submitted" and task.get("report") is None
    assert task["daemon_closure"]["missing"] == "original_task_run_not_observed"
    assert held(service, task_id) and releases(service, task_id) == []


async def test_transient_failures_notify_once_after_the_threshold(world, monkeypatch):
    service, host, daemon = world
    monkeypatch.setattr(native_closure, "NOTICE_AFTER_FAILURES", 3)
    monkeypatch.setattr(native_closure, "MAX_ATTEMPTS", 4)
    task_id, _ = await submitted(service, host, "stuck", pending=True)
    before = len(notices(host, task_id))
    for expected in (0, 0, 1, 1):
        await tick(service, daemon)
        assert len(notices(host, task_id)) == before + expected
    key = native_closure.META_PREFIX + task_id
    assert service.managed_service.store.state.get_meta(key)["exhausted"] is True
    calls = len(daemon.calls)
    await tick(service, daemon)
    assert len(daemon.calls) == calls  # exhausted: the origin must act
    assert stored(service, task_id)["phase"] == "validation_submitted"


async def test_backoff_keeps_a_failed_task_out_of_the_next_cycle(world, monkeypatch):
    service, host, daemon = world
    monkeypatch.setattr(native_closure, "BACKOFF_BASE_SECONDS", 60)
    task_id, _ = await submitted(service, host, "backoff", pending=True)
    await tick(service, daemon)
    calls = len(daemon.calls)
    await tick(service, daemon)
    assert len(daemon.calls) == calls
    key = native_closure.META_PREFIX + task_id
    assert service.managed_service.store.state.get_meta(key)["attempts"] == 1


async def test_memory_unconfirmed_then_confirmed(world, monkeypatch):
    service, host, daemon = world
    task_id, _ = await submitted(service, host, "memory")
    real, attempts = SocketMemory.finish, []

    async def flaky(self, project, actor, cycle_id, status, report):
        attempts.append(cycle_id)
        if len(attempts) == 1:
            cycle = stored(service, task_id)["memory"]
            return dict(cycle, write_state="pending", write_receipt=None)
        return await real(self, project, actor, cycle_id, status, report)

    monkeypatch.setattr(SocketMemory, "finish", flaky)
    await tick(service, daemon)
    task = stored(service, task_id)
    assert task["phase"] == "memory_pending" and task["blocked_reason"] is None
    assert held(service, task_id) and releases(service, task_id) == []
    await tick(service, daemon)
    task = stored(service, task_id)
    assert task["phase"] == "pr_pending" and task["work_status"] == "succeeded"
    assert len(attempts) == 2 and len(releases(service, task_id)) == 1
    assert task["completion_evidence"]["closure"]["mode"] == native_closure.CLOSURE_MODE


async def test_maintenance_flag_stops_closure(world):
    service, host, daemon = world
    task_id, _ = await submitted(service, host, "maintenance")
    await tick(service, daemon)  # opens the daemon ledger; closes this task
    other, _ = await submitted(service, host, "maintenance-2")
    store = service.managed_service.store
    engine = ManagedTasks(
        service.config,
        store,
        daemon,
        SocketMemory(service.config),
        admission=service.state,
    )
    service.state.set_meta("runtime_maintenance", {"id": "window"})
    try:
        calls, events = len(daemon.calls), len(stored(service, other)["events"])
        await service.reconcile_managed_tasks(daemon)  # the whole batch is skipped
        await engine.reconcile(other)  # and so is a task reached mid-batch
        assert len(daemon.calls) == calls
        assert len(stored(service, other)["events"]) == events
        assert held(service, other)
    finally:
        service.state.set_meta("runtime_maintenance", None)
    await tick(service, daemon)
    assert stored(service, other)["work_status"] == "succeeded"


async def test_busy_scope_is_skipped_and_closed_later(world):
    service, host, daemon = world
    task_id, _ = await submitted(service, host, "busy")
    facade = service.managed_service
    facade.admit("caller", "origin-op", action="resume", task_id=task_id)
    facade.acquire_scope("origin-op", ("task", task_id))
    try:
        await tick(service, daemon)
        assert daemon.calls == []
        assert stored(service, task_id)["phase"] == "validation_submitted"
    finally:
        facade.finish("origin-op")
    await tick(service, daemon)
    assert stored(service, task_id)["work_status"] == "succeeded"


async def test_config_switch_restores_parent_driven_closure(world):
    service, host, daemon = world
    task_id, _ = await submitted(service, host, "off")
    service.config.managed_native_daemon_closure = False
    try:
        await tick(service, daemon)
        assert daemon.calls == []
        assert stored(service, task_id)["phase"] == "validation_submitted"
    finally:
        service.config.managed_native_daemon_closure = True


async def test_origin_resume_and_daemon_never_certify_twice(world, monkeypatch):
    service, host, daemon = world
    real, finishes = SocketMemory.finish, []

    async def counted(self, project, actor, cycle_id, status, report):
        finishes.append(cycle_id)
        return await real(self, project, actor, cycle_id, status, report)

    monkeypatch.setattr(SocketMemory, "finish", counted)
    # Origin first: the caller-bound path keeps the mandatory task_status read.
    first, _ = await submitted(service, host, "origin-first")
    mark = len(host.calls)
    resumed = await command(service.config, "resume", {"task_id": first}, host)
    assert resumed["work_status"] == "succeeded"
    assert "task_status" in [name for name, _ in host.calls[mark:]]
    assert "closure" not in stored(service, first)["completion_evidence"]
    events = len(stored(service, first)["events"])
    await tick(service, daemon)
    assert daemon.calls == []
    assert len(stored(service, first)["events"]) == events
    # Daemon first: a later origin resume replays and changes nothing.
    second, _ = await submitted(service, host, "daemon-first")
    await tick(service, daemon)
    certified = stored(service, second)
    assert certified["work_status"] == "succeeded"
    replay = await command(service.config, "resume", {"task_id": second}, host)
    assert replay["phase"] == certified["phase"]
    after = stored(service, second)
    assert after["completion_evidence"] == certified["completion_evidence"]
    assert len(after["events"]) == len(certified["events"])
    assert len(finishes) == 2
    assert len(releases(service, first)) == 1 == len(releases(service, second))


async def test_negative_report_is_a_final_state_closed_once(world):
    service, host, daemon = world
    task = await command(
        service.config,
        "start",
        dict(
            kind="work",
            key="negative",
            text="Implement",
            cwd=str(service.config.projects_root / "alpha"),
        ),
        host,
    )
    child = task["child"]["childThreadId"]
    host.caller = child
    report = {
        "summary": "FAIL: suite red",
        "checks": [{"command": "pytest", "status": "failed"}],
    }
    await command(
        service.config, "finish", {"task_id": task["id"], "report": report}, host
    )
    host.caller = "origin"
    host.complete(child)
    await tick(service, daemon)
    done = stored(service, task["id"])
    assert done["work_status"] == "failed"
    assert done["blocked_reason"] == "work_not_successful"
    assert not held(service, task["id"])
    calls = len(daemon.calls)
    await tick(service, daemon)
    assert len(daemon.calls) == calls and len(releases(service, task["id"])) == 1


async def test_batch_resume_closes_mixed_outcomes_in_one_session(world):
    service, host, _ = world
    good, _ = await submitted(service, host, "good")
    dirty, dirty_child = await submitted(service, host, "dirty")
    running, _ = await submitted(service, host, "running", complete=False)
    interrupted, _ = await submitted(service, host, "interrupted")
    busy, _ = await submitted(service, host, "busy")
    executing = await command(
        service.config,
        "start",
        dict(
            kind="work",
            key="executing",
            text="Still working",
            cwd=str(service.config.projects_root / "alpha"),
        ),
        host,
    )
    (Path(host.threads[dirty_child]["worktreePath"]) / "stray.txt").write_text("x")
    facade = service.managed_service
    facade.store.update(
        interrupted,
        "blocked",
        {"reason": "managed_host_ack_eof"},
        blocked_reason="managed_host_ack_eof",
    )
    facade.admit("caller", "other-op", action="progress", task_id=busy)
    facade.acquire_scope("other-op", ("task", busy))
    mark = len(host.calls)
    try:
        result = await command(service.config, "resume-submitted", {}, host)
    finally:
        facade.finish("other-op")
    outcomes = {item["task_id"]: item for item in result["tasks"]}
    assert set(outcomes) == {good, dirty, running, interrupted, busy}
    assert executing["id"] not in outcomes
    assert outcomes[good]["outcome"] == "closed"
    assert outcomes[interrupted]["outcome"] == "closed"
    assert outcomes[dirty]["outcome"] == "rejected"
    assert outcomes[dirty]["reason"] == "validated_commit_or_clean_workspace_required"
    assert outcomes[running]["outcome"] == "blocked"
    assert outcomes[running]["reason"] == "children_still_running"
    assert outcomes[busy]["outcome"] == "skipped_busy"
    assert outcomes[busy]["operation_id"] == "other-op"
    assert result["counts"] == {
        "closed": 2,
        "rejected": 1,
        "blocked": 1,
        "skipped_busy": 1,
    }
    assert result["complete"] is False and result["selected"] == 5
    # A rejection is a response, not a transition; a busy task is untouched.
    assert stored(service, dirty)["phase"] == "validation_submitted"
    assert stored(service, busy)["phase"] == "validation_submitted"
    assert stored(service, good)["work_status"] == "succeeded"
    assert "closure" not in stored(service, good)["completion_evidence"]
    assert not held(service, good) and not held(service, interrupted)
    names = [name for name, _ in host.calls[mark:]]
    # One identity and one project catalog for the whole session.
    assert names.count("orchestrator_capabilities") == 1
    assert names.count("t3_project_list") == 1
    assert names.count("task_status") >= 2  # still mandatory when caller-bound
    again = await command(service.config, "resume-submitted", {}, host)
    assert {i["task_id"] for i in again["tasks"]} == {dirty, busy}


async def test_batch_resume_filters_by_project_and_origin(world):
    service, host, _ = world
    task_id, _ = await submitted(service, host, "scoped")
    none = await command(service.config, "resume-submitted", {"project": "beta"}, host)
    assert none["selected"] == 0 and none["tasks"] == [] and none["complete"] is True
    host.threads["stranger"] = dict(host.threads["origin"], threadId="stranger")
    host.caller = "stranger"
    foreign = await command(service.config, "resume-submitted", {}, host)
    assert foreign["selected"] == 0
    host.caller = "origin"
    with pytest.raises(StateError, match="batch_resume_payload_invalid"):
        await command(service.config, "resume-submitted", {"task_id": task_id}, host)
    mine = await command(service.config, "resume-submitted", {"project": "alpha"}, host)
    assert [i["outcome"] for i in mine["tasks"]] == ["closed"]
    assert mine["complete"] is True


async def test_batch_resume_stops_when_the_bridge_is_lost(world):
    service, host, _ = world
    first, _ = await submitted(service, host, "first")
    second, _ = await submitted(service, host, "second")
    original, seen = host.call, []

    async def lossy(name, args):
        seen.append(name)
        if name == "t3_thread_list":
            raise StateError("host_response_uncertain")
        return await original(name, args)

    host.call = lossy
    try:
        result = await command(service.config, "resume-submitted", {}, host)
    finally:
        host.call = original
    by_id = {i["task_id"]: i for i in result["tasks"]}
    assert by_id[first]["outcome"] == "interrupted"
    assert by_id[second]["outcome"] == "not_attempted"
    assert stored(service, second)["phase"] == "validation_submitted"
    # The interrupted task had no effect recorded: the next batch takes it again.
    assert native_closure.pre_effect_interruption(stored(service, first))
    retry = await command(service.config, "resume-submitted", {}, host)
    assert [i["outcome"] for i in retry["tasks"]] == ["closed", "closed"]


def test_cli_accepts_the_batch_flags():
    args = parser().parse_args(
        ["task", "resume", "--all-submitted", "--project", "alpha", "--stdio-host"]
    )
    assert args.all_submitted and args.project == "alpha" and args.file is None
    assert isinstance(ScopeBusy("op"), StateError)
