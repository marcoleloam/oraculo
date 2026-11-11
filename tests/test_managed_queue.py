"""Callers of different scopes proceed; one scope and the caller cap do not."""

import asyncio
import json

from test_managed_disconnect import open_operation, settled
from test_managed_service import Caller
from test_managed_tasks import initialize

from oraculo.managed_service import command


async def close(writer):
    writer.close()
    await writer.wait_closed()


async def test_different_scopes_progress_while_one_ack_is_open(running_service):
    service = running_service
    first_reader, first = await open_operation(
        service, action="progress", payload={"task_id": "task-a", "message": "a"}
    )
    first_frame = json.loads(await first_reader.readline())
    second_reader, second = await open_operation(
        service, action="progress", payload={"task_id": "task-b", "message": "b"}
    )
    second_frame = json.loads(await asyncio.wait_for(second_reader.readline(), 2))
    assert first_frame["tool"] == second_frame["tool"] == "orchestrator_capabilities"
    assert first_frame["operation_id"] != second_frame["operation_id"]
    diag = service.managed_service.diagnostic()
    assert len(diag["operations"]) == 2
    assert diag["queued_callers"] == 0
    await service.reconcile_managed_tasks(None)
    assert len(service.managed_service.diagnostic()["operations"]) == 2
    await close(first)
    await close(second)
    await settled(service)
    assert (
        service.managed_service.store.db.execute(
            "SELECT count(*) FROM managed_tasks"
        ).fetchone()[0]
        == 0
    )


async def test_same_scope_returns_operation_id_without_a_global_busy(
    running_service,
):
    service = running_service
    owner_reader, owner = await open_operation(
        service, action="progress", payload={"task_id": "task-a", "message": "a"}
    )
    frame = json.loads(await owner_reader.readline())
    other_reader, other = await open_operation(
        service, action="progress", payload={"task_id": "task-a", "message": "again"}
    )
    reply = json.loads(await asyncio.wait_for(other_reader.readline(), 2))
    assert reply["error"]["message"] == "managed_scope_busy"
    assert reply["error"]["retryable"] is True
    assert reply["error"]["operation_id"] == frame["operation_id"]
    assert reply["error"]["scope"] == {"kind": "task", "task_id": "task-a"}
    facade = service.managed_service
    assert facade.busy and facade.diagnostic()["operation_id"] == frame["operation_id"]
    assert facade.diagnostic()["queued_callers"] == 0
    await close(other)
    await close(owner)
    await settled(service)


async def test_disconnect_releases_only_that_scope(running_service):
    service = running_service
    owner_reader, owner = await open_operation(
        service, action="progress", payload={"task_id": "task-a", "message": "a"}
    )
    await owner_reader.readline()
    other_reader, other = await open_operation(
        service, action="progress", payload={"task_id": "task-b", "message": "b"}
    )
    other_frame = json.loads(await asyncio.wait_for(other_reader.readline(), 2))
    await close(owner)
    async with asyncio.timeout(2):
        while len(service.managed_service.diagnostic()["operations"]) != 1:
            await asyncio.sleep(0.01)
    remaining = service.managed_service.diagnostic()["operations"][0]
    assert remaining["operation_id"] == other_frame["operation_id"]
    assert remaining["scope"] == {"kind": "task", "task_id": "task-b"}
    await close(other)
    await settled(service)


async def test_caller_cap_rejects_excess_without_the_scope_error(
    running_service, monkeypatch
):
    monkeypatch.setattr("oraculo.managed_service.MAX_QUEUED_CALLERS", 1)
    service = running_service
    reader, owner = await open_operation(
        service, action="progress", payload={"task_id": "task-a", "message": "a"}
    )
    await reader.readline()
    excess_reader, excess = await open_operation(
        service, action="progress", payload={"task_id": "task-b", "message": "b"}
    )
    reply = json.loads(await asyncio.wait_for(excess_reader.readline(), 2))
    assert reply["error"]["message"] == "managed_adapter_busy_retry_same_key"
    assert reply["error"]["retryable"] is True
    assert "operation_id" not in reply["error"]
    await close(excess)
    await close(owner)
    await settled(service)
    again_reader, again = await open_operation(
        service, action="progress", payload={"task_id": "task-b", "message": "b"}
    )
    assert (
        json.loads(await asyncio.wait_for(again_reader.readline(), 2))["tool"]
        == "orchestrator_capabilities"
    )
    await close(again)
    await settled(service)


async def test_cancelling_one_connection_keeps_the_other_scope(running_service):
    service = running_service
    first_reader, first = await open_operation(
        service, action="progress", payload={"task_id": "task-a", "message": "a"}
    )
    await first_reader.readline()
    first_owner = service.managed_service.owner
    second_reader, second = await open_operation(
        service, action="progress", payload={"task_id": "task-b", "message": "b"}
    )
    second_frame = json.loads(await asyncio.wait_for(second_reader.readline(), 2))
    first_owner.cancel()
    await asyncio.gather(first_owner, return_exceptions=True)
    async with asyncio.timeout(2):
        while len(service.managed_service.diagnostic()["operations"]) != 1:
            await asyncio.sleep(0.01)
    remaining = service.managed_service.diagnostic()["operations"][0]
    assert remaining["operation_id"] == second_frame["operation_id"]
    assert remaining["task_id"] == "task-b"
    await close(first)
    await close(second)
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
        await asyncio.sleep(0)
    finally:
        release.set()
    results = await asyncio.gather(first, second)
    assert all(r["phase"] == "executing" for r in results), results
    assert len({r["id"] for r in results}) == 2
    assert len({r["child"]["childThreadId"] for r in results}) == 2
    assert service.state.capacity()["occupied"] == 2
