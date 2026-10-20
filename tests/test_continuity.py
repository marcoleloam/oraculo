"""AT-020 acceptance regressions; remote T3 is simulated, never UI closure."""

import json

import pytest
from test_reconcile import Remote
from test_reconcile import snapshot as unit_snapshot
from test_service import DispatchLifecycleRemote

from oraculo.memory.schema import digest
from oraculo.protocol import canonical_json
from oraculo.reconcile import Reconciler
from oraculo.service import Service


def snapshot(*args, **kwargs):
    result = unit_snapshot(*args, **kwargs)
    result["thread"]["projectId"] = "alpha"
    return result


def completed_work(service, result="Checks passed"):
    state = service.state
    objective = state.submit_objective("alpha", "Disposable continuity task")
    job = state.enqueue_job(objective["id"], "implementation", {})
    attempt = state.acquire_grant(
        job["id"], "codex", "current", "coordinator", "owner-run"
    )
    state.claim_grant(attempt["id"], attempt["generation"], "child", "run", "task")
    report = {
        "attempt_id": attempt["id"],
        "generation": attempt["generation"],
        "run_id": "run",
        "status": "succeeded",
        "evidence": {
            "summary": "Implementation verified",
            "checks": [
                {"argv": ["python", "-m", "unittest"], "exit_code": 0, "result": result}
            ],
        },
    }
    state.set_meta(f"execution_report:{attempt['id']}", report)
    return attempt, report


async def test_next_stage_waits_for_tree_then_dispatches_once_after_restart(
    running_service,
):
    service = running_service
    attempt, _ = completed_work(service)
    identity = service.state.get_attempt(attempt["id"])
    remote = Remote()
    remote.snapshots["child"] = snapshot("child", "run", "completed")
    remote.snapshots["grandchild"] = snapshot("grandchild", "grand-run", parent="child")
    restarted = Service(service.config, service.config_path)
    restarted.catalog = service.catalog
    try:
        assert restarted.state.get_attempt(attempt["id"]) == identity
        assert (await Reconciler(restarted.state, remote).reconcile())[
            "state"
        ] == "running"
        restarted.after_execution()
        assert not [j for j in restarted.state.list_jobs() if j["kind"] == "memory"]
        remote.snapshots["grandchild"] = snapshot(
            "grandchild", "grand-run", "completed", parent="child"
        )
        assert (await Reconciler(restarted.state, remote).reconcile())["released"]
        for _ in range(3):
            restarted.after_execution()
        memory = [j for j in restarted.state.list_jobs() if j["kind"] == "memory"]
        assert len(memory) == 1
        payload = json.loads(memory[0]["payload"])
        assert payload["provenance"]["run_id"] == "run"
        assert (
            restarted.state.connection.execute(
                "SELECT count(*) FROM attempts"
            ).fetchone()[0]
            == 1
        )

        class PendingCoordinator(DispatchLifecycleRemote):
            async def send(self, *args, **kwargs):
                receipt = await super().send(*args, **kwargs)
                self.snapshot["thread"]["activeRunId"] = receipt["runId"]
                self.snapshot["thread"]["status"] = "running"
                return receipt

        transport = PendingCoordinator()
        await restarted.dispatch(transport)
        await restarted.dispatch(transport)
        assert len(transport.calls) == 1
        assert json.loads(transport.calls[0][1])["job_id"] == memory[0]["id"]
        assert restarted.state.lease() is None
    finally:
        restarted.state.close()


@pytest.mark.parametrize("restart", [False, True])
async def test_retry_when_dispatch_is_historical_and_coordinator_idle(
    running_service, restart
):
    service = running_service
    objective = service.state.submit_objective("alpha", "Queued work")
    service.state.enqueue_job(objective["id"], "implementation", {})

    class HistoricalCoordinator(DispatchLifecycleRemote):
        async def read_thread(self, thread):
            current = snapshot(thread, "newer-idle", "completed")
            current["recentRuns"] += [
                {"runId": f"dispatch-run-{i}", "status": "completed"}
                for i in range(1, len(self.calls) + 1)
            ]
            return current

    remote = HistoricalCoordinator()
    await service.dispatch(remote)
    active = Service(service.config, service.config_path) if restart else service
    active.catalog = service.catalog
    try:
        for _ in range(service.config.max_dispatch_attempts + 2):
            await active.dispatch(remote)
        assert len(remote.calls) == service.config.max_dispatch_attempts, (
            f"Historical terminal dispatch stuck: {len(remote.calls)} sends; health={active.health}"
        )
        assert len({call[1] for call in remote.calls}) == 1
        base = remote.calls[0][2]
        assert [c[2] for c in remote.calls] == [
            base,
            base + ":retry:2",
            base + ":retry:3",
        ]
        assert active.health == "dispatch_attempts_exhausted"
    finally:
        if restart:
            active.state.close()


@pytest.mark.parametrize(
    "lines,unicode_output", [(1, False), (500, False), (500, True)]
)
async def test_automatic_memory_fits_transport_and_preserves_full_source(
    running_service, lines, unicode_output
):
    service = running_service
    output = "\n".join(
        f"case_{i:04d}: positive negative mixed-sign zero identity commutativity "
        "integer-type result verified against expected fixture output; PASS"
        + (" 🧪" * 12 if unicode_output else "")
        for i in range(lines)
    )
    attempt, report = completed_work(service, output)
    assert service.state.record_execution(
        attempt["id"],
        attempt["generation"],
        "run",
        "succeeded",
        pending_children=False,
        tree_confirmed=True,
        evidence=report,
    )
    service.after_execution()
    memory = [j for j in service.state.list_jobs() if j["kind"] == "memory"]
    assert len(memory) == 1
    source = canonical_json(json.loads(memory[0]["payload"])["execution_evidence"])

    class LimitedTransport:
        def __init__(self):
            self.sent = []

        async def send(self, thread, message, key, **kwargs):
            units = len(message.encode("utf-16-le")) // 2
            assert units <= 120000, f"Memory dispatch: {units} UTF-16 units > 120000"
            self.sent.append((message, key))
            return {"runId": "memory-dispatch"}

    remote = LimitedTransport()
    # Check the full snapshot even when transport rejects the dispatch.
    try:
        await service.dispatch(remote)
    finally:
        raw = service.config.vault_root / f"projects/alpha/raw/{digest(source)}.md"
        assert raw.read_bytes() == source.encode()
    assert len(remote.sent) == 1, "Automatic memory must actually be dispatched"
