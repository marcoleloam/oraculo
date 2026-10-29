"""Real SQLite and runtime guards; host/launchd observations are explicit fixtures."""

import copy
import json
import time

import pytest
from test_reservation_carry import (
    AUTHORIZATION,
    mutate,
    open_world,
    snapshot,
)
from test_reservation_carry import (
    runtime_world as runtime_fixture,
)
from test_reservation_carry import (
    world as world_fixture,
)

from oraculo import maintenance, reservation_carry, runtime, service_control
from oraculo.protocol import content_hash
from oraculo.state import StateError
from oraculo.terminal_carry import ReadState, pre_stop, validate

world = world_fixture
runtime_world = runtime_fixture


def witness(w):
    loaded, state, store = open_world(w["conf"])
    records = []
    try:
        for ident in w["carry"]:
            task = store.get(ident)
            task["origin"]["project_id"] = "alpha"
            store.update(
                ident,
                "executing",
                {"fixture": "reportless-terminal-task"},
                origin=task["origin"],
                project={"project_id": "alpha"},
                submitted_report=None,
            )
            task = store.get(ident)
            child = task["child"]
            row = state._one(
                "SELECT id,provider,slots,state,receipt FROM managed_reservations WHERE id=?",
                (ident,),
            )
            records.append(
                dict(
                    task_id=ident,
                    task_sha256=content_hash(task),
                    reservation_sha256=content_hash(row),
                    snapshot=dict(
                        thread=dict(
                            threadId=child["childThreadId"],
                            projectId="alpha",
                            parentThreadId=task["origin"]["thread_id"],
                            activeRunId=None,
                            pendingRequestCount=0,
                            status="failed",
                            latestRunId=child["childRunId"],
                            runCount=1,
                        ),
                        recentRuns=[
                            dict(runId=child["childRunId"], ordinal=1, status="failed")
                        ],
                        childrenObserved=True,
                        hasPendingChildRuns=False,
                    ),
                )
            )
    finally:
        state.close()
        store.close()
    return dict(
        schema_version=1,
        kind="terminal-carry-v1",
        observed_at=time.time(),
        caller_thread_id="operator-thread",
        task_ids=w["carry"],
        tasks=records,
        runtime=dict(
            pid=4539,
            protocol_version=1,
            release_id="old",
            config_path=str(w["conf"]),
            config_sha256=runtime.config_hash(w["conf"]),
        ),
    )


def controller_proof(w, proof):
    service = w["service"]
    status, request = service.status, service.request

    def observed():
        value = status()
        if service.running:
            value["runtime"] = proof["runtime"]
        return value

    def read(kind, payload):
        if kind == "managed.read":
            return dict(source="service", adapter=dict(busy=False))
        return request(kind, payload)

    service.status, service.request = observed, read


def prove(w, proof, *, fresh=True):
    state = ReadState(w["config"])
    try:
        return reservation_carry.prove(
            state, w["config"], w["carry"], terminal_proof=proof, fresh=fresh
        )
    finally:
        state.close()


def test_reportless_carry_preserves_tasks_reservations_and_capacity(
    runtime_world, tmp_path
):
    w = runtime_world
    mutate(w, lambda state, _: state.set_capacity(3))
    proof = witness(w)
    controller_proof(w, proof)
    before = snapshot(w["config"])
    actor = dict(user="fixture", authorization=AUTHORIZATION, terminal_proof=proof)
    path = w["root"] / "service-operation.json"
    result = service_control.carry_stop(w["service"], None, path, w["carry"], actor)
    assert result["maintenance"] == "held"
    assert snapshot(w["config"]) == before
    journal = json.loads(path.read_text())
    backup = journal["carry"]["terminal_preflight"]["backup_root"]
    from pathlib import Path

    assert Path(backup, "state.sqlite3").is_file()
    assert Path(backup, "managed-tasks.sqlite3").is_file()
    proof_path = tmp_path / "proof.json"
    proof_path.write_text(json.dumps(proof))
    active = runtime.activate(
        w["root"],
        w["bins"],
        w["conf"],
        w["service"],
        "new",
        carry=w["carry"],
        authorization=AUTHORIZATION,
        terminal_proof_path=proof_path,
    )
    assert active["state"] == "active" and not active["service_started"]
    started = service_control.start_operation(
        w["service"],
        json.loads(path.read_text()),
        path,
        carry=w["carry"],
        actor=actor,
    )
    assert started["carried_reservations"] == w["carry"]
    assert snapshot(w["config"]) == before
    state = ReadState(w["config"])
    try:
        assert state.capacity()["requested"] == 3
        assert state.capacity()["occupied"] == 2
        assert state.get_meta("runtime_maintenance") is None
    finally:
        state.close()


@pytest.mark.parametrize(
    "change",
    [
        "active",
        "pending",
        "descendants",
        "wrong_thread",
        "wrong_parent",
        "wrong_project",
        "wrong_original",
        "truncated",
        "stale",
        "future",
        "wrong_task_hash",
        "wrong_reservation_hash",
        "duplicate",
        "unknown_status",
        "missing_children",
        "different_task",
    ],
)
def test_invalid_terminal_proof_cannot_change_state_or_release_reservation(
    world, change
):
    proof = witness(world)
    before = snapshot(world["config"])
    record = proof["tasks"][0]
    thread = record["snapshot"]["thread"]
    if change == "active":
        thread["activeRunId"] = "live"
    elif change == "pending":
        thread["pendingRequestCount"] = 1
    elif change == "descendants":
        record["snapshot"]["hasPendingChildRuns"] = True
    elif change == "wrong_thread":
        thread["threadId"] = "other"
    elif change == "wrong_parent":
        thread["parentThreadId"] = "other"
    elif change == "wrong_project":
        thread["projectId"] = "other"
    elif change == "wrong_original":
        record["snapshot"]["recentRuns"][0]["runId"] = "other"
    elif change == "truncated":
        thread["runCount"] = 2
    elif change == "stale":
        proof["observed_at"] -= 301
    elif change == "future":
        proof["observed_at"] += 60
    elif change == "wrong_task_hash":
        record["task_sha256"] = "other"
    elif change == "wrong_reservation_hash":
        record["reservation_sha256"] = "other"
    elif change == "duplicate":
        proof["tasks"].append(copy.deepcopy(record))
    elif change == "unknown_status":
        thread["status"] = "unknown"
    elif change == "missing_children":
        record["snapshot"].pop("childrenObserved")
    else:
        proof["task_ids"] = ["other"]
    with pytest.raises(StateError):
        prove(world, proof)
    assert snapshot(world["config"]) == before


def test_changed_ledger_invalidates_fresh_host_proof(world):
    proof = witness(world)
    mutate(
        world,
        lambda _, store: store.update(
            world["submitted_task"], "executing", {"new": True}
        ),
    )
    with pytest.raises(StateError, match="binding_changed"):
        prove(world, proof)


def test_proof_is_pinned_to_maintenance_and_retry_must_keep_exact_bytes(world):
    proof = witness(world)
    actor = dict(terminal_proof=proof, authorization=AUTHORIZATION)
    loaded, state, store = open_world(world["conf"])
    store.close()
    try:
        first = maintenance.begin(
            state, "op-terminal", world["carry"], config=loaded, actor=actor
        )
        changed = copy.deepcopy(proof)
        changed["observed_at"] += 0.001
        with pytest.raises(StateError, match="terminal_proof_mismatch"):
            maintenance.begin(
                state,
                "op-terminal",
                world["carry"],
                config=loaded,
                actor=dict(terminal_proof=changed),
            )
        with pytest.raises(StateError, match="terminal_proof_mismatch"):
            maintenance.end(
                state, "op-terminal", first["token"], world["carry"], config=loaded
            )
        maintenance.end(
            state,
            "op-terminal",
            first["token"],
            world["carry"],
            config=loaded,
            actor=actor,
        )
    finally:
        state.close()


@pytest.mark.parametrize("change", ["runtime", "busy", "stale"])
def test_pre_stop_refuses_before_bootout(runtime_world, tmp_path, change):
    w = runtime_world
    proof = witness(w)
    controller_proof(w, proof)
    if change == "runtime":
        status = w["service"].status

        def wrong():
            value = status()
            value["runtime"] = dict(proof["runtime"], pid=99)
            return value

        w["service"].status = wrong
    elif change == "busy":
        w["service"].request = lambda *_: dict(
            source="service", adapter=dict(busy=True)
        )
    else:
        proof["observed_at"] -= 301
    with pytest.raises(StateError):
        pre_stop(w["config"], w["service"], proof, w["carry"], tmp_path / "backup")
    assert w["service"].running
    assert not (tmp_path / "backup").exists()


def test_stale_first_proof_refused(world):
    proof = witness(world)
    proof["observed_at"] -= 301
    with pytest.raises(StateError, match="stale"):
        validate(proof, world["carry"])


async def test_collector_uses_fresh_host_reads_and_rechecks_ledger(runtime_world):
    from oraculo.terminal_carry import collect

    w = runtime_world
    recorded = witness(w)
    controller_proof(w, recorded)
    frames = []
    snapshots = {
        t["snapshot"]["thread"]["threadId"]: t["snapshot"] for t in recorded["tasks"]
    }

    class Host:
        async def call(self, name, args):
            frames.append((name, args))
            if name == "orchestrator_capabilities":
                return dict(parentThreadId="operator-thread")
            if name == "t3_thread_list":
                return dict(
                    projectId="alpha",
                    total=len(snapshots),
                    nextCursor=None,
                    threads=[
                        dict(
                            threadId=s["thread"]["threadId"],
                            parentThreadId=s["thread"]["parentThreadId"],
                            status="failed",
                        )
                        for s in snapshots.values()
                    ],
                )
            assert name == "t3_thread_read"
            s = copy.deepcopy(snapshots[args["threadId"]])
            s["thread"]["settled"] = False
            return dict(s, items=[], hasMore=False, nextPosition=None)

    result = await collect(w["config"], w["service"], Host(), w["carry"])
    assert result["task_ids"] == recorded["task_ids"]
    assert len([f for f in frames if f[0] == "t3_thread_read"]) == 4
    assert prove(w, result)["terminal_proof_sha256"] == content_hash(result)
