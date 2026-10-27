"""Exceptional, audited maintenance that keeps finished executors' reservations."""

from __future__ import annotations

import argparse
import json
import sqlite3

import pytest
from test_runtime_install import config, release

from oraculo import maintenance
from oraculo import runtime as rt
from oraculo import service_control as sc
from oraculo.config import Config
from oraculo.protocol import content_hash
from oraculo.state import State, StateError
from oraculo.task_store import TaskStore

ORIGIN = {"thread_id": "origin-thread", "run_id": "origin-run"}
AUTHORIZATION = "Marco 2026-10-27: Corrigir + manutenção excepcional"
RUNNING = {"status": "running", "workState": "working", "hasPendingChildRuns": False}
DONE = {"status": "completed", "workState": "result_available"}


def open_world(conf):
    loaded = Config.load(conf)
    state = State(loaded.db_path, max_executors=loaded.max_executors)
    store = TaskStore(loaded.state_dir / "managed-tasks.sqlite3")
    return loaded, state, store


def add_task(state, store, key, history="submitted", *, submitter=None, reason=None):
    """Ledger shapes observed for WP2 (submitted) and WP6 (blocked)."""
    task_id = store.create(ORIGIN, key, {"title": key})["id"]
    child = {"childThreadId": "thread:" + key, "childRunId": "run:" + key + ":1"}
    state.reserve_managed(task_id, "claudeAgent")
    store.update(task_id, "launch_pending", {"intent": key}, launch_attempted=True)
    if history == "unbound":
        return task_id
    state.bind_managed(task_id, dict(child, **RUNNING))
    store.update(
        task_id,
        "launch_acknowledged",
        {"thread": child["childThreadId"]},
        child=child,
        transport_mode="native_child",
    )
    store.update(task_id, "executing", RUNNING, observed=RUNNING)
    if history == "executing":
        return task_id
    report = {"summary": "PASS " + key}
    store.update(
        task_id,
        "validation_submitted",
        {
            "report_sha256": content_hash(report),
            "submission": {
                "thread_id": submitter or child["childThreadId"],
                "run_id": child["childRunId"],
            },
        },
        submitted_report=report,
    )
    if history == "continued":
        store.update(task_id, "executing", RUNNING, observed=RUNNING)
    if history == "blocked":
        observed = dict(DONE, hasPendingChildRuns=False)
        store.update(task_id, "validating", observed, observed=observed)
        reason = reason or "terminal_execution_or_descendants_unproven"
        store.update(task_id, "blocked", {"reason": reason}, blocked_reason=reason)
    return task_id


@pytest.fixture
def world(tmp_path):
    conf = config(tmp_path / "config.toml")
    loaded, state, store = open_world(conf)
    wp2 = add_task(state, store, "wp2")
    wp6 = add_task(state, store, "wp6", "blocked")
    state.close()
    store.close()
    return dict(conf=conf, config=loaded, wp2=wp2, wp6=wp6, carry=sorted([wp2, wp6]))


def mutate(world, action):
    _, state, store = open_world(world["conf"])
    try:
        return action(state, store)
    finally:
        state.close()
        store.close()


def snapshot(loaded):
    """Reservations and task payloads, byte for byte."""
    state = sqlite3.connect(loaded.db_path)
    ledger = sqlite3.connect(loaded.state_dir / "managed-tasks.sqlite3")
    try:
        return (
            state.execute("SELECT * FROM managed_reservations ORDER BY id").fetchall(),
            ledger.execute(
                "SELECT id,payload FROM managed_tasks ORDER BY id"
            ).fetchall(),
        )
    finally:
        state.close()
        ledger.close()


def audits(loaded, kind="maintenance.carry"):
    db = sqlite3.connect(loaded.db_path)
    try:
        return [
            json.loads(row[0])
            for row in db.execute(
                "SELECT payload FROM audit_events WHERE kind=? ORDER BY id", (kind,)
            )
        ]
    finally:
        db.close()


def guard(world, carry, operation="op-carry"):
    actor = {"user": "fixture", "authorization": AUTHORIZATION}
    with maintenance.maintenance_guard(
        world["config"], operation, carry=carry, actor=actor
    ) as result:
        return result


def test_carry_accepts_finished_executors_and_preserves_everything(world):
    before = snapshot(world["config"])
    result = guard(world, world["carry"])
    assert result["carry_quiescent"] is True
    assert result["admission_closed"] is True
    # Quiescence itself is unchanged: the carried slots still count.
    assert result["quiescent"] is False
    assert result["capacity"]["occupied"] == 2
    proof = result["carry_proof"]
    assert proof["task_ids"] == world["carry"] and proof["released"] == []
    by_id = {t["task_id"]: t for t in proof["tasks"]}
    assert by_id[world["wp2"]]["phase"] == "validation_submitted"
    assert by_id[world["wp6"]]["blocked_reason"] == (
        "terminal_execution_or_descendants_unproven"
    )
    assert by_id[world["wp6"]]["submission"]["run_id"] == "run:wp6:1"
    assert snapshot(world["config"]) == before
    [audit] = audits(world["config"])
    assert audit["operation_id"] == "op-carry" and audit["phase"] == "begin"
    assert audit["actor"]["authorization"] == AUTHORIZATION
    assert audit["proof"]["sha256"] == proof["sha256"]
    state = State(world["config"].db_path, max_executors=3)
    try:
        assert maintenance.quiescent(state) is False
        assert state.get_meta("runtime_maintenance")["carry"] == {
            "task_ids": world["carry"]
        }
        ended = maintenance.end(
            state,
            "op-carry",
            result["token"],
            world["carry"],
            config=world["config"],
        )
    finally:
        state.close()
    assert ended["admission_closed"] is False and ended["carry_quiescent"] is True
    assert snapshot(world["config"]) == before
    assert [a["phase"] for a in audits(world["config"])] == ["begin", "end"]


def test_plain_maintenance_still_requires_quiescence(world):
    with pytest.raises(StateError, match="maintenance_managed_work_uncertain"):
        guard(world, None)
    state = State(world["config"].db_path, max_executors=3)
    try:
        with pytest.raises(StateError, match="maintenance_work_active_or_uncertain"):
            maintenance.begin(state, "op-plain")
        assert state.get_meta("runtime_maintenance") is None
    finally:
        state.close()


@pytest.mark.parametrize(
    "history,kwargs,code",
    [
        ("executing", {}, "carry_run_live"),
        ("continued", {}, "carry_run_live"),
        ("unbound", {}, "carry_launch_unbound"),
        ("submitted", {"submitter": "thread:origin"}, "carry_report_unproven"),
        (
            "blocked",
            {"reason": "host_response_uncertain"},
            "carry_host_effect_uncertain",
        ),
        (
            "blocked",
            {"reason": "launch_uncertain_no_unique_match"},
            "carry_host_effect_uncertain",
        ),
    ],
)
def test_carry_refuses_listed_task_with_possible_live_run(world, history, kwargs, code):
    task_id = mutate(world, lambda s, t: add_task(s, t, "suspect", history, **kwargs))
    before = snapshot(world["config"])
    with pytest.raises(StateError, match=code):
        guard(world, world["carry"] + [task_id])
    assert snapshot(world["config"]) == before
    assert audits(world["config"]) == []
    state = State(world["config"].db_path, max_executors=3)
    try:
        assert state.get_meta("runtime_maintenance") is None
    finally:
        state.close()


def test_carry_refuses_unlisted_occupancy_and_work(world):
    mutate(world, lambda s, t: add_task(s, t, "other"))
    with pytest.raises(StateError, match="carry_other_occupancy"):
        guard(world, world["carry"])


def test_carry_refuses_unreserved_launched_work_outside_list(world):
    def launched(state, store):
        task_id = add_task(state, store, "other")
        state.release_managed(task_id, dict(DONE, hasPendingChildRuns=False))

    mutate(world, launched)
    with pytest.raises(StateError, match="carry_other_managed_work"):
        guard(world, world["carry"])


def test_carry_refuses_uncertain_operation_and_pending_memory(world):
    mutate(
        world,
        lambda s, t: s.connection.execute(
            "INSERT INTO operations VALUES"
            "('op',NULL,'dispatch',NULL,'x',NULL,'uncertain','h',0)"
        ),
    )
    with pytest.raises(StateError, match="carry_operation_uncertain"):
        guard(world, world["carry"])
    mutate(world, lambda s, t: s.connection.execute("DELETE FROM operations"))

    def pending_cycle(state, store):
        state.connection.execute(
            "CREATE TABLE IF NOT EXISTS memory_cycles("
            "id TEXT PRIMARY KEY, project_id TEXT NOT NULL, payload TEXT NOT NULL)"
        )
        state.connection.execute(
            "INSERT INTO memory_cycles VALUES('c','p',?)",
            (json.dumps({"write_state": "pending"}),),
        )

    mutate(world, pending_cycle)
    with pytest.raises(StateError, match="carry_memory_cycle_pending"):
        guard(world, world["carry"])


def test_carry_requires_explicit_valid_complete_list(world):
    with pytest.raises(StateError, match="carry_reservation_missing"):
        guard(world, world["carry"] + ["task-" + "0" * 64])
    for bad in ([], ["wp2"], [world["wp2"], world["wp2"]]):
        with pytest.raises(StateError, match="carry_reservations_invalid"):
            guard(world, bad)


def test_carried_operation_never_falls_back_to_plain_or_other_list(world):
    token = guard(world, world["carry"])["token"]
    state = State(world["config"].db_path, max_executors=3)
    try:
        # What an older daemon or plain CLI would send for the same operation.
        with pytest.raises(StateError, match="maintenance_carry_required"):
            maintenance.begin(state, "op-carry")
        with pytest.raises(StateError, match="maintenance_carry_required"):
            maintenance.end(state, "op-carry", token)
        with pytest.raises(StateError, match="maintenance_carry_mismatch"):
            maintenance.begin(state, "op-carry", [world["wp2"]], config=world["config"])
        with pytest.raises(StateError, match="maintenance_already_active"):
            maintenance.begin(state, "op-other", world["carry"], config=world["config"])
    finally:
        state.close()


def test_end_accepts_daemon_release_but_not_new_occupancy(world):
    token = guard(world, world["carry"])["token"]

    def release_wp6(state, store):
        state.release_managed(world["wp6"], dict(DONE, hasPendingChildRuns=False))
        task = store.get(world["wp6"])
        store.update(
            world["wp6"], "settled", {"fixture": True}, work_status="succeeded"
        )
        return task

    mutate(world, release_wp6)
    mutate(
        world,
        lambda s, t: s.connection.execute(
            "INSERT INTO managed_reservations VALUES('intruder','codex',1,'reserved',NULL)"
        ),
    )
    state = State(world["config"].db_path, max_executors=3)
    try:
        with pytest.raises(StateError, match="carry_other_occupancy"):
            maintenance.end(
                state, "op-carry", token, world["carry"], config=world["config"]
            )
        state.connection.execute("DELETE FROM managed_reservations WHERE id='intruder'")
        ended = maintenance.end(
            state, "op-carry", token, world["carry"], config=world["config"]
        )
    finally:
        state.close()
    assert ended["carry_proof"]["released"] == [world["wp6"]]
    assert [t["task_id"] for t in ended["carry_proof"]["tasks"]] == [world["wp2"]]


class Service:
    """launchd/socket double; maintenance.status reads the real State."""

    supported = True

    def __init__(self, root, conf, running=True):
        self.root, self.config, self.running = root, conf, running
        self.steps = []
        # Health reported over the socket; identity is what the real
        # controller re-proves (see test_service_control for that logic).
        self.health, self.identity_proven = "online", True

    def status(self):
        if self.running and self.health != "online":
            return {
                "state": "failed",
                "error_code": "service_unhealthy",
                "health": self.health,
                "pid": 4539,
            }
        return {
            "state": "online" if self.running else "unloaded",
            "pid": 4539 if self.running else None,
        }

    def legacy_dispatch_acceptance(self, observed):
        self.steps.append("legacy_dispatch_acceptance")
        if not self.identity_proven:
            raise rt.RuntimeError("degraded_service_identity_unproven")
        return dict(
            accepted_as="legacy_dispatch_degraded",
            service_state=observed["state"],
            health=observed["health"],
            pid=observed["pid"],
        )

    def plist_bytes(self):
        return b"<plist>synthetic</plist>"

    def request(self, kind, payload):
        self.steps.append(kind)
        if kind != "maintenance.status":
            raise AssertionError("old daemon cannot carry: " + kind)
        loaded = Config.load(self.config)
        state = State(loaded.db_path, max_executors=loaded.max_executors)
        try:
            return maintenance.status(state)
        finally:
            state.close()

    def stop(self, recovery_release=None):
        self.steps.append(("stop", recovery_release))
        self.running = False

    def install(self, release_id):
        self.steps.append("install:" + release_id)

    def recovery_identity(self, release_id):
        return {"release_id": release_id}

    def start(self, release_id=None, previous_pid=None, *, accept_legacy=False):
        self.steps.append("start")
        self.running = True
        observed = self.status()
        if observed["state"] == "online":
            return observed
        acceptance = accept_legacy and sc.maintenance_acceptance(self, observed)
        if not acceptance:
            raise rt.RuntimeError("start_timeout")
        return dict(observed, service_acceptance=acceptance)


@pytest.fixture
def runtime_world(world, tmp_path):
    root, bins = tmp_path / "runtime", tmp_path / "bin"
    for name in ("old", "new"):
        release(root, name)
    rt.switch(root, "old")
    # Admission is exhausted before the controlled stop.
    mutate(world, lambda s, t: s.set_capacity(2))
    return dict(world, root=root, bins=bins, service=Service(root, world["conf"]))


def stop_with_carry(w):
    path = w["root"] / "service-operation.json"
    current = json.loads(path.read_text()) if path.exists() else None
    actor = {"user": "fixture", "authorization": AUTHORIZATION}
    return sc.carry_stop(w["service"], current, path, w["carry"], actor)


def test_full_carried_activation_config_change_and_start(runtime_world):
    w = runtime_world
    before = snapshot(w["config"])
    stopped = stop_with_carry(w)
    assert stopped["maintenance"] == "held"
    assert ("stop", "old") in w["service"].steps
    service_op = json.loads((w["root"] / "service-operation.json").read_text())
    assert service_op["state"] == "stopped" and service_op["token"]
    assert [a["phase"] for a in service_op["carry"]["audit"]] == [
        "pre_stop",
        "maintenance",
    ]
    assert service_op["carry"]["audit"][0]["capacity"]["available"] == 0

    with pytest.raises(rt.RuntimeError, match="carry_authorization_required"):
        rt.activate(
            w["root"], w["bins"], w["conf"], w["service"], "new", carry=w["carry"]
        )
    result = rt.activate(
        w["root"],
        w["bins"],
        w["conf"],
        w["service"],
        "new",
        carry=w["carry"],
        authorization=AUTHORIZATION,
    )
    assert result["state"] == "active" and result["service_started"] is False
    assert result["carried_reservations"] == w["carry"]
    assert rt.current_release(w["root"]) == "new"
    journal = json.loads((w["root"] / "operation.json").read_text())
    assert journal["state"] == "complete"
    assert journal["operation_id"] == service_op["operation_id"]
    assert [a["phase"] for a in journal["carry"]["audit"]] == ["maintenance", "end"]
    assert journal["carry"]["audit"][0]["actor"]["authorization"] == AUTHORIZATION
    assert (
        json.loads((w["root"] / "service-operation.json").read_text())["state"]
        == "complete"
    )
    assert snapshot(w["config"]) == before

    # Configuration change while unloaded, then a carried start.
    w["conf"].write_text(
        w["conf"].read_text().replace("max_executors = 3", "max_executors = 33")
    )
    with pytest.raises(StateError, match="maintenance_managed_work_uncertain"):
        sc.start_operation(w["service"], None, w["root"] / "service-operation.json")
    started = sc.start_operation(
        w["service"],
        json.loads((w["root"] / "service-operation.json").read_text()),
        w["root"] / "service-operation.json",
        carry=w["carry"],
        actor={"user": "fixture", "authorization": AUTHORIZATION},
    )
    assert started["carried_reservations"] == w["carry"]
    start_op = json.loads((w["root"] / "service-operation.json").read_text())
    assert start_op["state"] == "complete"
    assert start_op["capacity"]["configured_ceiling"] == 33
    assert [a["phase"] for a in start_op["carry"]["audit"]] == ["maintenance", "end"]
    assert w["service"].steps[-3:] == ["install:new", "start", "maintenance.status"]
    assert snapshot(w["config"]) == before
    state = State(w["config"].db_path, max_executors=33)
    try:
        assert state.get_meta("runtime_maintenance") is None
        assert state.set_capacity(33)["effective"] == 33
    finally:
        state.close()
    phases = [a["phase"] for a in audits(w["config"])]
    assert phases.count("begin") >= 3 and phases.count("end") == 2


def test_carried_activation_refuses_online_service_and_mismatch(runtime_world):
    w = runtime_world
    kwargs = dict(carry=w["carry"], authorization=AUTHORIZATION)
    with pytest.raises(rt.RuntimeError, match="carry_requires_stopped_service"):
        rt.activate(w["root"], w["bins"], w["conf"], w["service"], "new", **kwargs)
    assert not (w["root"] / "operation.json").exists()
    stop_with_carry(w)
    with pytest.raises(rt.RuntimeError, match="carry_mismatch"):
        rt.activate(
            w["root"],
            w["bins"],
            w["conf"],
            w["service"],
            "new",
            carry=[w["wp2"]],
            authorization=AUTHORIZATION,
        )
    with pytest.raises(rt.RuntimeError, match="carry_mismatch"):
        rt.activate(w["root"], w["bins"], w["conf"], w["service"], "new")
    assert rt.current_release(w["root"]) == "old"


def test_carry_stop_refuses_before_any_effect_when_admission_open(runtime_world):
    w = runtime_world
    mutate(w, lambda s, t: s.set_capacity(3))
    with pytest.raises(rt.RuntimeError, match="carry_admission_open"):
        stop_with_carry(w)
    mutate(w, lambda s, t: add_task(s, t, "other"))
    with pytest.raises(rt.RuntimeError, match="carry_occupancy_mismatch"):
        stop_with_carry(w)
    assert w["service"].running and not any(
        isinstance(step, tuple) for step in w["service"].steps
    )
    assert not (w["root"] / "service-operation.json").exists()


def test_carry_stop_records_refusal_after_offline_proof(runtime_world):
    w = runtime_world
    mutate(w, lambda s, t: s.set_capacity(3))
    suspect = mutate(w, lambda s, t: add_task(s, t, "live", "executing"))
    w["carry"] = sorted(w["carry"] + [suspect])
    before = snapshot(w["config"])
    with pytest.raises(StateError, match="carry_run_live"):
        stop_with_carry(w)
    journal = json.loads((w["root"] / "service-operation.json").read_text())
    assert journal["state"] == "carry_refused"
    assert journal["carry"]["refusal"]["error_code"] == "carry_run_live"
    assert snapshot(w["config"]) == before
    state = State(w["config"].db_path, max_executors=3)
    try:
        assert state.get_meta("runtime_maintenance") is None
    finally:
        state.close()


def pending_journal(w, **changes):
    journal = dict(
        schema_version=1,
        operation_id="50510e86-1d69-472a-a97f-f856f7cb707b",
        state="acquiring_maintenance",
        previous="old",
        target="mid",
        config_sha256=rt.config_hash(w["conf"]),
        token=None,
        was_running=True,
        capacity=None,
        previous_pid=4539,
        old_plist="<plist>synthetic</plist>",
    )
    journal.update(changes)
    rt.write_json(w["root"] / "operation.json", journal)
    return journal


def test_abandon_unacquired_operation_then_carried_activation(runtime_world):
    w = runtime_world
    original = pending_journal(w)
    raw = (w["root"] / "operation.json").read_bytes()
    with pytest.raises(rt.RuntimeError, match="recovery_required"):
        rt.activate(w["root"], w["bins"], w["conf"], w["service"], "new")
    with pytest.raises(rt.RuntimeError, match="carry_mismatch"):
        rt.activate(
            w["root"],
            w["bins"],
            w["conf"],
            w["service"],
            "mid",
            carry=w["carry"],
            authorization=AUTHORIZATION,
        )
    with pytest.raises(rt.RuntimeError, match="recovery_required"):
        stop_with_carry(w)
    with pytest.raises(rt.RuntimeError, match="operation_mismatch"):
        rt.abandon(w["root"], w["conf"], w["service"], "other", authorization="x")
    planned = rt.abandon(
        w["root"],
        w["conf"],
        w["service"],
        original["operation_id"],
        authorization=AUTHORIZATION,
        dry_run=True,
    )
    assert planned["state"] == "planned"
    assert (w["root"] / "operation.json").read_bytes() == raw
    result = rt.abandon(
        w["root"],
        w["conf"],
        w["service"],
        original["operation_id"],
        authorization=AUTHORIZATION,
    )
    assert result["state"] == "abandoned"
    assert result["proof"]["service_state"] == "online"
    archive = w["root"] / "operations-archive" / (original["operation_id"] + ".json")
    assert archive.read_bytes() == raw
    journal = json.loads((w["root"] / "operation.json").read_text())
    assert journal["state"] == "abandoned" and journal["target"] == "mid"
    assert journal["abandoned"]["actor"]["authorization"] == AUTHORIZATION
    assert rt.diagnose(w["root"], w["bins"], w["conf"])["state"] != "recovery_required"
    # Idempotent replay returns the recorded abandonment.
    again = rt.abandon(
        w["root"], w["conf"], w["service"], original["operation_id"], authorization="x"
    )
    assert again["archive_sha256"] == result["archive_sha256"]

    stop_with_carry(w)
    rt.activate(
        w["root"],
        w["bins"],
        w["conf"],
        w["service"],
        "new",
        carry=w["carry"],
        authorization=AUTHORIZATION,
    )
    journal = json.loads((w["root"] / "operation.json").read_text())
    assert journal["supersedes"]["operation_id"] == original["operation_id"]
    assert journal["supersedes"]["archive_sha256"] == result["archive_sha256"]


@pytest.mark.parametrize(
    "changes",
    [
        {"token": "acquired"},
        {"state": "switching"},
        {"previous": "new"},
        {"old_plist": "<plist>other</plist>"},
        {"config_sha256": "0" * 64},
    ],
)
def test_abandon_refuses_when_effects_are_possible(runtime_world, changes):
    w = runtime_world
    journal = pending_journal(w, **changes)
    raw = (w["root"] / "operation.json").read_bytes()
    with pytest.raises(
        rt.RuntimeError, match="operation_effects_possible|config_changed"
    ):
        rt.abandon(
            w["root"],
            w["conf"],
            w["service"],
            journal["operation_id"],
            authorization=AUTHORIZATION,
        )
    assert (w["root"] / "operation.json").read_bytes() == raw
    assert not (w["root"] / "operations-archive").exists()


def test_abandon_refuses_operation_holding_maintenance_offline(runtime_world):
    w = runtime_world
    journal = pending_journal(w)
    w["service"].running = False
    mutate(
        w,
        lambda s, t: s.set_meta(
            "runtime_maintenance",
            {"operation_id": journal["operation_id"], "token": "lost-ack"},
        ),
    )
    with pytest.raises(rt.RuntimeError, match="operation_holds_maintenance"):
        rt.abandon(
            w["root"],
            w["conf"],
            w["service"],
            journal["operation_id"],
            authorization=AUTHORIZATION,
        )
    assert json.loads((w["root"] / "operation.json").read_text())["state"] == (
        "acquiring_maintenance"
    )


def test_plain_activation_of_carried_world_is_refused_then_abandonable(runtime_world):
    w = runtime_world
    w["service"].running = False
    with pytest.raises(StateError, match="maintenance_managed_work_uncertain"):
        rt.activate(w["root"], w["bins"], w["conf"], w["service"], "new")
    journal = json.loads((w["root"] / "operation.json").read_text())
    assert journal["state"] == "acquiring_maintenance" and journal["token"] is None
    result = rt.abandon(
        w["root"],
        w["conf"],
        w["service"],
        journal["operation_id"],
        authorization=AUTHORIZATION,
    )
    assert result["proof"]["service_state"] == "unloaded"
    assert result["proof"]["maintenance_holder"] is None


def test_carried_rollback_and_start_of_previous_release(runtime_world):
    w = runtime_world
    kwargs = dict(carry=w["carry"], authorization=AUTHORIZATION)
    stop_with_carry(w)
    rt.activate(w["root"], w["bins"], w["conf"], w["service"], "new", **kwargs)
    result = rt.activate(
        w["root"], w["bins"], w["conf"], w["service"], rollback=True, **kwargs
    )
    assert result["release_id"] == "old" and rt.current_release(w["root"]) == "old"
    path = w["root"] / "service-operation.json"
    sc.start_operation(
        w["service"],
        json.loads(path.read_text()),
        path,
        carry=w["carry"],
        actor={"user": "fixture"},
    )
    assert json.loads(path.read_text())["state"] == "complete"
    assert w["service"].running


def test_cli_exposes_carry_only_on_maintenance_steps():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command")
    rt.register_parsers(commands)
    sc.register_parsers(commands)
    task = "task-" + "a" * 64
    args = parser.parse_args(
        [
            "runtime",
            "activate",
            "rel",
            "--carry-reservation",
            task,
            "--carry-reservation",
            task.replace("a", "b"),
            "--carry-authorization",
            AUTHORIZATION,
        ]
    )
    assert args.carry_reservations == [task, task.replace("a", "b")]
    assert args.carry_authorization == AUTHORIZATION
    for argv in (
        ["runtime", "rollback", "--carry-reservation", task],
        ["service", "stop", "--carry-reservation", task],
        ["service", "start", "--carry-reservation", task],
        ["runtime", "abandon-operation", "--operation-id", "x", "--authorization", "y"],
    ):
        parser.parse_args(argv)
    for argv in (
        ["service", "restart", "--carry-reservation", task],
        ["runtime", "upgrade", "--bundle", "b", "--carry-reservation", task],
        ["runtime", "abandon-operation", "--operation-id", "x"],
    ):
        with pytest.raises(SystemExit):
            parser.parse_args(argv)


# The live daemon reports failed/service_unhealthy only because a legacy
# objective exhausted its dispatch attempts. Maintenance paths tolerate that
# single, settled dispatcher signal with re-proven identity; nothing else.
EXHAUSTED = "dispatch_attempts_exhausted"
REFUSED_HEALTH = [
    "awaiting_coordinator",
    "dispatch_identity_unknown",
    "uncertain_coordinator_launch",
    "disconnected",
    "never-seen-health",
]


def test_abandon_accepts_daemon_degraded_only_by_legacy_dispatcher(runtime_world):
    w = runtime_world
    w["service"].health = EXHAUSTED
    journal = pending_journal(w)
    raw = (w["root"] / "operation.json").read_bytes()
    planned = rt.abandon(
        w["root"],
        w["conf"],
        w["service"],
        journal["operation_id"],
        authorization=AUTHORIZATION,
        dry_run=True,
    )
    assert planned["state"] == "planned"
    assert planned["proof"]["service_health"] == EXHAUSTED
    assert (w["root"] / "operation.json").read_bytes() == raw
    result = rt.abandon(
        w["root"],
        w["conf"],
        w["service"],
        journal["operation_id"],
        authorization=AUTHORIZATION,
    )
    assert result["state"] == "abandoned"
    proof = json.loads((w["root"] / "operation.json").read_text())["abandoned"]
    assert proof["proof"]["service_state"] == "failed"
    assert proof["proof"]["service_health"] == EXHAUSTED
    assert proof["proof"]["service_acceptance"]["accepted_as"] == (
        "legacy_dispatch_degraded"
    )
    assert proof["proof"]["maintenance_holder"] is None
    assert "maintenance.status" in w["service"].steps


@pytest.mark.parametrize("health", REFUSED_HEALTH)
def test_abandon_refuses_any_other_unhealthy_daemon(runtime_world, health):
    w = runtime_world
    w["service"].health = health
    journal = pending_journal(w)
    raw = (w["root"] / "operation.json").read_bytes()
    with pytest.raises(rt.RuntimeError, match="service_state_unproven"):
        rt.abandon(
            w["root"],
            w["conf"],
            w["service"],
            journal["operation_id"],
            authorization=AUTHORIZATION,
        )
    assert (w["root"] / "operation.json").read_bytes() == raw
    assert "legacy_dispatch_acceptance" not in w["service"].steps


def test_abandon_refuses_degraded_daemon_without_coherent_identity(runtime_world):
    w = runtime_world
    w["service"].health, w["service"].identity_proven = EXHAUSTED, False
    journal = pending_journal(w)
    raw = (w["root"] / "operation.json").read_bytes()
    with pytest.raises(rt.RuntimeError, match="degraded_service_identity_unproven"):
        rt.abandon(
            w["root"],
            w["conf"],
            w["service"],
            journal["operation_id"],
            authorization=AUTHORIZATION,
        )
    assert (w["root"] / "operation.json").read_bytes() == raw
    assert not (w["root"] / "operations-archive").exists()


def test_carry_stop_and_start_accept_legacy_dispatcher_health(runtime_world):
    w = runtime_world
    w["service"].health = EXHAUSTED
    before = snapshot(w["config"])
    stopped = stop_with_carry(w)
    assert stopped["maintenance"] == "held"
    assert ("stop", "old") in w["service"].steps
    pre_stop = json.loads((w["root"] / "service-operation.json").read_text())["carry"][
        "audit"
    ][0]
    assert pre_stop["phase"] == "pre_stop"
    assert pre_stop["service"]["state"] == "failed"
    assert pre_stop["service"]["health"] == EXHAUSTED
    assert pre_stop["service_acceptance"]["accepted_as"] == "legacy_dispatch_degraded"
    rt.activate(
        w["root"],
        w["bins"],
        w["conf"],
        w["service"],
        "new",
        carry=w["carry"],
        authorization=AUTHORIZATION,
    )
    path = w["root"] / "service-operation.json"
    started = sc.start_operation(
        w["service"],
        json.loads(path.read_text()),
        path,
        carry=w["carry"],
        actor={"user": "fixture", "authorization": AUTHORIZATION},
    )
    assert started["carried_reservations"] == w["carry"]
    assert started["service_acceptance"]["health"] == EXHAUSTED
    operation = json.loads(path.read_text())
    assert operation["state"] == "complete"
    assert operation["service_acceptance"]["accepted_as"] == "legacy_dispatch_degraded"
    assert snapshot(w["config"]) == before


def test_carried_start_resumes_after_bootstrap_with_legacy_health(runtime_world):
    w = runtime_world
    stop_with_carry(w)
    rt.activate(
        w["root"],
        w["bins"],
        w["conf"],
        w["service"],
        "new",
        carry=w["carry"],
        authorization=AUTHORIZATION,
    )
    path = w["root"] / "service-operation.json"
    actor = {"user": "fixture", "authorization": AUTHORIZATION}
    # A plain wait would have timed out after the bootstrap: carry_starting.
    w["service"].health = "awaiting_coordinator"
    with pytest.raises(rt.RuntimeError, match="start_timeout"):
        sc.start_operation(
            w["service"], json.loads(path.read_text()), path, carry=w["carry"]
        )
    assert json.loads(path.read_text())["state"] == "carry_starting"
    with pytest.raises(rt.RuntimeError, match="service_state_unproven"):
        sc.start_operation(
            w["service"], json.loads(path.read_text()), path, carry=w["carry"]
        )
    w["service"].health = EXHAUSTED
    sc.start_operation(
        w["service"], json.loads(path.read_text()), path, carry=w["carry"], actor=actor
    )
    assert json.loads(path.read_text())["state"] == "complete"


@pytest.mark.parametrize("health", REFUSED_HEALTH)
def test_carry_stop_refuses_other_unhealthy_daemon_before_any_effect(
    runtime_world, health
):
    w = runtime_world
    w["service"].health = health
    with pytest.raises(rt.RuntimeError, match="service_state_unproven"):
        stop_with_carry(w)
    assert w["service"].running
    assert not any(isinstance(step, tuple) for step in w["service"].steps)
    assert not (w["root"] / "service-operation.json").exists()


def test_carry_stop_refuses_degraded_daemon_without_coherent_identity(runtime_world):
    w = runtime_world
    w["service"].health, w["service"].identity_proven = EXHAUSTED, False
    with pytest.raises(rt.RuntimeError, match="degraded_service_identity_unproven"):
        stop_with_carry(w)
    assert w["service"].running
    assert not any(isinstance(step, tuple) for step in w["service"].steps)
    assert not (w["root"] / "service-operation.json").exists()


def test_plain_paths_still_require_online(runtime_world):
    w = runtime_world
    w["service"].health = EXHAUSTED
    with pytest.raises(rt.RuntimeError, match="service_state_unproven"):
        rt.activate(w["root"], w["bins"], w["conf"], w["service"], "new")
    with pytest.raises(rt.RuntimeError, match="service_state_unproven"):
        sc.start_operation(w["service"], None, w["root"] / "service-operation.json")
    assert "legacy_dispatch_acceptance" not in w["service"].steps
