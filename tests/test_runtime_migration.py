from __future__ import annotations

import json
import os
import plistlib
from pathlib import Path

import pytest
from test_runtime_install import config, release

from oraculo import runtime as rt
from oraculo.config import Config
from oraculo.runtime_migration import migrate
from oraculo.service_control import ServiceController
from oraculo.state import State


@pytest.fixture
def legacy(tmp_path, monkeypatch):
    root, candidate, bin_dir = (
        tmp_path / "legacy root é",
        tmp_path / "candidate",
        tmp_path / "bin",
    )
    old = root / "releases/old"
    (old / ".venv/bin").mkdir(parents=True)
    (old / ".venv/bin/python").write_text("legacy remains unverified")
    (old / "untouched").write_text("legacy payload")
    (root / "current").symlink_to("releases/old")
    release(candidate, "new")
    conf = config(tmp_path / "config.toml")
    state = State(Config.load(conf).db_path, max_executors=3)
    state.set_capacity(3)
    state.close()
    ctl = ServiceController(
        root,
        conf,
        label="local.oraculo.test.migration",
        plist_dir=tmp_path / "agents",
        log_dir=tmp_path / "logs",
        platform="darwin",
    )
    raw = plistlib.dumps(
        dict(
            Label=ctl.label,
            ProgramArguments=[
                str(root / "current/.venv/bin/python"),
                "-m",
                "oraculo",
                "--config",
                str(conf),
                "serve",
            ],
            WorkingDirectory=str(root / "current"),
            KeepAlive=True,
        )
    )
    rt.atomic_write(ctl.plist, raw)
    monkeypatch.setattr(ctl, "inspect_launchd", lambda: dict(loaded=False, pid=None))
    monkeypatch.setattr(ctl, "alive", lambda pid: False)
    return dict(
        root=root,
        bin_dir=bin_dir,
        config_path=conf,
        controller=ctl,
        candidate_root=candidate,
        release_id="new",
        expected_legacy_pointer="releases/old",
        expected_plist_sha256=rt.digest(ctl.plist),
        previous_pid=42,
    )


def run(legacy, **kwargs):
    with rt.runtime_lock(legacy["root"]):
        return migrate(**legacy, **kwargs)


def test_offline_migration_preserves_legacy_config_capacity_and_durable_backup(legacy):
    root, ctl = legacy["root"], legacy["controller"]
    old = rt.inventory(root / "releases/old", symlinks=True)
    config_bytes = ctl.config.read_bytes()
    raw = ctl.plist_bytes()
    result = run(legacy)
    assert result["state"] == "migrated" and result["service_started"] is False
    assert result["legacy_rollback_supported"] is False
    assert rt.current_release(root) == "new"
    assert rt.inventory(root / "releases/old", symlinks=True) == old
    assert ctl.config.read_bytes() == config_bytes
    backup = Path(result["backup"])
    assert (backup / "current.link").read_text() == "releases/old"
    assert (backup / "legacy.plist").read_bytes() == raw
    assert ctl.owned_plist() == ctl.plist_value("new")
    journal = json.loads((root / "legacy-migration.json").read_text())
    service_journal = json.loads((root / "service-operation.json").read_text())
    state = State(Config.load(ctl.config).db_path, max_executors=3)
    try:
        assert state.capacity()["effective"] == state.capacity()["requested"] == 3
        assert state.get_meta("runtime_maintenance") == dict(
            operation_id=journal["operation_id"], token=journal["token"]
        )
    finally:
        state.close()
    assert service_journal["token"] == journal["token"]
    assert run(legacy)["operation_id"] == result["operation_id"]
    with pytest.raises(rt.RuntimeError, match="no_previous_release"):
        rt.activate(root, legacy["bin_dir"], ctl.config, ctl, rollback=True)


@pytest.mark.parametrize(
    "fault",
    [
        "loaded",
        "unknown",
        "pid",
        "socket",
        "pointer",
        "plist_hash",
        "foreign",
        "config",
        "candidate",
        "schema",
        "active",
        "singleton",
        "managed_lock",
        "runtime_busy",
    ],
)
def test_migration_fail_closed(legacy, monkeypatch, fault):
    import contextlib
    import fcntl

    from oraculo.service import Singleton

    root, ctl = legacy["root"], legacy["controller"]
    original_pointer = os.readlink(root / "current")
    original_plist = ctl.plist_bytes()
    stack = contextlib.ExitStack()
    try:
        if fault in {"loaded", "unknown"}:
            monkeypatch.setattr(
                ctl,
                "inspect_launchd",
                lambda: dict(loaded=True if fault == "loaded" else None, pid=None),
            )
        elif fault == "pid":
            monkeypatch.setattr(ctl, "alive", lambda pid: True)
        elif fault == "socket":
            Config.load(ctl.config).socket_path.touch()
        elif fault == "pointer":
            legacy["expected_legacy_pointer"] = "releases/foreign"
        elif fault == "plist_hash":
            legacy["expected_plist_sha256"] = "a" * 64
        elif fault == "foreign":
            raw = plistlib.dumps(dict(Label="foreign", ProgramArguments=["/bin/false"]))
            rt.atomic_write(ctl.plist, raw)
            original_plist = raw
            legacy["expected_plist_sha256"] = rt.digest(ctl.plist)
        elif fault == "config":
            ctl.config.unlink()
        elif fault == "candidate":
            (legacy["candidate_root"] / "releases/new/payload").write_text("corrupt")
        elif fault == "schema":
            path = legacy["candidate_root"] / "releases/new/manifest.json"
            value = json.loads(path.read_text())
            value["schema_max"] = 3
            rt.write_json(path, value)
        elif fault == "active":
            state = State(Config.load(ctl.config).db_path, max_executors=3)
            state.reserve_managed("fixture", "codex")
            state.close()
        elif fault == "singleton":
            stack.enter_context(Singleton(Config.load(ctl.config).state_dir))
        elif fault == "managed_lock":
            lock = stack.enter_context(
                (Config.load(ctl.config).state_dir / "managed-tasks.lock").open("a")
            )
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            stack.enter_context(rt.runtime_lock(root))
        with pytest.raises((rt.RuntimeError, OSError, ValueError)):
            run(legacy)
        assert os.readlink(root / "current") == original_pointer
        assert ctl.plist_bytes() == original_plist
        assert not (root / "releases/new").exists()
    finally:
        stack.close()


@pytest.mark.parametrize("fault", ["pointer", "plist", "launcher", "journal"])
def test_migration_resume_same_journal_after_effect(legacy, monkeypatch, fault):
    root, ctl = legacy["root"], legacy["controller"]
    original_switch, original_install = rt.switch, ctl.install
    original_write = rt.atomic_write
    triggered = False

    def crash_switch(*args):
        original_switch(*args)
        if fault == "pointer":
            raise KeyboardInterrupt()

    def crash_install(*args):
        if fault == "plist":
            raise OSError("injected failure after legacy plist archive")
        original_install(*args)

    def crash_write(path, data, mode=0o600):
        nonlocal triggered
        original_write(path, data, mode)
        if not triggered and (
            (fault == "launcher" and path == legacy["bin_dir"] / "oraculo")
            or (fault == "journal" and path == root / "service-operation.json")
        ):
            triggered = True
            raise KeyboardInterrupt()

    monkeypatch.setattr(rt, "switch", crash_switch)
    monkeypatch.setattr(ctl, "install", crash_install)
    monkeypatch.setattr(rt, "atomic_write", crash_write)
    with pytest.raises((OSError, KeyboardInterrupt)):
        run(legacy)
    journal = json.loads((root / "legacy-migration.json").read_text())
    assert journal["state"] != "complete"
    with pytest.raises(rt.RuntimeError, match="legacy_migration_pending"):
        rt.activate(root, legacy["bin_dir"], ctl.config, ctl, "new")
    monkeypatch.setattr(rt, "switch", original_switch)
    monkeypatch.setattr(ctl, "install", original_install)
    monkeypatch.setattr(rt, "atomic_write", original_write)
    result = run(legacy)
    after = json.loads((root / "legacy-migration.json").read_text())
    assert result["state"] == "migrated"
    assert (
        after["operation_id"] == journal["operation_id"]
        and after["token"] == journal["token"]
    )


def test_migration_refuses_changed_token_before_pointer_switch(legacy, monkeypatch):
    import contextlib

    from oraculo import maintenance

    root = legacy["root"]
    original = rt.switch
    monkeypatch.setattr(
        rt, "switch", lambda *args: (_ for _ in ()).throw(OSError("interrupt"))
    )
    with pytest.raises(OSError):
        run(legacy)
    journal = json.loads((root / "legacy-migration.json").read_text())
    guard = maintenance.maintenance_guard

    @contextlib.contextmanager
    def wrong_token(config, operation):
        with guard(config, operation) as observed:
            yield dict(observed, token="unrelated")

    monkeypatch.setattr(maintenance, "maintenance_guard", wrong_token)
    monkeypatch.setattr(rt, "switch", original)
    with pytest.raises(rt.RuntimeError, match="maintenance_token_mismatch"):
        run(legacy)
    assert os.readlink(root / "current") == "releases/old"
    assert (
        json.loads((root / "legacy-migration.json").read_text())["token"]
        == journal["token"]
    )


def test_migration_dry_run_is_read_only_and_cli_registered(legacy):
    root = legacy["root"]
    before = rt.inventory(root, symlinks=True)
    result = migrate(**legacy, dry_run=True)
    assert result["state"] == "planned"
    assert rt.inventory(root, symlinks=True) == before
    assert not legacy["bin_dir"].exists()
    import argparse

    parser = argparse.ArgumentParser()
    rt.register_parsers(parser.add_subparsers(dest="command"))
    args = parser.parse_args(
        [
            "runtime",
            "migrate-legacy",
            "--candidate-root",
            str(legacy["candidate_root"]),
            "--release-id",
            "new",
            "--expected-legacy-pointer",
            "releases/old",
            "--expected-plist-sha256",
            legacy["expected_plist_sha256"],
            "--previous-pid",
            "42",
        ]
    )
    assert args.runtime_action == "migrate-legacy"


def test_migration_backup_is_official_consistent_private_and_checked_on_retry(
    legacy, monkeypatch
):
    import sqlite3

    from oraculo.state import State

    original_switch, original_backup = rt.switch, State.backup
    calls = []

    def backup(self, path):
        calls.append(path)
        assert self.get_meta("runtime_maintenance") is not None
        original_backup(self, path)

    monkeypatch.setattr(State, "backup", backup)
    monkeypatch.setattr(
        rt, "switch", lambda *args: (_ for _ in ()).throw(OSError("interrupt"))
    )
    with pytest.raises(OSError):
        run(legacy)
    journal = json.loads((legacy["root"] / "legacy-migration.json").read_text())
    snapshot = Path(journal["state_backup"]["path"])
    assert calls == [snapshot]
    assert snapshot.stat().st_mode & 0o777 == 0o600
    assert rt.digest(snapshot) == journal["state_backup"]["sha256"]
    assert journal["state_backup"]["state_schema"] == 4
    with sqlite3.connect(snapshot) as copy:
        assert copy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert (
            copy.execute("SELECT max(version) FROM schema_migrations").fetchone()[0]
            == 4
        )
    snapshot.write_bytes(b"corrupt backup")
    monkeypatch.setattr(rt, "switch", original_switch)
    with pytest.raises(rt.RuntimeError, match="legacy_artifact_changed"):
        run(legacy)
    assert os.readlink(legacy["root"] / "current") == "releases/old"


def test_completed_migration_can_start_or_activate_compatible_fix(legacy):
    root, ctl = legacy["root"], legacy["controller"]
    migrated = run(legacy)
    release(root, "fix")
    result = rt.activate(root, legacy["bin_dir"], ctl.config, ctl, "fix")
    assert result["state"] == "active" and result["service_started"] is False
    assert result["operation_id"] == migrated["operation_id"]
    assert (
        json.loads((root / "service-operation.json").read_text())["state"] == "complete"
    )


def test_migration_diagnostics_redact_token_and_surface_pending_operation(
    legacy, monkeypatch
):
    monkeypatch.setattr(
        rt, "switch", lambda *args: (_ for _ in ()).throw(OSError("interrupt"))
    )
    with pytest.raises(OSError):
        run(legacy)
    journal = json.loads((legacy["root"] / "legacy-migration.json").read_text())
    observed = rt.diagnose(legacy["root"], legacy["bin_dir"], legacy["config_path"])
    assert observed["state"] == "recovery_required"
    assert observed["migration"]["operation_id"] == journal["operation_id"]
    assert journal["token"] not in json.dumps(observed)
