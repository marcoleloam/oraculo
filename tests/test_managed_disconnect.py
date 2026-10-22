"""Real temporary Unix service and clients; host effects are explicit fixtures."""

import asyncio
import json
import sys
import threading

import pytest
from test_managed_service import Caller
from test_managed_tasks import initialize

from oraculo.cli import make_envelope, request
from oraculo.managed_service import command
from oraculo.managed_tasks import ManagedTasks
from oraculo.protocol import MAX_ENVELOPE_BYTES, canonical_json
from oraculo.state import StateError
from oraculo.task_store import TaskStore


async def open_operation(service, action="start", payload=None):
    reader, writer = await asyncio.open_unix_connection(
        str(service.config.socket_path), limit=MAX_ENVELOPE_BYTES + 1
    )
    envelope = make_envelope(
        "managed.execute",
        "*",
        dict(protocol_version=1, action=action, payload=payload or {}),
    )
    writer.write(canonical_json(envelope).encode() + b"\n")
    await writer.drain()
    return reader, writer


async def settled(service):
    async with asyncio.timeout(3):
        while service.managed_service.busy:
            await asyncio.sleep(0.01)


def payload(service):
    return dict(
        kind="work",
        key="same-intent",
        text="Fixture",
        cwd=str(service.config.projects_root / "alpha"),
    )


async def acknowledge(writer, frame, result):
    writer.write(
        canonical_json(
            dict(
                operation_id=frame["operation_id"],
                request_id=frame["request_id"],
                result=result,
            )
        ).encode()
        + b"\n"
    )
    await writer.drain()


async def pump_until(reader, writer, host, tool):
    async with asyncio.timeout(5):
        while True:
            frame = json.loads(await reader.readline())
            assert frame.get("marker") == "ORACULO_SERVICE_HOST_CALL_V1", frame
            result = await host.call(frame["tool"], frame["arguments"])
            if frame["tool"] == tool:
                return frame, result
            await acknowledge(writer, frame, result)


async def test_eof_at_first_ack_releases_operation(running_service):
    service = running_service
    reader, writer = await open_operation(service)
    frame = json.loads(await reader.readline())
    assert frame["tool"] == "orchestrator_capabilities"
    assert service.managed_service.busy
    writer.transport.abort()
    await settled(service)
    # A separate request is admitted, not a mere observation of the flag.
    reader, writer = await open_operation(service)
    assert json.loads(await reader.readline())["tool"] == "orchestrator_capabilities"
    writer.close()
    await writer.wait_closed()
    await settled(service)


@pytest.mark.parametrize("early_cancel", [False, True])
async def test_cancelled_background_waiter_retains_busy_until_worker_exits(
    running_service, monkeypatch, early_cancel
):
    service = running_service
    store = TaskStore(service.config.state_dir / "managed-tasks.sqlite3")
    store.create({"thread_id": "origin", "run_id": "run"}, "key", {"text": "fixture"})
    store.close()
    entered, release = threading.Event(), threading.Event()

    async def slow(self, task_id):
        entered.set()
        assert release.wait(3)

    monkeypatch.setattr(ManagedTasks, "reconcile", slow)
    await service.reconcile_managed_tasks(None)
    if early_cancel:
        service.managed_flush.cancel()
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        service.managed_flush.cancel()
        await asyncio.sleep(0.03)
        assert service.managed_service.busy, "worker still owns SQLite/admission"
        with pytest.raises(StateError, match="managed_adapter_busy_retry_same_key"):
            await command(service.config, "start", {}, None)
    finally:
        release.set()
        await asyncio.gather(service.managed_flush, return_exceptions=True)
        await settled(service)


@pytest.mark.parametrize(
    "bad,reason",
    [
        (b"[]\n", "mismatch"),
        (b"null\n", "mismatch"),
        (b"not json\n", "invalid"),
        (b'{"operation_id":"wrong","result":{}}\n', "mismatch"),
    ],
)
async def test_malformed_ack_has_bounded_error_and_releases(
    running_service, bad, reason
):
    service = running_service
    reader, writer = await open_operation(service)
    await reader.readline()
    writer.write(bad)
    await writer.drain()
    response = json.loads(await reader.readline())
    assert response["error"]["message"] == "managed_host_ack_" + reason
    writer.close()
    await writer.wait_closed()
    await settled(service)


async def test_silent_ack_times_out_and_status_identifies_owner(
    running_service, monkeypatch
):
    monkeypatch.setattr("oraculo.managed_service.HOST_ACK_TIMEOUT", 0.15)
    service = running_service
    reader, writer = await open_operation(service)
    frame = json.loads(await reader.readline())
    status = await request(service.config, make_envelope("status.get", "*", {}))
    active = status["data"]["managed_adapter"]
    assert active["state"] == "active" and active["mode"] == "caller"
    assert active["phase"] == "host_ack" and active["client_eof"] is False
    assert active["operation_id"] == frame["operation_id"]
    assert active["host_request_id"] == frame["request_id"]
    assert active["timeout_seconds"] == 0.15
    other_reader, other_writer = await open_operation(service)
    busy = json.loads(await other_reader.readline())
    assert busy["error"]["message"] == "managed_adapter_busy_retry_same_key"
    assert busy["error"]["retryable"] is True
    assert busy["error"]["adapter"]["operation_id"] == frame["operation_id"]
    other_writer.close()
    await other_writer.wait_closed()
    response = json.loads(await reader.readline())
    assert response["error"]["message"] == "managed_host_ack_timeout"
    writer.close()
    await writer.wait_closed()
    await settled(service)


async def test_live_slow_ack_retains_operation_and_default_minutes(
    running_service, monkeypatch
):
    from oraculo.managed_service import HOST_ACK_TIMEOUT, HOST_FRAME_TIMEOUT
    from oraculo.task_transport import StdioHostTransport

    assert (
        HOST_FRAME_TIMEOUT
        > HOST_ACK_TIMEOUT
        > StdioHostTransport.DEFAULT_ACK_TIMEOUT
        >= 600
    )
    monkeypatch.setattr("oraculo.managed_service.HOST_ACK_TIMEOUT", 0.4)
    service = running_service
    initialize(service.config.projects_root / "alpha")
    host = Caller(service.config.projects_root)
    entered = asyncio.Event()
    original = host.call

    async def delayed(name, args):
        if name == "orchestrator_capabilities":
            entered.set()
            await asyncio.sleep(0.15)
        return await original(name, args)

    monkeypatch.setattr(host, "call", delayed)
    active = asyncio.create_task(
        command(service.config, "start", payload(service), host)
    )
    await entered.wait()
    with pytest.raises(StateError, match="busy_retry_same_key"):
        await command(service.config, "start", payload(service), host)
    task = await active
    assert task["phase"] == "executing" and len(host.created) == 1
    assert not service.managed_service.busy


@pytest.mark.parametrize("tool", ["delegate_task", "t3_thread_send"])
@pytest.mark.parametrize("fault", ["eof", "malformed", "timeout"])
async def test_lost_ack_retains_effect_and_retries_same_key_without_duplicates(
    running_service, monkeypatch, tool, fault
):
    monkeypatch.setattr("oraculo.managed_service.HOST_ACK_TIMEOUT", 0.2)
    service = running_service
    initialize(service.config.projects_root / "alpha")
    host = Caller(service.config.projects_root)
    reader, writer = await open_operation(service, payload=payload(service))
    frame, result = await pump_until(reader, writer, host, tool)
    if fault == "eof":
        writer.transport.abort()
    elif fault == "malformed":
        writer.write(b"[]\n")
        await writer.drain()
    # For timeout, keep the real socket open without sending an ACK.
    await settled(service)
    writer.close()
    await writer.wait_closed()
    store = service.managed_service.store
    task = store.get(store.db.execute("SELECT id FROM managed_tasks").fetchone()[0])
    assert len(host.created) == 1
    assert service.state.capacity()["occupied"] == 1
    assert task["origin"]["thread_id"] == "origin"
    assert task["launch_attempted"]
    if tool == "delegate_task":
        assert not task.get("child")
        expected = {"eof": "eof", "malformed": "mismatch", "timeout": "timeout"}[fault]
        assert task["blocked_reason"] == "managed_host_ack_" + expected
        assert len(host.delegations) == 1
    else:
        assert store.pending_returns(task["id"])
    replay = await command(service.config, "start", payload(service), host)
    assert replay["id"] == task["id"] and replay["phase"] == "executing"
    assert len(host.created) == 1 and len(host.delegations) == 1
    if tool == "t3_thread_send":
        assert frame["arguments"]["clientRequestId"] in host.sends
        assert host.sends[frame["arguments"]["clientRequestId"]]["receipt"] == result
    assert not store.pending_returns(task["id"])


async def test_client_cancellation_after_host_effect_is_uncertain(running_service):
    service = running_service
    initialize(service.config.projects_root / "alpha")
    host = Caller(service.config.projects_root)
    entered = asyncio.Event()

    class Paused:
        async def call(self, name, args):
            result = await host.call(name, args)
            if name == "delegate_task":
                entered.set()
                await asyncio.Future()
            return result

    active = asyncio.create_task(
        command(service.config, "start", payload(service), Paused())
    )
    await asyncio.wait_for(entered.wait(), 3)
    active.cancel()
    with pytest.raises(asyncio.CancelledError):
        await active
    await settled(service)
    assert len(host.created) == 1 and service.state.capacity()["occupied"] == 1
    replay = await command(service.config, "start", payload(service), host)
    assert replay["phase"] == "executing" and len(host.created) == 1


async def test_client_disconnect_during_memory_retains_real_wait(
    running_service, monkeypatch
):
    service = running_service
    initialize(service.config.projects_root / "alpha")
    host = Caller(service.config.projects_root)
    entered, release = asyncio.Event(), asyncio.Event()
    handle = service.handle

    async def paused(envelope):
        if envelope.kind == "memory.cycle.begin":
            entered.set()
            await release.wait()
        return await handle(envelope)

    monkeypatch.setattr(service, "handle", paused)
    active = asyncio.create_task(
        command(service.config, "start", payload(service), host)
    )
    try:
        await asyncio.wait_for(entered.wait(), 3)
        active.cancel()
        await asyncio.gather(active, return_exceptions=True)
        await asyncio.sleep(0.02)
        diag = (await command(service.config, "list", {}, None))["adapter"]
        assert diag["state"] == "active" and diag["client_eof"] is True
        assert diag["phase"] == "memory"
        with pytest.raises(StateError, match="busy_retry_same_key"):
            await command(service.config, "start", payload(service), host)
        assert not host.created
    finally:
        release.set()
        await settled(service)
    replay = await command(service.config, "start", payload(service), host)
    assert replay["phase"] == "executing" and len(host.created) == 1


async def test_background_host_timeout_stops_batch_traffic(
    running_service, monkeypatch
):
    service = running_service
    store = TaskStore(service.config.state_dir / "managed-tasks.sqlite3")
    task = store.create(
        {"thread_id": "origin", "run_id": "run"}, "key", {"text": "fixture"}
    )
    store.close()
    monkeypatch.setattr("oraculo.managed_service.BACKGROUND_HOST_TIMEOUT", 0.15)
    entered = asyncio.Event()
    calls = []

    class Host:
        async def call(self, name, args):
            calls.append((name, args))
            entered.set()
            await asyncio.Future()

    await service.reconcile_managed_tasks(Host())
    await entered.wait()
    diag = (await request(service.config, make_envelope("status.get", "*", {})))[
        "data"
    ]["managed_adapter"]
    assert diag["mode"] == "background" and diag["phase"] == "host_call"
    await asyncio.wait_for(service.managed_flush, 1)
    assert not service.managed_service.busy
    assert len(calls) == 1
    assert service.managed_service.store.pending_returns(task["id"])
    reader, writer = await open_operation(service)
    assert json.loads(await reader.readline())["tool"] == "orchestrator_capabilities"
    writer.close()
    await writer.wait_closed()
    await settled(service)


async def test_abrupt_client_process_exit_releases_wait(running_service):
    service = running_service
    code = """
import asyncio,json,sys
from oraculo.cli import make_envelope
async def main():
    r,w=await asyncio.open_unix_connection(sys.argv[1])
    e=make_envelope("managed.execute","*",dict(protocol_version=1,action="start",payload={}))
    w.write(json.dumps(e).encode()+b"\\n"); await w.drain()
    print((await r.readline()).decode(),flush=True)
    await asyncio.Future()
asyncio.run(main())
"""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        code,
        str(service.config.socket_path),
        stdout=asyncio.subprocess.PIPE,
    )
    try:
        frame = json.loads(await asyncio.wait_for(process.stdout.readline(), 3))
        assert frame["tool"] == "orchestrator_capabilities"
        process.kill()  # Only the fixture child, never a live/global process.
        await asyncio.wait_for(process.wait(), 3)
        await settled(service)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def test_connection_task_cancel_closes_socket_and_releases(running_service):
    service = running_service
    reader, writer = await open_operation(service)
    await reader.readline()
    owner = service.managed_service.owner
    owner.cancel()
    await asyncio.gather(owner, return_exceptions=True)
    assert await asyncio.wait_for(reader.read(), 1) == b""
    assert not service.managed_service.busy
    writer.close()
    await writer.wait_closed()


async def test_memory_timeout_then_late_service_result_keeps_same_cycle(
    running_service, monkeypatch
):
    service = running_service
    initialize(service.config.projects_root / "alpha")
    host = Caller(service.config.projects_root)
    entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    handle = service.handle

    async def paused(envelope):
        if envelope.kind == "memory.cycle.begin":
            entered.set()
            await release.wait()
            try:
                return await handle(envelope)
            finally:
                finished.set()
        return await handle(envelope)

    monkeypatch.setattr(service, "handle", paused)
    active = asyncio.create_task(
        command(service.config, "start", payload(service), host)
    )
    try:
        await asyncio.wait_for(entered.wait(), 3)
        result = await asyncio.wait_for(active, 7)  # Real request() five-second bound.
        assert result["phase"] == "blocked" and not host.created
        assert service.state.capacity()["occupied"] == 1
        assert not service.managed_service.busy
    finally:
        release.set()
        await asyncio.wait_for(finished.wait(), 2)
    cycles = service.memory_cycles.list("alpha")
    assert len(cycles) == 1
    replay = await command(service.config, "start", payload(service), host)
    assert replay["id"] == result["id"] and replay["phase"] == "executing"
    assert len(service.memory_cycles.list("alpha")) == 1 and len(host.created) == 1


@pytest.mark.parametrize("response", [b"", b"[]\n", b"invalid\n", None])
async def test_client_response_failure_is_uncertain(
    service_config, monkeypatch, response
):
    config, _ = service_config
    monkeypatch.setattr("oraculo.managed_service.HOST_FRAME_TIMEOUT", 0.05)
    finished = asyncio.Event()

    async def peer(reader, writer):
        await reader.readline()
        if response is None:
            await reader.read()  # Client timeout closes its socket.
        elif response:
            writer.write(response)
            await writer.drain()
        writer.close()
        await writer.wait_closed()
        finished.set()

    server = await asyncio.start_unix_server(peer, path=str(config.socket_path))
    async with server:
        with pytest.raises(
            StateError, match="^managed_response_uncertain_resume_same_intent$"
        ):
            await command(config, "start", {}, None)
        await asyncio.wait_for(finished.wait(), 1)


async def test_unknown_owner_diagnostic_never_clears_busy(running_service):
    service = running_service
    await command(service.config, "list", {}, None)
    facade = service.managed_service
    facade.busy = True  # Models an untracked owner, never a recovery API.
    try:
        status = await request(service.config, make_envelope("status.get", "*", {}))
        diag = status["data"]["managed_adapter"]
        assert diag["state"] == "owner_unknown"
        assert diag["recovery"] == "inspect_do_not_clear"
        assert facade.busy
    finally:
        facade.busy = False


async def test_uncertain_independent_launch_never_relaunches(running_service):
    service = running_service
    service.config.max_executors = service.state.max_executors = 2
    service.state.provider_limits["codex"] = 2
    service.state.set_capacity(2)
    initialize(service.config.projects_root / "alpha")
    host = Caller(service.config.projects_root)
    intent = dict(payload(service), independent=True)
    reader, writer = await open_operation(service, payload=intent)
    await pump_until(reader, writer, host, "t3_thread_launch")
    writer.transport.abort()
    await settled(service)
    host.list_visible = False
    blocked = await command(service.config, "start", intent, host)
    assert blocked["phase"] == "blocked" and blocked["launch_attempted"]
    assert len(host.created) == 1 and service.state.capacity()["occupied"] == 1
    host.list_visible = True
    recovered = await command(service.config, "start", intent, host)
    assert recovered["id"] == blocked["id"] and recovered["phase"] == "executing"
    assert len([name for name, args in host.calls if name == "t3_thread_launch"]) == 1
    fresh = await command(
        service.config, "start", dict(payload(service), key="new-intent"), host
    )
    assert fresh["id"] != recovered["id"] and fresh["phase"] == "executing"
    assert len(host.created) == 2
