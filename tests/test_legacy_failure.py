"""Real temporary SQLite/socket/lock tests; T3 and legacy protocol are doubles.

No installed release, production daemon, vault or launchd is used. Memory evidence
is produced by the ordinary real service fixture before serving the legacy reply.
"""

import asyncio
import copy
import json
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_managed_tasks import report, start
from test_managed_tasks import system as system_fixture

from oraculo.host_ack import make_ack
from oraculo.legacy_failure import reconcile
from oraculo.maintenance import check_legacy_tasks
from oraculo.protocol import canonical_json
from oraculo.state import StateError

system = system_fixture


@contextmanager
def process_lock(path):
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import fcntl,sys; f=open(sys.argv[1],'a'); fcntl.flock(f,fcntl.LOCK_EX); print('locked',flush=True); sys.stdin.read()",
            str(path),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout.readline().strip() == "locked"
        yield process
    finally:
        process.communicate(timeout=5)
        assert process.returncode == 0


@pytest.fixture
async def legacy(system):
    task = await start(system)
    evidence = report(task)
    evidence["checks"][0]["status"] = "failed"
    system.host.complete(task["child"]["childThreadId"])
    cycle = await system.memory.finish(
        "alpha", task["parent"], task["memory"]["id"], "failed", evidence
    )
    task = system.store.get(task["id"])
    task.update(
        submitted_report=evidence,
        phase="blocked",
        blocked_reason="passing_validation_evidence_required",
    )
    with tempfile.TemporaryDirectory(prefix="ora-legacy-", dir="/tmp") as directory:
        config = system.service.config.model_copy(update={"state_dir": Path(directory)})
        path = config.state_dir / "managed-tasks.sqlite3"
        for database in [config.db_path, path]:
            with sqlite3.connect(database) as db:
                db.executescript(
                    "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY); INSERT INTO schema_migrations VALUES(3);"
                )
        with sqlite3.connect(path) as db:
            db.execute("UPDATE schema_migrations SET version=2")
            db.executescript(
                "CREATE TABLE managed_tasks(id TEXT PRIMARY KEY, origin TEXT, dedupe TEXT, request_hash TEXT, payload TEXT); CREATE TABLE task_returns(id TEXT PRIMARY KEY,task_id TEXT,payload TEXT,receipt TEXT);"
            )
            db.execute(
                "INSERT INTO managed_tasks VALUES(?,?,?,?,?)",
                (task["id"], "origin", "test", "hash", canonical_json(task)),
            )
        path.with_suffix(".lock").touch()
        state = SimpleNamespace(
            config=config,
            path=path,
            task=task,
            cycle=cycle,
            host=system.host,
            calls=[],
            mode="legacy",
            mutations={},
        )

        async def connection(reader, writer):
            envelope = json.loads(await reader.readline())
            state.calls.append(envelope)
            if envelope["kind"] == "managed.capabilities":
                if state.mode == "timeout":
                    await reader.read()
                    writer.close()
                    return
                if state.mode == "eof":
                    writer.close()
                    return
                if state.mode == "new":
                    response = {
                        "request_id": envelope["request_id"],
                        "ok": True,
                        "data": {"protocol_version": 1},
                    }
                elif state.mode == "ambiguous":
                    response = {"ok": False, "error": {"code": "OSError"}}
                else:
                    response = {
                        "request_id": "unknown",
                        "ok": False,
                        "state": "error",
                        "error": {
                            "code": "ValidationError",
                            "message": "kind [type=literal_error, input_value='managed.capabilities', input_type=str]",
                            "retryable": False,
                        },
                    }
            else:
                assert envelope["kind"] == "memory.cycle.status"
                assert envelope["origin"] == {
                    k: task["parent"][k] for k in ("thread_id", "run_id")
                }
                response = {
                    "request_id": envelope["request_id"],
                    "ok": True,
                    "data": {**copy.deepcopy(state.cycle), **state.mutations},
                }
            writer.write(canonical_json(response).encode() + b"\n")
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_unix_server(
            connection, path=str(config.socket_path)
        )
        with process_lock(config.state_dir / "daemon.lock") as daemon:
            state.daemon = daemon
            async with server:
                yield state


def ledger(legacy):
    with sqlite3.connect(legacy.path) as db:
        return json.loads(db.execute("SELECT payload FROM managed_tasks").fetchone()[0])


def rewrite(legacy, **changes):
    task = ledger(legacy)
    task.update(changes)
    with sqlite3.connect(legacy.path) as db:
        db.execute("UPDATE managed_tasks SET payload=?", (canonical_json(task),))


async def test_reconcile_preserves_schema_main_bytes_and_replays_lost_ack(legacy):
    main_bytes = legacy.config.db_path.read_bytes()
    with sqlite3.connect(legacy.path) as db:
        schema = db.execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall()
    legacy.host.fail_send_once = True
    result = await reconcile(legacy.config, {"task_id": legacy.task["id"]}, legacy.host)
    assert result["work_status"] == "failed"
    assert result["blocked_reason"] == "work_not_successful"
    assert result["reconciliation"] == "confirmed"
    assert result["pending_reconciliation_returns"] == 1
    before = ledger(legacy)
    assert (
        before["submitted_report"]
        == before["report"]
        == legacy.task["submitted_report"]
    )
    assert before["events"][:-1] == legacy.task["events"]
    replay = await reconcile(legacy.config, {"task_id": legacy.task["id"]}, legacy.host)
    assert replay["replayed"] is True
    assert replay["pending_reconciliation_returns"] == 0
    assert ledger(legacy) == before
    with sqlite3.connect(legacy.path) as db:
        assert (
            schema
            == db.execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall()
        )
        assert db.execute("SELECT max(version) FROM schema_migrations").fetchone() == (
            2,
        )
        assert db.execute("SELECT count(*) FROM task_returns").fetchone() == (1,)
    assert main_bytes == legacy.config.db_path.read_bytes()
    check_legacy_tasks(legacy.config)


async def test_legacy_negative_review_preserves_dropped_verdict_and_metadata(legacy):
    evidence = copy.deepcopy(legacy.task["submitted_report"])
    evidence.update(
        verdict="FAIL",
        reviewed_public_head="a" * 40,
        public_base="b" * 40,
        pull_request="https://github.com/example/project/pull/2",
        task_id=legacy.task["id"],
        linked_implementation_task="prior-task",
        origin={"thread_id": "origin"},
        memory={"read_state": "found"},
        findings=[{"id": "PUB-04", "severity": "P2"}],
        prior_findings=[],
        documentation_observations=[],
    )
    # The old service omits these fields; its existing canonical digest is
    # therefore unchanged. The failed check is retained and proves rejection.
    rewrite(legacy, submitted_report=evidence)
    before = legacy.config.db_path.read_bytes()
    result = await reconcile(legacy.config, {"task_id": legacy.task["id"]}, legacy.host)
    assert result["reconciliation"] == "confirmed"
    assert result["work_status"] == "failed"
    saved = ledger(legacy)
    assert saved["submitted_report"] == saved["report"] == evidence
    assert legacy.config.db_path.read_bytes() == before
    assert saved["publication"] is None


async def test_dropped_verdict_alone_cannot_authorize_legacy_recovery(legacy):
    evidence = copy.deepcopy(legacy.task["submitted_report"])
    evidence["checks"][0]["status"] = "passed"
    evidence["summary"] = "Review completed"
    evidence["verdict"] = "FAIL"
    rewrite(legacy, submitted_report=evidence)
    before = legacy.path.read_bytes()
    with pytest.raises(StateError, match="legacy_report_format_required"):
        await reconcile(legacy.config, {"task_id": legacy.task["id"]}, legacy.host)
    assert legacy.path.read_bytes() == before


@pytest.mark.parametrize(
    "mode", ["new", "ambiguous", "timeout", "eof", "offline", "schema4", "ledger4"]
)
async def test_service_failures_never_authorize_write(legacy, mode):
    legacy.mode = mode
    if mode == "offline":
        legacy.config.socket_path.unlink()
    if mode in {"schema4", "ledger4"}:
        with sqlite3.connect(
            legacy.config.db_path if mode == "schema4" else legacy.path
        ) as db:
            db.execute("INSERT INTO schema_migrations VALUES(4)")
    before = legacy.path.read_bytes()
    with pytest.raises((StateError, ValueError, OSError)):
        await reconcile(legacy.config, {"task_id": legacy.task["id"]}, legacy.host)
    assert legacy.path.read_bytes() == before


@pytest.mark.parametrize(
    "field,value",
    [
        ("work_status", "succeeded"),
        ("write_state", "pending"),
        ("identity", {}),
        ("project_id", "other"),
        ("id", "other"),
        ("report_sha256", "0" * 64),
        (
            "write_receipt",
            {"state": "confirmed", "operation_id": "other", "commit": "f" * 40},
        ),
    ],
)
async def test_mismatched_memory_blocks_without_changes(legacy, field, value):
    legacy.mutations[field] = value
    before = legacy.path.read_bytes()
    with pytest.raises(StateError):
        await reconcile(legacy.config, {"task_id": legacy.task["id"]}, legacy.host)
    assert legacy.path.read_bytes() == before


@pytest.mark.parametrize(
    "state",
    [
        "running",
        "pending_children",
        "new_run",
        "foreign_parent",
        "wrong_native_run",
        "pending_requests",
    ],
)
async def test_fresh_host_evidence_required(legacy, state):
    child = legacy.task["child"]
    thread = legacy.host.threads[child["childThreadId"]]
    if state == "running":
        thread.update(status="running", activeRunId=child["childRunId"])
    elif state == "pending_children":
        legacy.host.complete(child["childThreadId"], pending=True)
    elif state == "new_run":
        thread["latestRunId"] = "new-run"
    elif state == "foreign_parent":
        thread["parentThreadId"] = "other"
    elif state == "pending_requests":
        thread["pendingRequestCount"] = 1
    else:
        legacy.host.native_status[child["taskId"]]["childRunId"] = "wrong"
    before = legacy.path.read_bytes()
    with pytest.raises(Exception):
        await reconcile(legacy.config, {"task_id": legacy.task["id"]}, legacy.host)
    assert legacy.path.read_bytes() == before


async def test_host_change_during_memory_query_is_observed(legacy, monkeypatch):
    import oraculo.legacy_failure as module

    original = module.confirmed_cycle

    async def changed(*args):
        result = await original(*args)
        legacy.host.threads[legacy.task["child"]["childThreadId"]].update(
            status="running", activeRunId="new-run"
        )
        return result

    monkeypatch.setattr(module, "confirmed_cycle", changed)
    before = legacy.path.read_bytes()
    with pytest.raises(Exception):
        await reconcile(legacy.config, {"task_id": legacy.task["id"]}, legacy.host)
    assert legacy.path.read_bytes() == before


async def test_ledger_writer_in_other_process_blocks_reconciliation(legacy):
    before = legacy.path.read_bytes()
    with process_lock(legacy.path.with_suffix(".lock")):
        with pytest.raises(StateError, match="managed_adapter_busy"):
            await reconcile(legacy.config, {"task_id": legacy.task["id"]}, legacy.host)
    assert legacy.path.read_bytes() == before
    assert legacy.calls == []


@pytest.mark.parametrize(
    "change",
    ["reused_report", "replaced_evidence", "origin", "success", "payload_receipt"],
)
async def test_reconciliation_identity_report_and_payload_are_immutable(legacy, change):
    payload = {"task_id": legacy.task["id"]}
    if change == "reused_report":
        rewrite(legacy, report={"summary": "other"})
    elif change == "replaced_evidence":
        evidence = copy.deepcopy(legacy.task["submitted_report"])
        evidence["summary"] = "another failed report"
        rewrite(legacy, submitted_report=evidence)
    elif change == "origin":
        legacy.host.caller = legacy.task["child"]["childThreadId"]
    elif change == "success":
        rewrite(legacy, work_status="succeeded")
    else:
        payload["receipt"] = {"work_status": "failed"}
    before = legacy.path.read_bytes()
    with pytest.raises(StateError):
        await reconcile(legacy.config, payload, legacy.host)
    assert legacy.path.read_bytes() == before


async def test_socket_response_without_live_daemon_lock_is_not_authority(legacy):
    legacy.daemon.communicate(timeout=5)
    before = legacy.path.read_bytes()
    with pytest.raises(StateError, match="live_legacy_service_required"):
        await reconcile(legacy.config, {"task_id": legacy.task["id"]}, legacy.host)
    assert legacy.path.read_bytes() == before
    assert legacy.calls == []


async def test_service_replaced_during_cycle_read_fails_closed(legacy, monkeypatch):
    import oraculo.legacy_failure as module

    original = module.confirmed_cycle

    async def replaced(*args):
        result = await original(*args)
        legacy.mode = "new"
        return result

    monkeypatch.setattr(module, "confirmed_cycle", replaced)
    before = legacy.path.read_bytes()
    with pytest.raises(StateError, match="live_legacy_service_required"):
        await reconcile(legacy.config, {"task_id": legacy.task["id"]}, legacy.host)
    assert legacy.path.read_bytes() == before


async def test_sqlite_compare_and_swap_preserves_concurrent_change(legacy, monkeypatch):
    from oraculo.managed_tasks import ManagedTasks

    original = ManagedTasks._terminal_gate

    async def changed(engine, task):
        await original(engine, task)
        rewrite(legacy, blocked_reason="concurrent-observation")

    monkeypatch.setattr(ManagedTasks, "_terminal_gate", changed)
    with pytest.raises(StateError, match="legacy_task_changed_retry"):
        await reconcile(legacy.config, {"task_id": legacy.task["id"]}, legacy.host)
    assert ledger(legacy)["blocked_reason"] == "concurrent-observation"
    assert ledger(legacy)["work_status"] == "pending"
    with sqlite3.connect(legacy.path) as db:
        assert db.execute("SELECT count(*) FROM task_returns").fetchone() == (0,)


async def test_replay_still_checks_current_host_without_rewriting_record(legacy):
    await reconcile(legacy.config, {"task_id": legacy.task["id"]}, legacy.host)
    before = legacy.path.read_bytes()
    legacy.host.threads[legacy.task["child"]["childThreadId"]].update(
        status="running", activeRunId="later-run"
    )
    with pytest.raises(Exception):
        await reconcile(legacy.config, {"task_id": legacy.task["id"]}, legacy.host)
    assert legacy.path.read_bytes() == before


async def test_borrowed_cycle_cannot_authorize_another_task(legacy):
    rewrite(legacy, memory={**legacy.task["memory"], "id": "cycle-borrowed"})
    before = legacy.path.read_bytes()
    with pytest.raises(StateError, match="legacy_memory_binding_mismatch"):
        await reconcile(legacy.config, {"task_id": legacy.task["id"]}, legacy.host)
    assert legacy.path.read_bytes() == before


async def test_cli_stdio_command_reports_confirmed_failure_exit_one(legacy, tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "\n".join(
            f"{key} = {json.dumps(str(getattr(legacy.config, key)))}"
            for key in ("state_dir", "projects_root", "vault_root")
        )
    )
    payload = tmp_path / "reconcile.json"
    payload.write_text(json.dumps({"task_id": legacy.task["id"]}))
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "oraculo",
        "--config",
        str(config_path),
        "task",
        "reconcile-legacy-failure",
        "--file",
        str(payload),
        "--stdio-host",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    result = None
    try:
        async with asyncio.timeout(20):
            while line := await process.stdout.readline():
                if line == b"{\n":
                    result = json.loads(line + await process.stdout.read())
                    break
                item = json.loads(line)
                if item.get("marker") == "ORACULO_HOST_CALL_V1":
                    response = await legacy.host.call(item["tool"], item["arguments"])
                    process.stdin.write(
                        (
                            canonical_json(
                                make_ack(item, json.dumps(response).encode())
                            )
                            + "\n"
                        ).encode()
                    )
                    await process.stdin.drain()
                else:
                    result = item
            assert await process.wait() == 1, (await process.stderr.read()).decode()
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    assert result["reconciliation"] == "confirmed"
    assert result["blocked_reason"] == "work_not_successful"
    assert result["work_status"] == "failed"
    assert result["pending_reconciliation_returns"] == 0
