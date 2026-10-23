"""Synthetic producer/real flush regressions; SQLite is always disposable."""

import copy

from oraculo.managed_tasks import ManagedTasks
from oraculo.presentation import parse_return_message
from oraculo.task_store import TaskStore

BATCH = [
    "pr_policy_recorded",
    "pr_ready",
    "workspace_ready",
    "validating",
    "pr_waiting",
    "merge_pending",
    "merged",
    "final_memory_pending",
    "settlement_pending",
    "settled",
]


class Host:
    def __init__(self):
        self.calls = []
        self.messages = {}
        self.lose_ack = False

    async def call(self, name, args):
        assert name == "t3_thread_send"
        self.calls.append(copy.deepcopy(args))
        key = args["clientRequestId"]
        if key in self.messages:
            assert self.messages[key] == args
        self.messages[key] = copy.deepcopy(args)
        if self.lose_ack:
            self.lose_ack = False
            raise TimeoutError("synthetic ACK lost after acceptance")
        return {"threadId": args["threadId"], "messageId": "message-" + key}


def create(store, key="key", origin="origin", **request):
    return store.create(
        {"thread_id": origin, "run_id": "run"},
        key,
        {"title": "Synthetic task", **request},
    )["id"]


def engine(store, host):
    return ManagedTasks(None, store, host, None)


async def test_ten_final_presentations_send_once_with_complete_history(tmp_path):
    store = TaskStore(tmp_path / "ledger.db")
    host = Host()
    ident = create(store)
    await engine(store, host).flush_returns(ident)
    host.calls.clear()
    for i, phase in enumerate(BATCH):
        store.update(ident, phase, {"synthetic": i}, work_status="succeeded")
    await engine(store, host).flush_returns(ident)
    assert len(host.calls) == 1
    assert (
        parse_return_message(host.calls[0]["message"])["presentation"]["phase"]
        == "Encerrada"
    )
    assert len(store.get(ident)["events"]) == 11
    assert store.history(ident)["total"] == 11
    assert store.describe(ident)["pending_returns"] == 0
    await engine(store, host).flush_returns(ident)
    assert len(host.calls) == 1
    store.close()


async def test_semantic_dedup_survives_restart_without_false_ack(tmp_path):
    import pytest

    from oraculo.state import StateError

    path = tmp_path / "ledger.db"
    store = TaskStore(path)
    host = Host()
    ident = create(store)
    await engine(store, host).flush_returns(ident)
    store.close()
    store = TaskStore(path)
    original = store.get(ident)["events"][0]
    # Distinct audit evidence, identical useful status, different observed time.
    store.update(ident, "received", {"technical_retry": 2})
    await engine(store, host).flush_returns(ident)
    assert len(host.calls) == 1
    audit = store.history(ident)["returns"]
    assert audit[0]["delivery_status"] == "host_confirmed"
    assert audit[1]["disposition"] == "suppressed"
    assert audit[1]["delivery_status"] == "covered_by_confirmed"
    assert audit[1]["receipt"] is None
    assert audit[1]["representative_id"].endswith(original["id"])
    with pytest.raises(StateError, match="return_was_not_sent"):
        store.acknowledge_return(
            audit[1]["return_id"], {"threadId": "origin", "messageId": "fake"}
        )
    assert store.describe(ident)["pending_returns"] == 0
    store.close()


async def test_lost_ack_restart_replays_exact_intention_before_new_state(tmp_path):
    path = tmp_path / "ledger.db"
    store = TaskStore(path)
    ident = create(store)
    for i in range(9):
        store.update(ident, "received", {"attempt": i})
    host = Host()
    host.lose_ack = True
    await engine(store, host).flush_returns(ident)
    original = copy.deepcopy(host.calls[0])
    assert store.describe(ident)["pending_returns"] == 1
    audit = store.history(ident)["returns"]
    assert sum(a["disposition"] == "coalesced" for a in audit) == 9
    assert all(a["receipt"] is None for a in audit)
    assert all(a["delivery_status"] == "covered_by_pending" for a in audit[:-1])
    store.close()
    store = TaskStore(path)
    store.update(
        ident,
        "blocked",
        {"reason": "approval_required"},
        blocked_reason="approval_required",
    )
    await engine(store, host).flush_returns(ident)
    assert host.calls[1] == original
    assert len(host.calls) == 3 and len(host.messages) == 2
    assert (
        parse_return_message(host.calls[-1]["message"])["presentation"]["phase"]
        == "Bloqueada"
    )
    assert store.describe(ident)["pending_returns"] == 0
    assert all(a["receipt"] is None for a in store.history(ident)["returns"][:9])
    store.close()


async def test_crash_after_preparation_and_after_ack_commit(tmp_path):
    path = tmp_path / "ledger.db"
    store = TaskStore(path)
    ident = create(store)
    for i in range(9):
        store.update(ident, "received", {"attempt": i})
    selected = store.prepare_returns(ident)
    frozen = store.delivery(selected[0])
    assert len(selected) == 1
    store.close()  # process dies before host I/O
    store = TaskStore(path)
    host = Host()
    await engine(store, host).flush_returns(ident)
    assert parse_return_message(host.calls[0]["message"]) == frozen
    store.close()  # ACK committed, caller never got the result
    store = TaskStore(path)
    await engine(store, host).flush_returns(ident)
    assert len(host.calls) == 1
    assert store.describe(ident)["pending_returns"] == 0
    store.close()


async def test_human_changes_progress_blockers_approval_and_result(tmp_path):
    store = TaskStore(tmp_path / "ledger.db")
    ident = create(store)
    host = Host()
    await engine(store, host).flush_returns(ident)
    changes = [
        ("executing", {"progress": "Inspecting source"}, {}),
        ("executing", {"progress": "Validating result"}, {}),
        ("blocked", {"reason": "unknown_one"}, {"blocked_reason": "unknown_one"}),
        ("blocked", {"reason": "unknown_two"}, {"blocked_reason": "unknown_two"}),
        (
            "awaiting_approval",
            {"plan": "ready"},
            {
                "deliverable_kind": "plan",
                "approval_state": "pending",
                "work_status": "succeeded",
            },
        ),
        ("plan_approved", {"approved": True}, {"approval_state": "approved"}),
        ("executing", {"resumed": True}, {"work_status": "pending"}),
        ("failed", {"validation": "failed"}, {"work_status": "failed"}),
    ]
    for n, (phase, proof, fields) in enumerate(changes, start=2):
        store.update(ident, phase, proof, **fields)
        await engine(store, host).flush_returns(ident)
        assert len(host.calls) == n
        # Continuing action requirements remain visible in their last notice;
        # repeated technical proofs do not generate repeated reminders.
        store.update(ident, phase, {**proof, "technical": n}, **fields)
        await engine(store, host).flush_returns(ident)
        assert len(host.calls) == n
    messages = [parse_return_message(a["message"])["presentation"] for a in host.calls]
    assert messages[2]["activity"] == "Validating result"
    assert "revisar o planejamento" in messages[5]["next_action"]
    assert "negativo" in messages[-1]["result"]
    store.close()


async def test_return_to_previous_phase_is_a_new_useful_change(tmp_path):
    store = TaskStore(tmp_path / "ledger.db")
    ident = create(store)
    host = Host()
    for n, phase in enumerate(["executing", "blocked", "executing"]):
        store.update(ident, phase, {"step": n})
        await engine(store, host).flush_returns(ident)
    assert len(host.calls) == 3  # cursor compares last confirmation, not all time
    store.close()


async def test_tasks_and_origins_do_not_suppress_each_other(tmp_path):
    store = TaskStore(tmp_path / "ledger.db")
    host = Host()
    ids = [create(store), create(store, key="second"), create(store, origin="other")]
    for ident in ids:
        await engine(store, host).flush_returns(ident)
    assert len(host.calls) == 3
    assert [a["threadId"] for a in host.calls] == ["origin", "origin", "other"]
    store.close()


async def test_json_preserves_every_envelope_and_frozen_readable_is_never_rewritten(
    tmp_path,
):
    import json

    store = TaskStore(tmp_path / "ledger.db")
    ident = create(store, return_format="json")
    for i, phase in enumerate(BATCH):
        store.update(ident, phase, {"synthetic": i})
    host = Host()
    await engine(store, host).flush_returns(ident)
    assert len(host.calls) == 11
    assert all(json.loads(a["message"])["task_id"] == ident for a in host.calls)
    readable = create(store, key="readable")
    for i in range(3):
        store.update(readable, "received", {"synthetic": i})
    from oraculo.presentation import return_message

    expected = {
        event["id"]: return_message(store.delivery(event))
        for event in store.pending_returns(readable)
    }
    await engine(store, host).flush_returns(readable)
    assert len(host.calls) == 15  # all four pre-existing immutable deliveries
    assert all(a["message"] == expected[a["clientRequestId"]] for a in host.calls[-4:])
    store.update(readable, "received", {"fresh": True})
    await engine(store, host).flush_returns(readable)
    assert len(host.calls) == 15  # last old frozen ACK seeds the semantic cursor
    store.close()


async def test_formatless_legacy_and_cached_json_are_not_coalesced(tmp_path):
    import json

    store = TaskStore(tmp_path / "ledger.db")
    ident = create(store)
    task = store.get(ident)
    task.pop("return_format")
    store.db.execute(
        "UPDATE managed_tasks SET payload=? WHERE id=?", (json.dumps(task), ident)
    )
    for i in range(3):
        store.update(ident, "executing", {"step": i})
    host = Host()
    await engine(store, host).flush_returns(ident)
    assert len(host.calls) == 4
    assert all(json.loads(a["message"]) for a in host.calls)
    store.close()


async def test_preparation_rolls_back_atomically(tmp_path, monkeypatch):
    import pytest

    store = TaskStore(tmp_path / "ledger.db")
    ident = create(store)
    store.update(ident, "executing", {"step": 1})
    original = store._notification

    def fail_after_write(*args):
        original(*args)
        raise RuntimeError("synthetic prepare interruption")

    monkeypatch.setattr(store, "_notification", fail_after_write)
    with pytest.raises(RuntimeError):
        store.prepare_returns(ident)
    assert len(store.pending_returns(ident)) == 2
    assert (
        store.db.execute("SELECT count(*) FROM task_notifications").fetchone()[0] == 0
    )
    assert (
        store.db.execute(
            "SELECT count(*) FROM task_returns WHERE delivery IS NOT NULL"
        ).fetchone()[0]
        == 0
    )
    monkeypatch.setattr(store, "_notification", original)
    host = Host()
    await engine(store, host).flush_returns(ident)
    assert len(host.calls) == 1
    store.close()


async def test_sends_cannot_recursively_extend_the_batch(tmp_path):
    store = TaskStore(tmp_path / "ledger.db")
    ident = create(store)
    host = Host()
    call = host.call

    async def noisy(name, args):
        store.update(ident, "received", {"during_send": True})
        return await call(name, args)

    host.call = noisy
    await engine(store, host).flush_returns(ident)
    assert len(host.calls) == 1
    assert store.describe(ident)["pending_returns"] == 1
    await engine(store, host).flush_returns(ident)
    assert len(host.calls) == 1
    assert store.describe(ident)["pending_returns"] == 0
    store.close()


async def test_additive_migration_and_offline_audit(tmp_path):
    path = tmp_path / "ledger.db"
    store = TaskStore(path)
    ident = create(store)
    store.db.execute("DROP TABLE task_notifications")  # exact pre-feature layout
    store.db.execute("DELETE FROM schema_migrations WHERE version=5")
    original_events = store.get(ident)["events"]
    store.close()
    old = TaskStore(path, read_only=True)
    assert len(old.pending_returns(ident)) == 1
    assert old.history(ident)["returns"][0]["disposition"] == "unprepared"
    old.close()
    store = TaskStore(path)
    assert store.get(ident)["events"] == original_events
    store.update(ident, "received", {"technical": 2})
    await engine(store, Host()).flush_returns(ident)
    store.close()
    offline = TaskStore(path, read_only=True)
    audit = offline.history(ident)
    assert len(audit["events"]) == len(audit["returns"]) == 2
    assert audit["returns"][0]["disposition"] == "coalesced"
    assert offline.describe(ident)["pending_returns"] == 0
    offline.close()


async def test_socket_producer_progress_finish_and_history(
    running_service, service_config
):
    import asyncio
    import json
    import sys

    from test_managed_service import Caller
    from test_managed_tasks import initialize, report

    from oraculo.managed_service import command

    service = running_service
    initialize(service.config.projects_root / "alpha")
    host = Caller(service.config.projects_root)
    task = await command(
        service.config,
        "start",
        {
            "kind": "work",
            "key": "notification-integration",
            "text": "Synthetic work",
            "cwd": str(service.config.projects_root / "alpha"),
        },
        host,
    )

    def sends():
        return [args for name, args in host.calls if name == "t3_thread_send"]

    assert len(sends()) == 1  # actual start creates nine technical events
    assert task["event_count"] == 9
    for i, text in enumerate(["Checking source", "Checking source", "Checking tests"]):
        task = await command(
            service.config,
            "progress",
            {
                "task_id": task["id"],
                "key": "progress-" + str(i),
                "message": text,
            },
            host,
        )
    assert len(sends()) == 3
    host.complete(task["child"]["childThreadId"])
    task = await command(
        service.config,
        "finish",
        {
            "task_id": task["id"],
            "report": report(task),
        },
        host,
    )
    assert task["memory"]["write_state"] == "saved"
    assert task["admission"]["state"] == "not_reserved"
    assert task["pending_returns"] == 0
    audit = await command(service.config, "history", {"task_id": task["id"]}, None)
    assert audit["total"] == len(audit["events"])
    assert any(r["disposition"] == "coalesced" for r in audit["returns"])
    assert all(
        r["receipt"] is None for r in audit["returns"] if r["disposition"] != "intent"
    )
    # The official CLI reads all technical events and coverage through the socket.
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-B",
        "-m",
        "oraculo",
        "--config",
        str(service_config[1]),
        "task",
        "history",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate(
        json.dumps({"task_id": task["id"]}).encode()
    )
    assert process.returncode == 0, stderr.decode()
    assert json.loads(stdout)["returns"] == audit["returns"]


async def test_cached_json_in_readable_task_is_a_wire_compatibility_barrier(tmp_path):
    import json

    store = TaskStore(tmp_path / "ledger.db")
    ident = create(store)
    event = store.pending_returns(ident)[0]
    old = {"marker": "ORACULO_CONTROL_V1", "task_id": ident, "event_id": "legacy"}
    store.db.execute(
        "UPDATE task_returns SET delivery=? WHERE id=?", (json.dumps(old), event["id"])
    )
    store.update(ident, "received", {"new": True})
    host = Host()
    await engine(store, host).flush_returns(ident)
    assert len(host.calls) == 2
    assert json.loads(host.calls[0]["message"]) == old
    assert host.calls[1]["message"].startswith("**Synthetic task")
    store.close()


async def test_rejected_ack_does_not_confirm_or_lose_group(tmp_path):
    store = TaskStore(tmp_path / "ledger.db")
    ident = create(store)
    store.update(ident, "executing", {"step": 1})
    host = Host()
    call = host.call

    async def bad_ack(name, args):
        await call(name, args)
        return {"threadId": "wrong-origin", "messageId": "untrusted"}

    host.call = bad_ack
    await engine(store, host).flush_returns(ident)
    assert store.describe(ident)["pending_returns"] == 1
    assert all(row["receipt"] is None for row in store.history(ident)["returns"])
    host.call = call
    await engine(store, host).flush_returns(ident)
    assert len(host.calls) == 2 and len(host.messages) == 1
    assert store.describe(ident)["pending_returns"] == 0
    store.close()


def test_managed_schema_fences_old_writers_without_migrating_main_state(tmp_path):
    import pytest

    from oraculo.state import State, StateError

    main = State(tmp_path / "main.db")
    assert (
        main.connection.execute(
            "SELECT max(version) FROM schema_migrations"
        ).fetchone()[0]
        == 4
    )
    main.close()
    path = tmp_path / "managed.db"
    store = TaskStore(path)
    ident = create(store)
    assert (
        store.db.execute("SELECT max(version) FROM schema_migrations").fetchone()[0]
        == 5
    )
    store.close()
    # The pre-feature writer's existing version gate is still exactly max=4.
    with pytest.raises(StateError, match="state_schema_newer_than_runtime"):
        State(path)
    store = TaskStore(path)
    assert store.get(ident)["phase"] == "received"
    store.close()
