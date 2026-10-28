"""Real Unix callers share the writer in FIFO order; work remains independent."""

import asyncio
import json

from test_managed_disconnect import open_operation, settled
from test_managed_service import Caller
from test_managed_tasks import initialize

from oraculo.managed_service import command


async def wait_for_queue(service, count):
    async with asyncio.timeout(2):
        while service.managed_service.diagnostic()["queued_callers"] != count:
            await asyncio.sleep(0.005)


async def close(writer):
    writer.close()
    await writer.wait_closed()


async def test_concurrent_callers_wait_fifo_without_releasing_current_owner(
    running_service,
):
    service = running_service
    first_reader, first = await open_operation(service)
    first_frame = json.loads(await first_reader.readline())
    second_reader, second = await open_operation(service)
    third_reader, third = await open_operation(service)
    await wait_for_queue(service, 2)
    facade = service.managed_service
    assert facade.diagnostic()["operation_id"] == first_frame["operation_id"]
    assert not second_reader._buffer and not third_reader._buffer
    # A background tick must not jump ahead of waiting interactive callers.
    await service.reconcile_managed_tasks(None)
    assert facade.diagnostic()["operation_id"] == first_frame["operation_id"]
    await close(first)
    second_frame = json.loads(await asyncio.wait_for(second_reader.readline(), 2))
    assert second_frame["tool"] == "orchestrator_capabilities"
    assert not third_reader._buffer
    await close(second)
    third_frame = json.loads(await asyncio.wait_for(third_reader.readline(), 2))
    assert third_frame["tool"] == "orchestrator_capabilities"
    await close(third)
    await settled(service)
    assert not facade.waiters
    assert (
        facade.store.db.execute("SELECT count(*) FROM managed_tasks").fetchone()[0] == 0
    )


async def test_queue_timeout_preserves_owner_and_has_no_task_effect(
    running_service,
    monkeypatch,
):
    monkeypatch.setattr("oraculo.managed_service.CALLER_QUEUE_TIMEOUT", 0.03)
    service = running_service
    owner_reader, owner = await open_operation(service)
    frame = json.loads(await owner_reader.readline())
    queued_reader, queued = await open_operation(service)
    reply = json.loads(await asyncio.wait_for(queued_reader.readline(), 2))
    assert reply["error"]["message"] == "managed_adapter_busy_retry_same_key"
    facade = service.managed_service
    assert facade.busy and facade.diagnostic()["operation_id"] == frame["operation_id"]
    assert not facade.waiters
    await close(queued)
    await close(owner)
    await settled(service)


async def test_disconnected_waiter_is_skipped_before_host_call(running_service):
    service = running_service
    reader, owner = await open_operation(service)
    await reader.readline()
    _, disconnected = await open_operation(service)
    next_reader, next_writer = await open_operation(service)
    await wait_for_queue(service, 2)
    await close(disconnected)
    await asyncio.sleep(0.01)
    await close(owner)
    frame = json.loads(await asyncio.wait_for(next_reader.readline(), 2))
    assert frame["tool"] == "orchestrator_capabilities"
    await close(next_writer)
    await settled(service)


async def test_bounded_queue_rejects_excess_without_affecting_waiters(
    running_service,
    monkeypatch,
):
    monkeypatch.setattr("oraculo.managed_service.MAX_QUEUED_CALLERS", 1)
    service = running_service
    reader, owner = await open_operation(service)
    await reader.readline()
    next_reader, queued = await open_operation(service)
    await wait_for_queue(service, 1)
    excess_reader, excess = await open_operation(service)
    reply = json.loads(await asyncio.wait_for(excess_reader.readline(), 2))
    assert reply["error"]["message"] == "managed_adapter_busy_retry_same_key"
    await close(excess)
    await close(owner)
    assert (
        json.loads(await next_reader.readline())["tool"] == "orchestrator_capabilities"
    )
    await close(queued)
    await settled(service)


async def test_cancelled_waiter_after_wakeup_does_not_strand_next_caller(
    running_service,
):
    service = running_service
    reader, owner = await open_operation(service)
    await reader.readline()
    facade = service.managed_service
    first = asyncio.create_task(facade.acquire_caller())
    second = asyncio.create_task(facade.acquire_caller())
    await wait_for_queue(service, 2)
    first.cancel()
    await asyncio.gather(first, return_exceptions=True)
    assert facade.busy and len(facade.waiters) == 1
    await close(owner)
    await asyncio.wait_for(second, 2)
    assert not facade.waiters
    await settled(service)


async def test_simultaneous_starts_launch_independent_running_tasks(running_service):
    service = running_service
    service.state.max_executors = 3
    initialize(service.config.projects_root / "alpha")
    host = Caller(service.config.projects_root)
    entered, release = asyncio.Event(), asyncio.Event()
    call = host.call

    async def delayed(name, args):
        if name == "orchestrator_capabilities" and not entered.is_set():
            entered.set()
            await release.wait()
        return await call(name, args)

    host.call = delayed

    async def start(key):
        return await command(
            service.config,
            "start",
            dict(
                kind="work",
                key=key,
                text="Independent work",
                cwd=str(service.config.projects_root / "alpha"),
            ),
            host,
        )

    first = asyncio.create_task(start("work-one"))
    second = None
    try:
        await asyncio.wait_for(entered.wait(), 2)
        second = asyncio.create_task(start("work-two"))
        await wait_for_queue(service, 1)
    finally:
        release.set()
    results = await asyncio.gather(first, second)
    assert all(r["phase"] == "executing" for r in results), results
    assert len({r["id"] for r in results}) == 2
    assert len({r["child"]["childThreadId"] for r in results}) == 2
    assert service.state.capacity()["occupied"] == 2
