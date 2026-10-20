"""Real local publisher/socket, simulated remote T3 for managed entry paths."""

import json

import pytest
from test_plan_service import Dispatch, grant, submit, task
from test_service import command

from oraculo.memory.transport import MemoryArtifacts
from oraculo.service import Service


async def direct(service, action, payload, *, thread="direct", run="run"):
    return await command(
        service,
        "memory.cycle." + action,
        payload,
        origin={"thread_id": thread, "run_id": run},
    )


@pytest.mark.parametrize("planned", [False, True])
@pytest.mark.parametrize("status", ["succeeded", "failed", "stopped"])
async def test_managed_lifecycle_releases_slot_and_publishes_once(
    running_service, planned, status
):
    service = running_service
    if planned:
        await submit(service, [task("implementation")])
    else:
        await command(
            service, "objective.submit", {"text": "Implement bounded learning"}
        )
    job = service.state.list_jobs()[0]
    remote = Dispatch()
    await service.dispatch(remote)
    dispatch = remote.sent[0][1]
    assert dispatch["memory"]["read_state"] == "empty"
    artifact = dispatch["context"]["artifact"]
    assert (
        json.loads(
            MemoryArtifacts(service.config.state_dir).load(artifact, "alpha", job["id"])
        )["context"]["bundle"]["project_id"]
        == "alpha"
    )
    attempt = await grant(service, job["id"], "one")
    read = await command(
        service,
        "memory.read",
        {
            "attempt_id": attempt["id"],
            "generation": attempt["generation"],
            "artifact": artifact,
        },
        origin={"thread_id": attempt["thread_id"], "run_id": attempt["run_id"]},
    )
    assert read["ok"], read
    report = {
        "evidence": {
            "summary": "Unique implementation learning zebra",
            "limitations": ["remote mocked"],
        }
    }
    service.state.set_meta(f"execution_report:{attempt['id']}", report)
    assert service.state.record_execution(
        attempt["id"],
        attempt["generation"],
        attempt["run_id"],
        status,
        pending_children=False,
        tree_confirmed=True,
        evidence=report,
    )
    service.after_execution()
    cycle = service.memory_cycles.get(dispatch["memory"]["id"])
    assert cycle["work_status"] == status
    assert cycle["write_state"] == "pending"
    assert service.state.lease() is None
    assert not [j for j in service.state.list_jobs() if j["kind"] == "memory"]
    service.memory_cycles.publish_pending()
    saved = service.memory_cycles.get(cycle["id"])
    assert saved["write_state"] == "saved"
    head = service.publisher.git.head()
    restarted = Service(service.config, service.config_path)
    try:
        restarted.after_execution()
        restarted.memory_cycles.publish_pending()
        assert restarted.publisher.git.head() == head
        assert (
            restarted.memory_cycles.get(cycle["id"])["identity"]["run_id"]
            == attempt["run_id"]
        )
    finally:
        restarted.state.close()


async def test_direct_cycle_roundtrip_and_origin_guard(running_service):
    s = running_service
    started = await direct(s, "begin", {"key": "first", "query": "zebra"})
    assert started["ok"], started
    cycle = started["data"]
    assert cycle["read_state"] == "empty"
    assert (await direct(s, "begin", {"key": "first", "query": "zebra"}))["data"][
        "id"
    ] == cycle["id"]
    assert not (await direct(s, "status", {"cycle_id": cycle["id"]}, run="other"))["ok"]
    read = await direct(s, "read", {"cycle_id": cycle["id"]})
    assert read["ok"] and read["data"]["eof"]
    finished = await direct(
        s,
        "finish",
        {
            "cycle_id": cycle["id"],
            "status": "succeeded",
            "evidence": {
                "summary": "zebra uses immutable snapshot",
                "limitations": ["cooperative adapter"],
            },
        },
    )
    assert finished["data"]["write_state"] == "saved", finished
    second = await direct(
        s, "begin", {"key": "second", "query": "zebra"}, run="resumed"
    )
    assert second["data"]["read_state"] == "found"
    result = await direct(s, "read", {"cycle_id": second["data"]["id"]}, run="resumed")
    assert "zebra uses immutable snapshot" in result["data"]["content"]
    assert s.state.list_jobs() == []
    assert s.state.leases() == []


async def test_read_unavailable_visible_and_does_not_consume_additional_job(
    running_service, monkeypatch
):
    s = running_service
    monkeypatch.setattr(
        s.publisher,
        "_check_integrity",
        lambda: (_ for _ in ()).throw(OSError("unavailable")),
    )
    await command(s, "objective.submit", {"text": "work despite explicit gap"})
    remote = Dispatch()
    await s.dispatch(remote)
    assert remote.sent[0][1]["memory"]["read_state"] == "unavailable"
    assert len(s.state.list_jobs()) == 1


async def test_retry_attempt_gets_fresh_cycle(running_service):
    s = running_service
    await command(s, "objective.submit", {"text": "retry implementation"})
    job = s.state.list_jobs()[0]
    first = await grant(s, job["id"], "first")
    s.state.record_execution(
        first["id"],
        first["generation"],
        first["run_id"],
        "failed",
        pending_children=False,
        tree_confirmed=True,
        evidence={"error": "test"},
    )
    s.after_execution()
    second = await grant(s, job["id"], "second")
    a = s.state.get_meta(f"attempt_cycle:{first['id']}")
    b = s.state.get_meta(f"attempt_cycle:{second['id']}")
    assert a != b
    assert s.memory_cycles.get(a)["work_status"] == "failed"
    assert s.memory_cycles.get(b)["identity"]["attempt_number"] == 2


async def test_cancel_before_claim_preserves_late_child_identity(running_service):
    s = running_service
    await command(s, "objective.submit", {"text": "cancel before claim"})
    job = s.state.list_jobs()[0]
    acquired = await command(
        s,
        "grant.acquire",
        {"job_id": job["id"]},
        origin={"thread_id": "coordinator", "run_id": "owner"},
    )
    attempt = acquired["data"]["attempt"]
    await command(s, "objective.cancel", {"objective_id": job["objective_id"]})
    claim = await command(
        s,
        "grant.claim",
        {
            "attempt_id": attempt["id"],
            "generation": attempt["generation"],
            "thread_id": "late-child",
            "run_id": "late-run",
            "task_id": "late-task",
        },
        origin={"thread_id": "coordinator", "run_id": "owner"},
    )
    assert claim["ok"] and claim["data"]["effect_authorized"] is False
    s.state.record_execution(
        attempt["id"],
        attempt["generation"],
        "late-run",
        "stopped",
        pending_children=False,
        tree_confirmed=True,
        evidence={"cancelled": True},
    )
    s.after_execution()
    cycle = s.memory_cycles.get(s.state.get_meta(f"attempt_cycle:{attempt['id']}"))
    assert cycle["work_status"] == "stopped"
    assert cycle["identity"]["thread_id"] == "late-child"
    assert cycle["identity"]["run_id"] == "late-run"


async def test_cancel_dispatch_before_grant_does_not_leave_running_cycle(
    running_service,
):
    s = running_service
    await command(s, "objective.submit", {"text": "cancel dispatched"})
    job = s.state.list_jobs()[0]
    await s.dispatch(Dispatch())
    await command(s, "objective.cancel", {"objective_id": job["objective_id"]})
    s.after_execution()
    cycle = s.memory_cycles.list()[0]
    assert cycle["work_status"] == "stopped"
    assert "attempt_id" not in cycle["identity"]
    await s.memory_io("publish_pending")
    assert s.memory_cycles.get(cycle["id"])["write_state"] == "saved"


async def test_large_terminal_report_blocks_only_its_memory(running_service):
    s = running_service
    await command(s, "objective.submit", {"text": "large output"})
    job = s.state.list_jobs()[0]
    attempt = await grant(s, job["id"], "large")
    evidence = {"summary": "x" * 300_000}
    response = await command(
        s,
        "execution.report",
        {
            "attempt_id": attempt["id"],
            "generation": attempt["generation"],
            "run_id": attempt["run_id"],
            "status": "succeeded",
            "evidence": evidence,
        },
        origin={"thread_id": attempt["thread_id"], "run_id": attempt["run_id"]},
    )
    assert response["ok"]
    s.state.record_execution(
        attempt["id"],
        attempt["generation"],
        attempt["run_id"],
        "succeeded",
        pending_children=False,
        tree_confirmed=True,
        evidence=evidence,
    )
    s.after_execution()
    s.after_execution()
    cycle = s.memory_cycles.list()[0]
    assert cycle["work_status"] == "succeeded"
    assert cycle["write_state"] == "blocked"
    assert s.state.get_meta(f"postprocessed:{attempt['id']}")
    assert s.state.get_meta(f"execution_report:{attempt['id']}")["evidence"] == evidence
    assert s.state.lease() is None
    await command(s, "objective.submit", {"text": "another task"})
    remote = Dispatch()
    await s.dispatch(remote)
    assert len(remote.sent) == 1


async def test_writer_lock_does_not_block_status_or_event_loop(running_service):
    import asyncio
    import threading

    s = running_service
    locked = threading.Event()
    release = threading.Event()

    def hold():
        with s.publisher.locked():
            locked.set()
            release.wait(timeout=3)

    worker = threading.Thread(target=hold)
    worker.start()
    await asyncio.to_thread(locked.wait, 2)
    task = asyncio.create_task(direct(s, "begin", {"key": "locked", "query": "memory"}))
    try:
        await asyncio.sleep(0.05)
        assert not task.done()
        status = await asyncio.wait_for(command(s, "status.get", {}), timeout=0.5)
        assert status["ok"]
    finally:
        release.set()
        await asyncio.to_thread(worker.join)
    assert (await task)["ok"]


async def test_parallel_direct_cycles_keep_executors_and_receipts_separate(
    running_service,
):
    import asyncio

    s = running_service
    results = await asyncio.gather(
        *(
            direct(
                s,
                "begin",
                {"key": "same-key", "query": "parallel"},
                thread=f"thread-{i}",
            )
            for i in range(3)
        )
    )
    assert all(r["ok"] for r in results), results
    ids = [r["data"]["id"] for r in results]
    assert len(set(ids)) == 3
    finished = await asyncio.gather(
        *(
            direct(
                s,
                "finish",
                {
                    "cycle_id": cycle_id,
                    "status": "failed",
                    "evidence": {"summary": f"historical failure {i}"},
                },
                thread=f"thread-{i}",
            )
            for i, cycle_id in enumerate(ids)
        )
    )
    assert all(r["data"]["write_state"] == "saved" for r in finished), finished
    assert s.state.leases() == []
    assert s.state.list_jobs() == []


async def test_cancel_before_dispatch_records_no_fabricated_query(running_service):
    s = running_service
    await command(s, "objective.submit", {"text": "never started"})
    objective = s.state.list_objectives()[0]
    response = await command(s, "objective.cancel", {"objective_id": objective["id"]})
    assert response["ok"]
    cycle = s.memory_cycles.list()[0]
    assert cycle["work_status"] == "stopped"
    assert cycle["read_state"] == "unavailable"
    assert cycle["read_receipt"]["error"] == "cancelled_before_start"
    assert cycle["read_receipt"]["sha256"] is None
    assert s.state.leases() == []


async def test_operator_repair_preserves_rejection_and_replays(running_service):
    s = running_service
    started = await direct(s, "begin", {"key": "blocked", "query": "repair"})
    cycle_id = started["data"]["id"]
    result = await direct(
        s,
        "finish",
        {
            "cycle_id": cycle_id,
            "status": "failed",
            "evidence": {"summary": "x" * 300_000},
        },
    )
    # The direct adapter exposes a rejected report as a memory state, not success.
    assert result["ok"] and result["data"]["write_state"] == "blocked", result
    repair = {
        "cycle_id": cycle_id,
        "evidence": {"summary": "bounded corrected failure"},
    }
    repaired = await command(s, "memory.cycle.repair", repair)
    assert repaired["ok"] and repaired["data"]["write_state"] == "saved", repaired
    assert repaired["data"]["initial_finish_rejection"]["bytes"] > 256_000
    again = await command(s, "memory.cycle.repair", repair)
    assert again["data"]["write_receipt"] == repaired["data"]["write_receipt"]
    bad = await command(
        s,
        "memory.cycle.repair",
        {"cycle_id": cycle_id, "evidence": {"summary": "replacement"}},
    )
    assert not bad["ok"]


async def test_background_publication_does_not_occupy_executor_or_block_loop(
    running_service, monkeypatch
):
    import asyncio

    s = running_service
    gate = asyncio.Event()
    calls = []

    async def slow(method, **kwargs):
        calls.append(method)
        await gate.wait()

    cycle = s.memory_cycles.begin("alpha", "background", "flush", {"entry": "test"})
    s.memory_cycles.finish(cycle["id"], "succeeded", {"summary": "background"})
    monkeypatch.setattr(s, "memory_io", slow)
    s.start_memory_flush()
    s.start_memory_flush()
    await asyncio.sleep(0)
    assert calls == ["publish_pending"]
    assert s.state.leases() == []
    assert (await command(s, "status.get", {}))["ok"]
    gate.set()
    await s.memory_flush


async def test_busy_grant_does_not_enroll_phantom_second_attempt(running_service):
    s = running_service
    await command(s, "objective.submit", {"text": "one attempt"})
    job = s.state.list_jobs()[0]
    await grant(s, job["id"], "one")
    repeated = await command(
        s,
        "grant.acquire",
        {"job_id": job["id"]},
        origin={"thread_id": "coordinator", "run_id": "owner"},
    )
    assert repeated["data"]["state"] == "busy"
    assert len(s.memory_cycles.list()) == 1


async def test_delayed_preflight_cannot_admit_a_new_attempt_without_its_cycle(
    running_service, monkeypatch
):
    import asyncio

    s = running_service
    await command(s, "objective.submit", {"text": "race in grant"})
    job = s.state.list_jobs()[0]
    prepared, resume = asyncio.Event(), asyncio.Event()
    real_io = s.memory_io

    calls = 0

    async def delayed(method, *args, **kwargs):
        nonlocal calls
        calls += 1
        order = calls
        result = await real_io(method, *args, **kwargs)
        if order == 1:
            prepared.set()
            await resume.wait()
        return result

    monkeypatch.setattr(s, "memory_io", delayed)
    late = asyncio.create_task(
        command(
            s,
            "grant.acquire",
            {"job_id": job["id"]},
            origin={"thread_id": "coordinator", "run_id": "late"},
        ),
        name="delayed-grant",
    )
    await asyncio.wait_for(prepared.wait(), timeout=3)
    first = await grant(s, job["id"], "first")
    s.state.record_execution(
        first["id"],
        first["generation"],
        first["run_id"],
        "failed",
        pending_children=False,
        tree_confirmed=True,
        evidence={"failure": True},
    )
    s.after_execution()
    resume.set()
    response = await late
    assert response["data"]["state"] == "busy"
    assert response["data"]["reason"] == "memory_preparation_changed_retry"
    second = await grant(s, job["id"], "second")
    assert second["number"] == 2
    assert s.state.get_meta(f"attempt_cycle:{second['id']}")
    assert (
        s.state.get_meta(f"memory_artifact:{job['id']}")
        == s.state.get_meta(f"work_memory_reference:{job['id']}:2")["artifact"]
    )


async def test_stale_direct_begin_cannot_replace_recovered_artifact(
    running_service, monkeypatch
):
    from oraculo.memory.bridge import MemoryBridge

    s = running_service
    with monkeypatch.context() as patch:
        patch.setattr(
            s.publisher,
            "_check_integrity",
            lambda: (_ for _ in ()).throw(OSError("temporary")),
        )
        began = await direct(s, "begin", {"key": "race", "query": "query"})
    assert began["data"]["read_state"] == "unavailable"
    cycle_id = began["data"]["id"]
    stale = s.memory_cycles.get(cycle_id)
    recovered = await direct(s, "retry", {"cycle_id": cycle_id, "phase": "read"})
    assert recovered["data"]["read_state"] == "empty"
    reissued = MemoryBridge(s).artifact(stale)
    assert reissued["artifact"] == recovered["data"]["artifact"]["artifact"]
    old_read = await direct(
        s,
        "read",
        {"cycle_id": cycle_id, "artifact": began["data"]["artifact"]["artifact"]},
    )
    assert not old_read["ok"]
    read = await direct(
        s, "read", {"cycle_id": cycle_id, "artifact": reissued["artifact"]}
    )
    assert read["ok"]
    delivered = s.memory_cycles.get(cycle_id)["delivery_receipt"]
    snapshot = json.loads(read["data"]["content"])
    assert snapshot["context"]["read_state"] == "empty"
    assert delivered["context_sha256"] == snapshot["context"]["read_receipt"]["sha256"]


async def test_grant_receipt_replay_does_not_prepare_next_attempt(running_service):
    s = running_service
    await command(s, "objective.submit", {"text": "replay receipt"})
    job = s.state.list_jobs()[0]
    payload = {"job_id": job["id"]}
    origin = {"thread_id": "coordinator", "run_id": "owner"}
    first = await command(
        s, "grant.acquire", payload, origin=origin, request_id="original"
    )
    a = first["data"]["attempt"]
    s.state.fail_unstarted(
        a["id"],
        a["generation"],
        {
            "definitive_no_child": True,
            "error_kind": "validation",
            "client_request_id": "launch-rejected",
            "tool_error": "invalid target",
        },
    )
    s.after_execution()
    before = s.memory_cycles.list()
    assert (
        await command(s, "grant.acquire", payload, origin=origin, request_id="original")
    ) == first
    assert s.memory_cycles.list() == before
