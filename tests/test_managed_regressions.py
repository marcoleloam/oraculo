import pytest
from test_managed_service import Caller
from test_managed_tasks import initialize, report

from oraculo.managed_service import command
from oraculo.task_store import TaskStore


async def start(service, **extra):
    initialize(service.config.projects_root / "alpha")
    host = Caller(service.config.projects_root)
    payload = dict(
        kind="work",
        key="review",
        text="Review fixture",
        cwd=str(service.config.projects_root / "alpha"),
        **extra,
    )
    task = await command(service.config, "start", payload, host)
    assert task["phase"] == "executing", task
    return host, task


@pytest.mark.parametrize("terminal", ["failed", "cancelled", "interrupted"])
async def test_terminal_failure_releases_slot(running_service, terminal):
    host, task = await start(running_service)
    host.complete(task["child"]["childThreadId"], status=terminal)
    result = await command(
        running_service.config,
        "finish",
        dict(task_id=task["id"], report=report(task)),
        host,
    )
    assert result["work_status"] in {"failed", "stopped"}, result
    assert result["memory"]["write_state"] == "saved"
    assert running_service.state.capacity()["occupied"] == 0


async def test_plan_request_cannot_be_retyped_by_finish(running_service):
    host, task = await start(running_service, deliverable_kind="plan")
    host.complete(task["child"]["childThreadId"])
    result = await command(
        running_service.config,
        "finish",
        dict(
            task_id=task["id"],
            report=dict(report(task), deliverable_kind="implementation"),
        ),
        host,
    )
    assert result["blocked_reason"] == "deliverable_kind_conflict"
    assert result["phase"] != "pr_pending"


def test_failed_delivery_refreshes_before_retry(tmp_path):
    store = TaskStore(tmp_path / "ledger.db")
    task = store.create(
        dict(thread_id="origin", run_id="run"), "review", {"text": "fixture"}
    )
    store.update(
        task["id"], "blocked", {"reason": "temporary"}, blocked_reason="temporary"
    )
    event = store.pending_returns(task["id"])[-1]
    first = store.delivery(event)  # emit failed before host accepted; no ACK
    store.update(task["id"], "executing", {"recovered": True})
    retry = store.delivery(event)
    assert retry == first  # exact bytes remain safe under the same host dedupe key
    assert (
        retry["snapshot"]["phase"] == "executing"
        or retry.get("refresh_required") is True
    )
    store.close()


async def test_maintenance_blocks_review_writes(running_service):
    from oraculo.cli import make_envelope, request

    service = running_service
    begin = await request(
        service.config,
        make_envelope("maintenance.begin", "*", {"operation_id": "review-maint"}),
    )
    assert begin["ok"], begin
    result = await request(service.config, make_envelope("review.open", "alpha", {}))
    assert not result["ok"]


async def test_runtime_identity_hash_is_loaded_config(running_service):
    import hashlib

    from oraculo.cli import make_envelope, request

    service = running_service
    original = hashlib.sha256(service.config_path.read_bytes()).hexdigest()
    service.config_path.write_text(
        service.config_path.read_text() + "\npoll_idle_seconds=19\n"
    )
    observed = await request(service.config, make_envelope("status.get", "*", {}))
    assert observed["data"]["runtime"]["config_sha256"] == original


async def test_child_cannot_submit_retyped_plan(running_service):
    host, task = await start(running_service, deliverable_kind="plan")
    host.caller = task["child"]["childThreadId"]
    result = await command(
        running_service.config,
        "finish",
        {
            "task_id": task["id"],
            "report": dict(report(task), deliverable_kind="implementation"),
        },
        host,
    )
    assert result["blocked_reason"] == "deliverable_kind_conflict"
    assert not result.get("submitted_report")


async def test_maintenance_waits_for_review_entered_first(running_service, monkeypatch):
    import asyncio
    import threading

    from oraculo.cli import make_envelope, request

    entered, release = threading.Event(), threading.Event()
    original = running_service.reviews.open

    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(running_service.reviews, "open", delayed)
    pending = asyncio.create_task(
        request(running_service.config, make_envelope("review.open", "alpha", {}))
    )
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        result = await request(
            running_service.config,
            make_envelope("maintenance.begin", "*", {"operation_id": "race"}),
        )
        assert not result["ok"]
        assert result["error"]["message"] == "maintenance_memory_operation_active"
    finally:
        release.set()
        await pending


def test_loaded_config_hash_precedes_service_construction(tmp_path):
    import hashlib

    from test_runtime_install import config

    from oraculo.config import Config
    from oraculo.service import Service

    path = config(tmp_path / "config.toml")
    loaded = Config.load(path)
    expected = hashlib.sha256(path.read_bytes()).hexdigest()
    path.write_text(path.read_text() + "poll_idle_seconds = 19\n")
    service = Service(loaded, path)
    try:
        assert service.config.poll_idle_seconds == 60
        assert service.status("*")["runtime"]["config_sha256"] == expected
    finally:
        service.state.close()


async def test_unreserved_legacy_execution_blocks_all_admission_until_terminal(
    running_service,
):
    from oraculo.managed_tasks import ManagedTasks
    from oraculo.task_transport import SocketMemory

    service = running_service
    initialize(service.config.projects_root / "alpha")
    host = Caller(service.config.projects_root)
    store = TaskStore(service.config.state_dir / "managed-tasks.sqlite3")
    try:
        legacy = ManagedTasks(service.config, store, host, SocketMemory(service.config))
        task = await legacy.command(
            "start",
            dict(
                kind="work",
                key="legacy",
                text="legacy fixture",
                cwd=str(service.config.projects_root / "alpha"),
            ),
        )
    finally:
        store.close()
    # First service read claims the old ledger but never invents a reservation.
    await command(service.config, "status", {"task_id": task["id"]}, None)
    capacity = service.state.capacity()
    assert capacity["occupied"] == 0 and capacity["available"] == 0
    assert capacity["untracked_managed_tasks"] == [task["id"]]
    blocked = await command(
        service.config,
        "start",
        dict(
            kind="work",
            key="new",
            text="new fixture",
            cwd=str(service.config.projects_root / "alpha"),
        ),
        host,
    )
    assert (
        blocked["blocked_reason"] == "managed_legacy_execution_requires_reconciliation"
    )
    assert len(host.created) == 1
    host.complete(task["child"]["childThreadId"], status="failed")
    finished = await command(
        service.config, "finish", {"task_id": task["id"], "report": report(task)}, host
    )
    assert finished["work_status"] == "failed"
    assert service.state.capacity()["available"] == service.config.max_executors
