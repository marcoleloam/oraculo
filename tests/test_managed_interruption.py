"""A host channel that fails before any effect never blocks the task.

Engine tests use a fake host; the socket tests use the real temporary service
and compose with the per-task scope and disconnect behaviour.
"""

import asyncio
import json

import pytest
from test_managed_disconnect import open_operation, payload, pump_until, settled
from test_managed_service import Caller
from test_managed_tasks import Host, initialize, report, start, system  # noqa: F401

from oraculo.cli import run_managed_attempts, task_error_receipt
from oraculo.managed_service import command
from oraculo.state import StateError


class Flaky(Host):
    """Fails one named tool with a channel error, before or after acting."""

    def __init__(self, root):
        super().__init__(root)
        self.fail = None
        self.code = "host_response_uncertain"

    async def call(self, name, args):
        if self.fail == name:
            self.fail = None
            raise StateError(self.code)
        return await super().call(name, args)


@pytest.fixture
def flaky(system):  # noqa: F811
    host = Flaky(system.service.config.projects_root)
    system.host = host
    system.engine.host = host
    return system


def returns(store, task_id):
    return store.db.execute(
        "SELECT count(*) FROM task_returns WHERE task_id=?", (task_id,)
    ).fetchone()[0]


@pytest.mark.parametrize(
    "code",
    [
        "host_response_uncertain",
        "managed_host_ack_eof",
        "managed_host_ack_timeout",
        "managed_host_channel_failed",
    ],
)
@pytest.mark.parametrize("tool", ["t3_thread_read", "t3_thread_list", "task_status"])
async def test_read_failure_keeps_phase_and_names_the_same_command(flaky, code, tool):
    task = await start(flaky)
    flaky.host.complete(task["child"]["childThreadId"])
    before = flaky.store.get(task["id"])
    rows = returns(flaky.store, task["id"])
    request = {"task_id": task["id"], "report": report(task)}
    flaky.host.sends.clear()
    # The identity read comes first; fail the first read made for the task.
    original = flaky.host.call
    seen = []

    async def fail_task_read(name, args):
        seen.append(name)
        if name == tool and (tool != "t3_thread_read" or seen.count(name) == 2):
            raise StateError(code)
        return await original(name, args)

    flaky.host.call = fail_task_read
    with pytest.raises(StateError, match="^" + code + "$") as caught:
        await flaky.engine.command("finish", request)
    del flaky.host.call
    exc = caught.value
    assert exc.retryable is True and exc.auto_retry is False
    assert exc.recovery["command"] == "oraculo task finish"
    assert exc.recovery["payload"] == {"task_id": task["id"]}
    assert exc.recovery["task_state"] == "unchanged" and exc.recovery["cause"] == code
    assert "same payload (task_id, report)" in exc.recovery["instruction"]
    after = flaky.store.get(task["id"])
    assert after["phase"] == before["phase"] != "blocked"
    assert not after.get("blocked_reason")
    assert after["events"] == before["events"]
    assert after["snapshot_revision"] == before["snapshot_revision"]
    # Diagnostic only: no event, no outbox row, nothing sent to the origin.
    assert returns(flaky.store, task["id"]) == rows and not flaky.host.sends
    (note,) = after["diagnostics"]
    assert note["kind"] == "host_channel_interrupted_before_effect"
    assert note["reason"] == code and note["tool"] == tool
    assert note["action"] == "finish" and note["phase"] == before["phase"]
    assert flaky.store.describe(task["id"])["diagnostics"] == [note]
    receipt = task_error_receipt(exc)
    assert receipt["error"]["retryable"] and receipt["error"]["recovery"]
    # The same command, unchanged, completes.
    done = await flaky.engine.command("finish", request)
    assert done["phase"] == "pr_pending" and done["work_status"] == "succeeded"


async def test_interruption_is_not_repeated_automatically(flaky):
    attempts = []

    async def invoke():
        attempts.append(1)
        flaky.host.fail = "orchestrator_capabilities"
        return await flaky.engine.command("status", {"task_id": "missing"})

    with pytest.raises(StateError, match="host_response_uncertain") as caught:
        await run_managed_attempts(invoke)
    assert len(attempts) == 1
    # Before any task is known the error still names the command to repeat.
    assert caught.value.recovery["command"] == "oraculo task status"


async def test_identity_failure_on_start_creates_nothing(flaky):
    flaky.host.fail = "t3_project_list"
    request = {
        "kind": "work",
        "key": "interrupted-start",
        "cwd": str(flaky.service.config.projects_root / "alpha"),
        "text": "Implement.",
    }
    with pytest.raises(StateError) as caught:
        await flaky.engine.command("start", request)
    assert caught.value.recovery["payload"] == {"key": "interrupted-start"}
    assert not flaky.host.created
    task = await flaky.engine.command("start", request)
    assert task["phase"] == "executing" and len(flaky.host.created) == 1


@pytest.mark.parametrize("code", ["host_response_uncertain", "managed_host_ack_eof"])
async def test_uncertain_effect_still_blocks_with_recovery(flaky, code):
    flaky.host.code = code
    flaky.host.fail = "delegate_task"
    blocked = await start(flaky)
    assert blocked["phase"] == "blocked" and blocked["blocked_reason"] == code
    assert blocked["launch_attempted"] and not blocked.get("diagnostics")
    assert blocked["recovery"]["command"] == "oraculo task resume"
    assert blocked["recovery"]["payload"] == {"task_id": blocked["id"]}
    assert "Do not start a new task" in blocked["recovery"]["instruction"]
    # Status reads carry the same field, and leaving the block clears it.
    assert flaky.store.describe(blocked["id"])["recovery"] == blocked["recovery"]
    resumed = await flaky.engine.command("resume", {"task_id": blocked["id"]})
    assert resumed["phase"] == "executing" and "recovery" not in resumed
    assert len(flaky.host.created) == 1


async def test_read_failure_after_an_effect_is_uncertain_and_blocks(flaky):
    task = await start(flaky)
    flaky.host.complete(task["child"]["childThreadId"])
    original = flaky.host.call
    seen = []

    async def fail_after_memory(name, args):
        seen.append(name)
        if name == "task_status" and seen.count(name) == 2:
            raise StateError("managed_host_ack_timeout")
        return await original(name, args)

    flaky.host.call = fail_after_memory
    result = await flaky.engine.command(
        "finish", {"task_id": task["id"], "report": report(task)}
    )
    del flaky.host.call
    # Memory was written before the channel failed: not a clean interruption.
    assert result["phase"] == "blocked"
    assert result["blocked_reason"] == "managed_host_ack_timeout"
    assert result["work_status"] == "succeeded" and result["recovery"]
    # The channel is known dead: nothing was prepared for sending.
    assert not flaky.store.db.execute(
        "SELECT count(*) FROM task_notifications WHERE task_id=? AND decided_at>?",
        (task["id"], result["observed_at"]),
    ).fetchone()[0]


async def test_rejections_carry_recovery(flaky):
    task = await start(flaky)
    flaky.host.complete(task["child"]["childThreadId"])
    bad = dict(report(task), checks=[])
    rejected = await flaky.engine.command(
        "finish", {"task_id": task["id"], "report": bad}
    )
    assert rejected["phase"] == "blocked" and not rejected["rejection"]["persisted"]
    assert rejected["recovery"]["command"] == "oraculo task finish"
    assert rejected["recovery"]["payload"] == {"task_id": task["id"]}
    launch = await flaky.engine.command(
        "start",
        {
            "kind": "work",
            "key": "bad-flag",
            "cwd": str(flaky.service.config.projects_root / "alpha"),
            "text": "Implement.",
            "independent": "yes",
        },
    )
    assert launch["phase"] == "blocked" and launch["created"] is False
    assert launch["recovery"]["command"] == "oraculo task start"


async def test_socket_read_interruption_reports_retryable_recovery(running_service):
    service = running_service
    initialize(service.config.projects_root / "alpha")
    host = Caller(service.config.projects_root)
    task = await command(service.config, "start", payload(service), host)
    host.complete(task["child"]["childThreadId"])
    store = service.managed_service.store
    before = store.get(task["id"])
    request = {"task_id": task["id"], "report": report(task)}
    reader, writer = await open_operation(service, "finish", request)
    frame, _ = await pump_until(reader, writer, host, "t3_thread_list")
    # The bridge could not relay this read.
    writer.write(
        json.dumps(
            dict(
                operation_id=frame["operation_id"],
                request_id=frame["request_id"],
                error="host_response_uncertain",
            )
        ).encode()
        + b"\n"
    )
    await writer.drain()
    response = json.loads(await asyncio.wait_for(reader.readline(), 5))
    writer.close()
    await writer.wait_closed()
    await settled(service)
    error = response["error"]
    assert response["ok"] is False and error["message"] == "host_response_uncertain"
    assert error["retryable"] is True and error["interrupted"] is True
    assert error["recovery"]["command"] == "oraculo task finish"
    after = store.get(task["id"])
    assert after["phase"] == before["phase"] and after["events"] == before["events"]
    assert after["diagnostics"][0]["tool"] == "t3_thread_list"
    # The scope was released: the same command runs at once and completes.
    done = await command(service.config, "finish", request, host)
    assert done["phase"] == "pr_pending" and done["work_status"] == "succeeded"


async def test_socket_client_error_exposes_recovery_without_auto_retry(
    running_service,
):
    service = running_service
    initialize(service.config.projects_root / "alpha")
    host = Caller(service.config.projects_root)
    task = await command(service.config, "start", payload(service), host)
    host.complete(task["child"]["childThreadId"])

    class Broken:
        calls = 0

        async def call(self, name, args):
            if name == "task_status":
                Broken.calls += 1
                raise TimeoutError("bridge gave up")
            return await host.call(name, args)

    request = {"task_id": task["id"], "report": report(task)}
    with pytest.raises(StateError, match="host_response_uncertain") as caught:
        await run_managed_attempts(
            lambda: command(service.config, "finish", request, Broken())
        )
    await settled(service)
    assert Broken.calls == 1 and caught.value.auto_retry is False
    assert caught.value.recovery["payload"] == {"task_id": task["id"]}
    store = service.managed_service.store
    assert store.get(task["id"])["phase"] != "blocked"


async def test_socket_eof_on_a_read_leaves_the_task_unblocked(running_service):
    service = running_service
    initialize(service.config.projects_root / "alpha")
    host = Caller(service.config.projects_root)
    task = await command(service.config, "start", payload(service), host)
    host.complete(task["child"]["childThreadId"])
    store = service.managed_service.store
    phase = store.get(task["id"])["phase"]
    request = {"task_id": task["id"], "report": report(task)}
    reader, writer = await open_operation(service, "finish", request)
    await pump_until(reader, writer, host, "task_status")
    writer.transport.abort()
    await settled(service)
    after = store.get(task["id"])
    assert after["phase"] == phase and not after.get("blocked_reason")
    assert after["diagnostics"][0]["reason"] == "managed_host_ack_eof"
    done = await command(service.config, "finish", request, host)
    assert done["phase"] == "pr_pending"
