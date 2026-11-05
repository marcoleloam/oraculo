"""Regression coverage for per-job daemon isolation and admission accounting."""

import asyncio
import json
import logging
import threading
import time
from pathlib import Path

import pytest
from test_managed_service import Caller
from test_managed_tasks import initialize

from oraculo.cli import make_envelope, request
from oraculo.managed_service import ManagedService, command
from oraculo.protocol import canonical_json
from oraculo.state import StateError
from oraculo.task_store import TaskStore


class Remote:
    def __init__(self):
        self.sent = []

    async def send(self, thread, message, key, **kwargs):
        self.sent.append(json.loads(message))
        return {"runId": "dispatch-run"}


def enqueue(service, project_id, text, kind, payload):
    objective = service.state.submit_objective(project_id, text, dedupe_key=text)
    return service.state.enqueue_job(objective["id"], kind, payload)


async def test_d1_unregistered_project_blocks_that_job_only(running_service):
    """D1: project_not_registered_or_outside_scope blocks one job and continues."""
    service = running_service
    beta = service.config.projects_root / "beta"
    beta.mkdir()
    service.publisher.initialize_project("beta")
    service.state.register_project("beta", str(beta))
    service.state.set_coordinator("beta", "coordinator")
    blocked_job = enqueue(
        service,
        "alpha",
        "alpha first",
        "implementation",
        {"text": "alpha first"},
    )
    later_job = enqueue(
        service,
        "beta",
        "beta second",
        "implementation",
        {"text": "beta second"},
    )
    service.state.register_project(
        "alpha", str(service.config.projects_root / "alpha"), "unregistered"
    )
    remote = Remote()
    await service.dispatch(remote)
    blocked = service.state.get_job(blocked_job["id"])
    assert blocked["state"] == "blocked"
    assert service.state.get_meta(f"blocker:{blocked_job['id']}")["reason"] == (
        "project_not_registered_or_outside_scope"
    )
    assert [body["job_id"] for body in remote.sent] == [later_job["id"]]
    assert service.state.get_job(later_job["id"])["state"] == "queued"


async def test_d1_missing_ingest_source_blocks_that_job_only(running_service):
    """D1: FileNotFoundError from a deleted source_path does not stop dispatch."""
    service = running_service
    missing = service.config.projects_root / "alpha" / "gone.md"
    blocked_job = enqueue(
        service,
        "alpha",
        "ingest missing",
        "memory",
        {
            "text": "ingest missing",
            "kind": "memory",
            "memory_operation": "ingest",
            "source_path": str(missing),
        },
    )
    later_job = enqueue(
        service,
        "alpha",
        "implementation later",
        "implementation",
        {"text": "implementation later"},
    )
    remote = Remote()
    await service.dispatch(remote)
    blocked = service.state.get_job(blocked_job["id"])
    blocker = service.state.get_meta(f"blocker:{blocked_job['id']}")
    assert blocked["state"] == "blocked"
    assert blocker["error_type"] == "FileNotFoundError"
    assert [body["job_id"] for body in remote.sent] == [later_job["id"]]


async def test_d4_ingest_source_is_bounded_off_the_event_loop(
    running_service, monkeypatch
):
    """D4: stat and a max+1 read run off-loop and reject an oversized source."""
    service = running_service
    project = service.project("alpha")
    path = Path(project["path"]) / "sized.md"
    path.write_text("hello memory\n", encoding="utf-8")
    limit = service.config.memory.max_proposal_bytes
    main = threading.get_ident()
    window = {}
    real_open = Path.open

    def tracking_open(self, *args, **kwargs):
        handle = real_open(self, *args, **kwargs)
        if getattr(self, "name", None) != "sized.md":
            return handle

        class Bounded:
            def read(self, size=-1):
                window["thread"] = threading.get_ident()
                window["size"] = size
                window["start"] = time.monotonic()
                time.sleep(0.3)
                window["end"] = time.monotonic()
                return handle.read(size)

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                handle.close()
                return False

        return Bounded()

    monkeypatch.setattr(Path, "open", tracking_open)
    ticks = []

    async def ticker():
        while "end" not in window:
            ticks.append(time.monotonic())
            await asyncio.sleep(0.02)

    ticker_task = asyncio.create_task(ticker())
    context = await service.memory_context(
        project,
        {
            "text": "size the source",
            "memory_operation": "ingest",
            "source_path": str(path),
        },
    )
    await ticker_task
    during = [tick for tick in ticks if window["start"] < tick < window["end"]]
    assert context["source_content"] == "hello memory\n"
    assert window["thread"] != main
    assert window["size"] == limit + 1
    assert len(during) >= 3

    huge = Path(project["path"]) / "huge.md"
    huge.write_bytes(b"x" * (limit + 1))
    with pytest.raises(StateError, match="source_too_large"):
        await service.memory_context(
            project,
            {
                "text": "too big",
                "memory_operation": "ingest",
                "source_path": str(huge),
            },
        )
    binary = Path(project["path"]) / "binary.md"
    binary.write_bytes(b"\xff\xfe not text")
    with pytest.raises(StateError, match="source_not_utf8"):
        await service.memory_context(
            project,
            {
                "text": "binary",
                "memory_operation": "ingest",
                "source_path": str(binary),
            },
        )


async def test_a3_slow_git_does_not_stall_the_daemon_loop(
    running_service, monkeypatch
):
    """A3: synchronous git inside managed.execute stays off the daemon loop."""
    from oraculo.managed_tasks import git as real_git

    service = running_service
    initialize(service.config.projects_root / "alpha")
    main = threading.get_ident()
    started = threading.Event()
    idents = []
    calls = {"n": 0}

    def slow_git(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            idents.append(threading.get_ident())
            started.set()
            time.sleep(0.6)
        return real_git(*args, **kwargs)

    monkeypatch.setattr("oraculo.managed_tasks.git", slow_git)
    host = Caller(service.config.projects_root)

    async def probe():
        assert await asyncio.to_thread(started.wait, 5)
        began = time.monotonic()
        reply = await request(
            service.config, make_envelope("runtime.probe", "alpha", {})
        )
        return time.monotonic() - began, reply

    probe_task = asyncio.create_task(probe())
    result = await command(
        service.config,
        "start",
        dict(
            kind="work",
            key="a3-loop",
            text="Implement",
            cwd=str(service.config.projects_root / "alpha"),
        ),
        host,
    )
    elapsed, reply = await probe_task
    assert reply["ok"] is True and reply["data"]["liveness"] is True
    assert elapsed < 0.4
    assert idents == [idents[0]] and idents[0] != main
    assert result["phase"] == "executing"


async def test_d2_cancel_unbound_legacy_restores_capacity(running_service):
    """D2: cancelling an unbound legacy task clears managed_legacy_untracked."""
    service = running_service
    initialize(service.config.projects_root / "alpha")
    origin = {"thread_id": "origin", "run_id": "origin-run"}
    store = TaskStore(service.config.state_dir / "managed-tasks.sqlite3")
    try:
        created = store.create(origin, "legacy-unbound", {"title": "legacy"})
        store.update(
            created["id"],
            "launch_pending",
            {"intent": "legacy"},
            launch_attempted=True,
        )
        task_id = created["id"]
    finally:
        store.close()
    assert service.managed_service is None
    service.managed_service = ManagedService(service)
    assert service.state.get_meta("managed_legacy_untracked") == [task_id]
    assert service.state.capacity()["available"] == 0
    cancelled = await command(
        service.config,
        "cancel",
        {"task_id": task_id},
        Caller(service.config.projects_root),
    )
    assert cancelled["work_status"] == "cancelled"
    assert not service.state.get_meta("managed_legacy_untracked")
    assert service.state.capacity()["available"] == service.config.max_executors
    assert service.state.capacity()["occupied"] == 0


async def test_d2_startup_scan_treats_cancelled_as_terminal(running_service):
    """D2: a cancelled legacy row is not re-added to managed_legacy_untracked."""
    service = running_service
    origin = {"thread_id": "origin", "run_id": "origin-run"}
    store = TaskStore(service.config.state_dir / "managed-tasks.sqlite3")
    try:
        created = store.create(origin, "cancelled-legacy", {"title": "cancelled"})
        store.update(
            created["id"],
            "cancelled",
            {"reason": "origin_cancelled"},
            work_status="cancelled",
            launch_attempted=True,
        )
        task_id = created["id"]
    finally:
        store.close()
    service.managed_service = ManagedService(service)
    assert task_id not in (service.state.get_meta("managed_legacy_untracked") or [])
    assert service.state.capacity()["available"] == service.config.max_executors


async def test_d8_response_write_failure_is_logged(running_service, caplog):
    """D8: a failed response write records request id, kind and error type."""
    service = running_service
    reader = asyncio.StreamReader()
    envelope = make_envelope("runtime.probe", "alpha", {})
    reader.feed_data(canonical_json(envelope).encode() + b"\n")
    reader.feed_eof()

    class Writer:
        def write(self, _data):
            raise ConnectionResetError("dropped")

        async def drain(self):
            raise ConnectionResetError("dropped")

    with caplog.at_level(logging.WARNING, logger="oraculo"):
        await service._connection(reader, Writer())
    assert "response_write_failed" in caplog.text
    assert envelope["request_id"] in caplog.text
    assert "runtime.probe" in caplog.text
    assert "ConnectionResetError" in caplog.text
