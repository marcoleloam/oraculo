"""Per-scope concurrency: host waits must not freeze other tasks."""

import asyncio
import concurrent.futures
import json
import logging
import threading
import time

import pytest
from test_managed_disconnect import open_operation, settled
from test_managed_service import Caller
from test_managed_tasks import initialize

from oraculo.cli import (
    MEMORY_CYCLE_TIMEOUT,
    _request_timeout,
    make_envelope,
    request,
    run_managed_attempts,
    task_error_receipt,
)
from oraculo.managed_service import _LoopConnection, command
from oraculo.managed_tasks import ManagedTasks
from oraculo.state import State, StateError
from oraculo.stdio import print_json
from oraculo.task_store import TaskStore


def _origin(root, thread_id):
    host = Caller(root)
    host.caller = thread_id
    host.threads[thread_id] = dict(host.threads["origin"], threadId=thread_id)
    return host


def _start_payload(service, key):
    return dict(
        kind="work",
        key=key,
        text="Independent work",
        cwd=str(service.config.projects_root / "alpha"),
    )


async def _start(service, host, key):
    return await command(service.config, "start", _start_payload(service, key), host)


async def test_two_origins_progress_while_one_ack_is_held(running_service):
    service = running_service
    service.state.max_executors = 3
    initialize(service.config.projects_root / "alpha")
    root = service.config.projects_root
    first_host, second_host = _origin(root, "origin-a"), _origin(root, "origin-b")
    second_host.child_origin = 100
    release = asyncio.Event()
    entered = asyncio.Event()
    original = first_host.call

    async def delayed(name, args):
        if name == "orchestrator_capabilities":
            entered.set()
            await release.wait()
        return await original(name, args)

    first_host.call = delayed
    first = asyncio.create_task(_start(service, first_host, "key-a"))
    await asyncio.wait_for(entered.wait(), 2)
    second = await asyncio.wait_for(_start(service, second_host, "key-b"), 5)
    assert second["phase"] == "executing"
    assert not first.done()
    release.set()
    started = await first
    assert started["phase"] == "executing"
    assert started["id"] != second["id"]
    assert len(first_host.created) == 1 and len(second_host.created) == 1


async def test_publish_scope_does_not_block_another_start(running_service):
    service = running_service
    service.state.max_executors = 3
    initialize(service.config.projects_root / "alpha")
    host = Caller(service.config.projects_root)
    task = await _start(service, host, "published")
    reader, writer = await open_operation(
        service,
        action="publish",
        payload={"task_id": task["id"], "publication": {}},
    )
    frame = json.loads(await asyncio.wait_for(reader.readline(), 2))
    assert frame["tool"] == "orchestrator_capabilities"
    other = await asyncio.wait_for(_start(service, host, "beside-publish"), 5)
    assert other["phase"] == "executing" and other["id"] != task["id"]
    diag = service.managed_service.diagnostic()
    assert any(op["task_id"] == task["id"] for op in diag["operations"])
    writer.close()
    await writer.wait_closed()
    await settled(service)


async def test_same_intent_serializes_and_stays_idempotent(
    running_service, caplog
):
    service = running_service
    initialize(service.config.projects_root / "alpha")
    host = Caller(service.config.projects_root)
    release = asyncio.Event()
    entered = asyncio.Event()
    original = host.call

    async def delayed(name, args):
        if name == "delegate_task":
            entered.set()
            await release.wait()
        return await original(name, args)

    host.call = delayed
    first = asyncio.create_task(_start(service, host, "same-key"))
    await asyncio.wait_for(entered.wait(), 3)
    with pytest.raises(StateError, match="managed_scope_busy") as caught:
        await asyncio.wait_for(_start(service, host, "same-key"), 2)
    assert caught.value.operation_id
    assert caught.value.retryable is True
    release.set()
    with caplog.at_level(logging.INFO, logger="oraculo"):
        started = await first
    replay = await _start(service, host, "same-key")
    assert replay["id"] == started["id"] and started["phase"] == "executing"
    assert len(host.created) == 1
    events = [
        record.message
        for record in caplog.records
        if "managed_operation_finished" in record.message
    ]
    assert events
    assert "duration_seconds" in events[-1] and "wait_seconds" in events[-1]


async def test_concurrent_starts_at_capacity_do_not_oversubscribe(running_service):
    service = running_service
    service.config.max_executors = service.state.max_executors = 2
    service.state.provider_limits["codex"] = 2
    service.state.set_capacity(2)
    initialize(service.config.projects_root / "alpha")
    host = Caller(service.config.projects_root)
    release = asyncio.Event()
    entered = []
    original = host.call

    async def delayed(name, args):
        if name == "delegate_task":
            entered.append(args.get("clientRequestId"))
            await release.wait()
        return await original(name, args)

    host.call = delayed
    tasks = [
        asyncio.create_task(_start(service, host, f"edge-{number}"))
        for number in range(6)
    ]
    try:
        async with asyncio.timeout(8):
            while not (
                len(entered) == 2 and sum(task.done() for task in tasks) >= 4
            ):
                if len(entered) > 2:
                    break
                await asyncio.sleep(0.01)
        assert len(entered) == 2
        occupied = service.state.connection.execute(
            "SELECT count(*) FROM managed_reservations"
        ).fetchone()[0]
        assert occupied == 2
        assert service.state.capacity()["occupied"] == 2
    finally:
        release.set()
    results = await asyncio.gather(*tasks)
    executing = [item for item in results if item["phase"] == "executing"]
    waiting = [
        item
        for item in results
        if item.get("blocked_reason") == "managed_capacity_wait"
    ]
    assert len(executing) == 2 and len(waiting) == 4
    assert len(host.created) == 2
    assert service.state.capacity()["occupied"] == 2
    reserved = service.state.connection.execute(
        "SELECT count(*) FROM managed_reservations"
    ).fetchone()[0]
    assert reserved == 2


async def test_background_reconcile_does_not_block_other_callers(
    running_service, monkeypatch
):
    service = running_service
    monkeypatch.setattr("oraculo.managed_service.MAX_QUEUED_CALLERS", 1)
    store = TaskStore(service.config.state_dir / "managed-tasks.sqlite3")
    held = store.create(
        {"thread_id": "origin", "run_id": "run"}, "held", {"text": "fixture"}
    )["id"]
    store.close()
    entered, release = threading.Event(), threading.Event()

    async def slow(self, task_id):
        entered.set()
        assert release.wait(3)

    monkeypatch.setattr(ManagedTasks, "reconcile", slow)
    await service.reconcile_managed_tasks(None)
    assert await asyncio.to_thread(entered.wait, 1)
    service.state.max_executors = 2
    initialize(service.config.projects_root / "alpha")
    host = Caller(service.config.projects_root)
    try:
        started = await asyncio.wait_for(_start(service, host, "during-reconcile"), 5)
        assert started["phase"] == "executing"
        assert service.managed_service.busy
        with pytest.raises(StateError, match="managed_scope_busy"):
            await command(
                service.config,
                "progress",
                {"task_id": held, "message": "still held"},
                host,
            )
        service.managed_flush.cancel()
        await asyncio.sleep(0.05)
        assert service.managed_service.busy
        assert any(
            op["task_id"] == held and op["mode"] == "background"
            for op in service.managed_service.diagnostic()["operations"]
        )
    finally:
        release.set()
        await asyncio.gather(service.managed_flush, return_exceptions=True)
        await settled(service)


def test_atomic_transactions_do_not_interleave(tmp_path):
    import concurrent.futures

    loop = asyncio.new_event_loop()
    ready = threading.Event()

    def run_loop():
        asyncio.set_event_loop(loop)
        loop.call_soon(ready.set)
        loop.run_forever()

    owner = threading.Thread(target=run_loop, name="ledger-owner", daemon=True)
    owner.start()
    assert ready.wait(2)
    created = concurrent.futures.Future()

    def setup():
        try:
            state = State(tmp_path / "state.db", max_executors=4)
            state.connection.execute(
                "CREATE TABLE probe(owner TEXT NOT NULL, n INTEGER NOT NULL)"
            )
            state.connection = _LoopConnection(
                state.connection, loop, threading.current_thread()
            )
            created.set_result(state)
        except BaseException as exc:
            created.set_exception(exc)

    loop.call_soon_threadsafe(setup)
    state = created.result(3)
    observations = []
    errors = []
    guard = threading.Lock()

    def write(name, other):
        def body():
            for number in range(20):
                state.connection.execute(
                    "INSERT INTO probe VALUES(?, ?)", (name, number)
                )
                count = state.connection.execute(
                    "SELECT count(*) FROM probe WHERE owner=?", (other,)
                ).fetchone()[0]
                with guard:
                    observations.append(count)

        try:
            state.atomic(body)
        except BaseException as exc:
            errors.append(exc)

    workers = [
        threading.Thread(target=write, args=("a", "b")),
        threading.Thread(target=write, args=("b", "a")),
    ]
    try:
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(5)
            assert not worker.is_alive()
        assert not errors
        assert observations
        assert set(observations) <= {0, 20}
        total = state.connection.execute("SELECT count(*) FROM probe").fetchone()[0]
        assert total == 40
        state.connection.close()
    finally:
        loop.call_soon_threadsafe(loop.stop)
        owner.join(2)


async def test_cancel_one_caller_does_not_release_another_scope(running_service):
    service = running_service
    service.state.max_executors = 3
    initialize(service.config.projects_root / "alpha")
    host = Caller(service.config.projects_root)
    release = asyncio.Event()
    entered = []
    original = host.call

    async def delayed(name, args):
        if name == "delegate_task":
            entered.append(args.get("clientRequestId"))
            await release.wait()
        return await original(name, args)

    host.call = delayed
    first = asyncio.create_task(_start(service, host, "one"))
    second = asyncio.create_task(_start(service, host, "two"))
    third = None
    try:
        async with asyncio.timeout(5):
            while len(entered) < 2:
                await asyncio.sleep(0.01)
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        async with asyncio.timeout(2):
            while len(service.managed_service.diagnostic()["operations"]) != 1:
                await asyncio.sleep(0.01)
        with pytest.raises(StateError, match="managed_scope_busy"):
            await asyncio.wait_for(_start(service, host, "two"), 2)
        third = asyncio.create_task(_start(service, host, "three"))
        async with asyncio.timeout(5):
            while len(entered) < 3:
                await asyncio.sleep(0.01)
        with pytest.raises(StateError, match="managed_scope_busy"):
            await asyncio.wait_for(_start(service, host, "two"), 2)
    finally:
        release.set()
        await second
        if third is not None:
            await third
    assert len(host.created) == 2


async def test_cli_retries_retryable_task_errors_and_prints_stdout(capsys, monkeypatch):
    async def instant(_seconds):
        return None

    monkeypatch.setattr("oraculo.cli._retry_sleep", instant)
    calls = {"n": 0}

    async def succeed_on_third():
        calls["n"] += 1
        if calls["n"] < 3:
            raise StateError("managed_adapter_busy_retry_same_key")
        return {"phase": "executing"}

    assert (await run_managed_attempts(succeed_on_third))["phase"] == "executing"
    assert calls["n"] == 3

    async def not_retryable():
        calls["n"] += 1
        raise StateError("task_brief_required")

    calls["n"] = 0
    with pytest.raises(StateError, match="task_brief_required"):
        await run_managed_attempts(not_retryable)
    assert calls["n"] == 1

    async def always_busy():
        exc = StateError("managed_scope_busy")
        exc.retryable = True
        exc.operation_id = "op-1"
        exc.scope = {"kind": "task", "task_id": "task-1"}
        raise exc

    with pytest.raises(StateError, match="managed_scope_busy"):
        await run_managed_attempts(always_busy)

    async def memory_then_ok():
        calls["n"] += 1
        if calls["n"] < 2:
            exc = StateError("memory_cycle_timeout")
            exc.retryable = True
            raise exc
        return {"phase": "executing"}

    calls["n"] = 0
    assert (await run_managed_attempts(memory_then_ok))["phase"] == "executing"
    assert calls["n"] == 2
    bare = StateError("memory_cycle_timeout")
    assert task_error_receipt(bare)["error"]["retryable"] is True

    receipt = task_error_receipt(StateError("managed_adapter_busy_retry_same_key"))
    assert receipt["ok"] is False
    assert receipt["error"]["retryable"] is True
    assert receipt["error"]["code"] == "StateError"
    print_json(receipt)
    stdout = capsys.readouterr().out
    assert stdout.strip().startswith("{")
    assert "managed_adapter_busy_retry_same_key" in stdout


def test_memory_cycle_budget_is_not_the_fast_path():
    assert _request_timeout("memory.cycle.begin") == MEMORY_CYCLE_TIMEOUT
    assert _request_timeout("memory.cycle.read") == MEMORY_CYCLE_TIMEOUT
    assert MEMORY_CYCLE_TIMEOUT >= 60
    assert _request_timeout("status.get") == 5
    assert _request_timeout("rollout.verify") == 125


def _owner_loop():
    loop = asyncio.new_event_loop()
    ready = threading.Event()

    def run_loop():
        asyncio.set_event_loop(loop)
        loop.call_soon(ready.set)
        loop.run_forever()

    owner = threading.Thread(target=run_loop, name="ledger-owner", daemon=True)
    owner.start()
    assert ready.wait(2)
    return loop, owner


def _call_on_loop(loop, fn):
    done = concurrent.futures.Future()

    def wrapped():
        try:
            done.set_result(fn())
        except BaseException as exc:
            done.set_exception(exc)

    loop.call_soon_threadsafe(wrapped)
    return done.result(3)


def _rollback_race(state, write, surviving):
    """One open transaction rolls back; the other operation's write remains.

    On a shared hopped connection, transaction() joins in_transaction and the
    rollback erases both. Separate WAL connections must not do that.
    """
    opened = threading.Event()
    joined = threading.Event()
    errors = []

    def first():
        try:
            with state.transaction():
                write("a")
                opened.set()
                joined.wait(2)
                raise RuntimeError("rollback")
        except RuntimeError:
            return
        except BaseException as exc:
            errors.append(exc)

    def second():
        if not opened.wait(2):
            errors.append(TimeoutError("writer never opened"))
            return
        try:
            with state.transaction():
                write("b")
                joined.set()
        except BaseException as exc:
            errors.append(exc)

    workers = [
        threading.Thread(target=first),
        threading.Thread(target=second),
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(8)
        assert not worker.is_alive()
    assert not errors
    assert surviving() == ["b"]


def test_concurrent_rollback_preserves_the_other_write(tmp_path):
    loop, owner = _owner_loop()
    admission = None
    ledger = None
    try:
        admission = _call_on_loop(
            loop, lambda: State(tmp_path / "admission.db", max_executors=4)
        )

        def wrap_admission():
            admission.connection = _LoopConnection(
                admission.connection, loop, threading.current_thread()
            )

        _call_on_loop(loop, wrap_admission)

        def write_reservation(name):
            admission.connection.execute(
                "INSERT INTO managed_reservations VALUES(?,?,?,?,NULL)",
                (name, "codex", 1, "reserved"),
            )

        def reservation_ids():
            rows = admission.connection.execute(
                "SELECT id FROM managed_reservations ORDER BY id"
            ).fetchall()
            return [row["id"] for row in rows]

        _rollback_race(admission, write_reservation, reservation_ids)

        def open_ledger():
            store = TaskStore(tmp_path / "ledger.db")
            store.db = _LoopConnection(
                store.state.connection, loop, threading.current_thread()
            )
            return store

        ledger = _call_on_loop(loop, open_ledger)

        def write_task(name):
            ledger.db.execute(
                "INSERT INTO managed_tasks VALUES(?,?,?,?,?)",
                (name, "origin", name, "hash", "{}"),
            )

        def task_ids():
            rows = ledger.db.execute(
                "SELECT id FROM managed_tasks ORDER BY id"
            ).fetchall()
            return [row["id"] for row in rows]

        _rollback_race(ledger.state, write_task, task_ids)
    finally:
        if ledger is not None:
            _call_on_loop(loop, ledger.close)
        if admission is not None:
            _call_on_loop(loop, admission.close)
        loop.call_soon_threadsafe(loop.stop)
        owner.join(2)


async def test_start_reuses_identical_project_and_thread_proofs(running_service):
    service = running_service
    service.state.max_executors = 2
    initialize(service.config.projects_root / "alpha")
    host = Caller(service.config.projects_root)
    task = await _start(service, host, "frames")
    assert task["phase"] == "executing"
    lists = [args for name, args in host.calls if name == "t3_project_list"]
    assert len(lists) == 1
    origin_reads = [
        args
        for name, args in host.calls
        if name == "t3_thread_read"
        and args.get("threadId") == "origin"
        and args.get("runLimit") == 1
    ]
    assert len(origin_reads) == 1
    # A later resume is a new operation: the project-binding gate reads again.
    await command(
        service.config, "resume", {"task_id": task["id"]}, host
    )
    lists_after = [args for name, args in host.calls if name == "t3_project_list"]
    assert len(lists_after) >= 2


async def test_memory_proceeds_while_managed_worker_blocks_its_pool(running_service):
    service = running_service
    loop = asyncio.get_running_loop()
    small = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    loop.set_default_executor(small)
    reader, writer = await open_operation(service)
    try:
        frame = json.loads(await asyncio.wait_for(reader.readline(), 2))
        assert frame["tool"] == "orchestrator_capabilities"
        assert service.managed_service.busy
        envelope = make_envelope(
            "memory.cycle.begin",
            "alpha",
            {"key": "while-managed", "query": "served while a task waits"},
        )
        envelope["origin"] = {"thread_id": "direct", "run_id": "run"}
        started = time.monotonic()
        reply = await asyncio.wait_for(request(service.config, envelope), 3)
        assert reply["ok"], reply
        status = await asyncio.wait_for(
            request(service.config, make_envelope("status.get", "*", {})),
            1,
        )
        assert status["ok"]
        assert time.monotonic() - started < 3
        assert service.managed_service.busy
    finally:
        writer.close()
        await writer.wait_closed()
        await settled(service)
        loop.set_default_executor(
            concurrent.futures.ThreadPoolExecutor(max_workers=8)
        )
        small.shutdown(wait=False, cancel_futures=True)
