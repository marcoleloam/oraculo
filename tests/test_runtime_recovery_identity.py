import json
import sys
from pathlib import Path

import pytest
from test_runtime_install import Controller, config, release
from test_runtime_migration import legacy as legacy_fixture

from oraculo import runtime as rt
from oraculo.config import Config
from oraculo.runtime_migration import migrate
from oraculo.state import State

legacy = legacy_fixture


def test_migration_config_loaded_bytes_must_match_guard(legacy, monkeypatch):
    path = legacy["config_path"]
    original = Config.load
    old = original(path)
    new_dir = path.parent / "other-state"
    changed = old.model_copy(update={"state_dir": new_dir})
    state = State(changed.db_path, max_executors=3)
    state.reserve_managed("active-external", "codex")
    state.close()
    once = False

    def racing_load(cls, p):
        nonlocal once
        result = original(p)
        if Path(p) == path and not once:
            once = True
            path.write_text(path.read_text().replace(str(old.state_dir), str(new_dir)))
        return result

    monkeypatch.setattr(Config, "load", classmethod(racing_load))
    with rt.runtime_lock(legacy["root"]):
        with pytest.raises(rt.RuntimeError, match="config_changed"):
            migrate(**legacy)
    assert (legacy["root"] / "current").readlink() == Path("releases/old")
    assert not (legacy["root"] / "legacy-migration.json").exists()
    state = State(changed.db_path, max_executors=3)
    try:
        assert state.capacity()["occupied"] == 1
        assert state.get_meta("runtime_maintenance") is None
    finally:
        state.close()


def test_activation_retries_after_maintenance_end_before_complete(
    tmp_path, monkeypatch
):
    root, bins = tmp_path / "runtime", tmp_path / "bin"
    conf = config(tmp_path / "config.toml")
    release(root, "old")
    release(root, "new")
    rt.switch(root, "old")
    state = State(Config.load(conf).db_path, max_executors=3)
    state.close()
    ctl = Controller(root, conf, running=False)
    write = rt.write_json
    fired = False

    def fail_completion(path, value):
        nonlocal fired
        if (
            path == root / "operation.json"
            and value.get("state") == "complete"
            and not fired
        ):
            fired = True
            raise OSError(
                "injected crash after durable maintenance.end before complete journal"
            )
        return write(path, value)

    monkeypatch.setattr(rt, "write_json", fail_completion)
    with pytest.raises(OSError):
        rt.activate(root, bins, conf, ctl, "new")
    before = json.loads((root / "operation.json").read_text())
    state = State(Config.load(conf).db_path, max_executors=3)
    assert state.get_meta("runtime_maintenance") is None
    state.close()
    monkeypatch.setattr(rt, "write_json", write)
    result = rt.activate(root, bins, conf, ctl, "new")
    assert result["state"] == "active"
    after = json.loads((root / "operation.json").read_text())
    assert after["state"] == "complete" and after["token"] == before["token"]


@pytest.mark.parametrize("action", ["list", "status", "history"])
def test_offline_status_does_not_migrate_legacy_ledger_under_writer_lock(
    tmp_path, action
):
    import fcntl
    import sqlite3
    import subprocess

    from oraculo.task_store import TaskStore

    conf = config(tmp_path / "config.toml")
    loaded = Config.load(conf)
    path = loaded.state_dir / "managed-tasks.sqlite3"
    store = TaskStore(path)
    task = store.create({"thread_id": "fixture-origin"}, "fixture-task", {})
    store.db.execute("DROP TABLE task_notifications")
    store.db.execute("ALTER TABLE task_returns DROP COLUMN delivery")
    store.db.execute("DELETE FROM schema_migrations WHERE version>=4")
    store.db.execute("INSERT OR IGNORE INTO schema_migrations VALUES(3)")
    store.close()
    before = path.read_bytes()
    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "oraculo",
                "--config",
                str(conf),
                "task",
                action,
            ],
            input=json.dumps({"task_id": task["id"]}),
            capture_output=True,
            text=True,
        )
    db = sqlite3.connect(path)
    try:
        version = db.execute("SELECT max(version) FROM schema_migrations").fetchone()[0]
        columns = [r[1] for r in db.execute("PRAGMA table_info(task_returns)")]
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["source"] == "local_ledger"
    finally:
        db.close()
    assert version == 3 and "delivery" not in columns and path.read_bytes() == before


def test_service_start_retries_after_maintenance_end_journal_fault(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace

    from oraculo import maintenance
    from oraculo import service_control as sc

    root = tmp_path / "runtime"
    conf = config(tmp_path / "config.toml")
    release(root, "one")
    rt.switch(root, "one")
    state = State(Config.load(conf).db_path, max_executors=3)
    guard = maintenance.begin(state, "start-operation")
    journal_path = root / "service-operation.json"
    rt.write_json(
        journal_path,
        dict(
            operation_id=guard["operation_id"],
            token=guard["token"],
            state="stopped",
            config_sha256=rt.config_hash(conf),
        ),
    )

    class OnlineController:
        def __init__(self):
            self.root = root
            self.config = conf

        def require_supported(self):
            pass

        def status(self):
            return {"state": "online"}

        def recovery_identity(self, selected):
            return {"release_id": selected}

        def start(self, previous_pid=None):
            return {"state": "online"}

        def request(self, kind, payload):
            if kind == "maintenance.begin":
                return maintenance.begin(state, payload["operation_id"])
            if kind == "maintenance.end":
                return maintenance.end(state, payload["operation_id"], payload["token"])
            assert kind == "maintenance.status"
            return maintenance.status(state)

    monkeypatch.setattr(
        sc.ServiceController, "from_args", lambda args: OnlineController()
    )
    original = sc.write_json

    def fail_completion(path, value):
        if value.get("state") == "complete":
            raise OSError("completion journal fault")
        original(path, value)

    args = SimpleNamespace(service_action="start", dry_run=False)
    monkeypatch.setattr(sc, "write_json", fail_completion)
    try:
        failed = sc.dispatch(args)
        assert failed["state"] == "failed"
        assert state.get_meta("runtime_maintenance") is None
        monkeypatch.setattr(sc, "write_json", original)
        assert sc.dispatch(args)["state"] == "online"
        final = json.loads(journal_path.read_text())
        assert final["state"] == "complete" and final["token"] == guard["token"]
    finally:
        state.close()


def test_offline_ledger_missing_and_future_schema_never_created_or_migrated(tmp_path):
    import sqlite3

    from oraculo.state import StateError
    from oraculo.task_store import TaskStore

    path = tmp_path / "absent" / "ledger.sqlite3"
    with pytest.raises(StateError, match="read_unavailable"):
        TaskStore(path, read_only=True)
    assert not path.parent.exists()
    path.parent.mkdir()
    store = TaskStore(path)
    store.db.execute("INSERT INTO schema_migrations VALUES(99)")
    store.close()
    before = path.read_bytes()
    with pytest.raises(StateError, match="read_unavailable"):
        TaskStore(path, read_only=True)
    assert path.read_bytes() == before
    connection = sqlite3.connect(path)
    connection.execute("DELETE FROM schema_migrations WHERE version=99")
    connection.commit()
    connection.close()
    store = TaskStore(path, read_only=True)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            store.db.execute("DELETE FROM managed_tasks")
    finally:
        store.close()


@pytest.mark.parametrize("fault", ["completion_write", "end_ack"])
def test_online_activation_recovers_completed_maintenance(tmp_path, monkeypatch, fault):
    from oraculo import maintenance

    root, bins = tmp_path / "runtime", tmp_path / "bin"
    conf = config(tmp_path / "config.toml")
    release(root, "old")
    release(root, "new")
    rt.switch(root, "old")
    state = State(Config.load(conf).db_path, max_executors=3)
    ctl = Controller(root, conf)
    fired = False

    def request(kind, payload):
        nonlocal fired
        if kind == "maintenance.begin":
            return maintenance.begin(state, payload["operation_id"])
        if kind == "maintenance.end":
            result = maintenance.end(state, payload["operation_id"], payload["token"])
            if fault == "end_ack" and not fired:
                fired = True
                raise OSError("lost successful maintenance.end ACK")
            return result
        assert kind == "maintenance.status"
        return maintenance.status(state)

    ctl.request = request
    original = rt.write_json

    def write(path, value):
        nonlocal fired
        if (
            fault == "completion_write"
            and value.get("state") == "complete"
            and not fired
        ):
            fired = True
            raise OSError("completion journal fault")
        original(path, value)

    monkeypatch.setattr(rt, "write_json", write)
    try:
        with pytest.raises(OSError):
            rt.activate(root, bins, conf, ctl, "new")
        token = json.loads((root / "operation.json").read_text())["token"]
        assert state.get_meta("runtime_maintenance") is None
        assert rt.activate(root, bins, conf, ctl, "new")["state"] == "active"
        final = json.loads((root / "operation.json").read_text())
        assert final["state"] == "complete" and final["token"] == token
        assert state.get_meta("runtime_maintenance") is None
    finally:
        state.close()
