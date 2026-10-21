from __future__ import annotations

import contextlib
import json
import signal
import subprocess
import sys
import types
from pathlib import Path

import pytest

from oraculo import runtime as rt


def release(root, name, schema_max=4):
    target = root / "releases" / name
    (target / "venv/bin").mkdir(parents=True)
    (target / "venv/bin/python").symlink_to(sys.executable)
    (target / "payload").write_text(name)
    rt.write_json(
        target / "manifest.json",
        dict(
            schema_version=1,
            release_id=name,
            source_commit="a" * 40,
            python="3.12",
            schema_min=1,
            schema_max=schema_max,
            interpreter_sha256=rt.digest(Path(sys.executable)),
            files=rt.inventory(target, symlinks=True),
        ),
    )
    return target


def config(path):
    path.write_text(
        f'projects_root = "{path.parent}/projects"\nvault_root = "{path.parent}/vault"\nstate_dir = "{path.parent}/state"\nmax_executors = 3\n'
    )
    return path


class Controller:
    supported = True

    def __init__(self, root, config, running=True):
        self.root, self.config, self.running = root, config, running
        self.steps = []
        self.failure = None
        self.on_stop = None
        self.guard = dict(
            quiescent=True,
            admission_closed=True,
            state_schema=4,
            config_schema=1,
            capacity={"effective": 3},
            token="synthetic",
        )

    def status(self):
        return {
            "state": "online" if self.running else "unloaded",
            "pid": 123 if self.running else None,
        }

    def plist_bytes(self):
        return b"<plist>synthetic</plist>"

    def request(self, kind, payload):
        self.steps.append(kind)
        if kind == "maintenance.begin":
            self.guard["operation_id"] = payload["operation_id"]
        return self.guard

    def stop(self):
        self.steps.append("stop")
        if self.failure == "stop":
            raise rt.RuntimeError("stop_timeout")
        self.running = False
        if self.on_stop:
            self.on_stop()

    def install(self, release_id):
        self.steps.append("install:" + release_id)
        if self.failure == "install":
            raise rt.RuntimeError("plist_failed")

    def recovery_identity(self, release_id):
        return {"release_id": release_id}

    def start(self, release_id=None, previous_pid=None):
        self.steps.append("start:" + release_id)
        if self.failure == "start":
            raise rt.RuntimeError("start_timeout")
        self.running = True


@pytest.fixture
def setup(tmp_path, monkeypatch):
    root = tmp_path / "runtime é spaces"
    bin_dir = tmp_path / "bin spaces"
    conf = config(tmp_path / "config.toml")
    release(root, "old")
    release(root, "new")
    rt.switch(root, "old")
    controller = Controller(root, conf)

    @contextlib.contextmanager
    def guard(config, operation_id):
        controller.guard["operation_id"] = operation_id
        yield controller.guard

    monkeypatch.setitem(
        sys.modules,
        "oraculo.maintenance",
        types.SimpleNamespace(
            maintenance_guard=guard,
            end_maintenance=lambda *args: controller.steps.append("offline_end"),
        ),
    )
    return root, bin_dir, conf, controller


def test_activation_round_trip_and_capacity(setup):
    root, bin_dir, conf, controller = setup
    assert rt.activate(root, bin_dir, conf, controller, "new")["state"] == "active"
    assert rt.current_release(root) == "new"
    assert controller.steps == [
        "maintenance.begin",
        "stop",
        "install:new",
        "start:new",
        "maintenance.status",
        "maintenance.end",
    ]
    assert (
        rt.activate(root, bin_dir, conf, controller, rollback=True)["state"] == "active"
    )
    assert rt.current_release(root) == "old"
    assert json.loads((root / "operation.json").read_text())["capacity"] == {
        "effective": 3
    }


@pytest.mark.parametrize(
    "failure,selected", [("stop", "old"), ("install", "new"), ("start", "new")]
)
def test_failure_journal_and_rollback(setup, failure, selected):
    root, bin_dir, conf, controller = setup
    controller.failure = failure
    with pytest.raises(rt.RuntimeError):
        rt.activate(root, bin_dir, conf, controller, "new")
    journal = json.loads((root / "operation.json").read_text())
    assert journal["state"] == "recovery_required"
    assert rt.current_release(root) == selected
    with pytest.raises(rt.RuntimeError, match="recovery_required"):
        rt.activate(root, bin_dir, conf, controller, "unrelated")
    controller.failure = None
    rt.activate(root, bin_dir, conf, controller, rollback=True)
    assert rt.current_release(root) == "old"


def test_crash_after_pointer_before_receipt_recovers(setup, monkeypatch):
    root, bin_dir, conf, controller = setup
    original = rt.switch

    def crash(root, target):
        original(root, target)
        raise KeyboardInterrupt()

    monkeypatch.setattr(rt, "switch", crash)
    with pytest.raises(KeyboardInterrupt):
        rt.activate(root, bin_dir, conf, controller, "new")
    assert json.loads((root / "operation.json").read_text())["state"] == "switching"
    monkeypatch.setattr(rt, "switch", original)
    rt.activate(root, bin_dir, conf, controller, rollback=True)
    assert rt.current_release(root) == "old"


def test_crash_before_begin_retains_operation_id(setup, monkeypatch):
    root, bin_dir, conf, controller = setup

    def crash(*args):
        raise KeyboardInterrupt()

    monkeypatch.setattr(controller, "request", crash)
    with pytest.raises(KeyboardInterrupt):
        rt.activate(root, bin_dir, conf, controller, "new")
    journal = json.loads((root / "operation.json").read_text())
    assert journal["state"] == "acquiring_maintenance"
    assert journal["operation_id"]
    assert journal["capacity"] is None
    assert rt.current_release(root) == "old"
    # Recovery must adopt the first observed capacity instead of comparing
    # the restarted service against the never-recorded None.
    monkeypatch.undo()
    result = rt.activate(root, bin_dir, conf, controller, "new")
    assert result["state"] == "active"
    journal = json.loads((root / "operation.json").read_text())
    assert journal["state"] == "complete"
    assert journal["operation_id"] == result["operation_id"]
    assert journal["capacity"] == {"effective": 3}
    assert rt.current_release(root) == "new"


def test_config_race_preserves_stopped_state(setup):
    root, bin_dir, conf, controller = setup
    controller.on_stop = lambda: conf.write_text(
        conf.read_text() + "\npoll_idle_seconds=7\n"
    )
    with pytest.raises(rt.RuntimeError, match="config_changed"):
        rt.activate(root, bin_dir, conf, controller, "new")
    assert rt.current_release(root) == "old"
    assert not controller.running
    with pytest.raises(rt.RuntimeError, match="config_changed"):
        rt.activate(root, bin_dir, conf, controller, rollback=True)
    assert "maintenance.end" not in controller.steps


@pytest.mark.parametrize(
    "guard",
    [
        dict(quiescent=False),
        dict(admission_closed=False),
        dict(state_schema=5),
        dict(token=None),
    ],
)
def test_unknown_active_or_schema_blocks_before_stop(setup, guard):
    root, bin_dir, conf, controller = setup
    controller.guard.update(guard)
    with pytest.raises(rt.RuntimeError):
        rt.activate(root, bin_dir, conf, controller, "new")
    assert rt.current_release(root) == "old"
    assert "stop" not in controller.steps
    assert (root / "operation.json").exists()


def test_rollback_cannot_read_new_schema(setup):
    root, bin_dir, conf, controller = setup
    old = root / "releases/old/manifest.json"
    value = json.loads(old.read_text())
    value["schema_max"] = 2
    rt.write_json(old, value)
    rt.activate(root, bin_dir, conf, controller, "new")
    with pytest.raises(rt.RuntimeError, match="schema_incompatible"):
        rt.activate(root, bin_dir, conf, controller, rollback=True)
    assert rt.current_release(root) == "new"


def test_recovery_failure_is_retained(setup):
    root, bin_dir, conf, controller = setup
    controller.failure = "start"
    with pytest.raises(rt.RuntimeError):
        rt.activate(root, bin_dir, conf, controller, "new")
    controller.failure = "install"
    with pytest.raises(rt.RuntimeError):
        rt.activate(root, bin_dir, conf, controller, rollback=True)
    assert (
        json.loads((root / "operation.json").read_text())["state"]
        == "recovery_required"
    )
    assert not controller.running


def test_fresh_selection_without_config_never_starts(tmp_path):
    root = tmp_path / "root"
    release(root, "new")
    controller = Controller(root, tmp_path / "missing", running=False)
    rt.activate(root, tmp_path / "bin", tmp_path / "missing", controller, "new")
    assert controller.steps == ["install:new"]
    assert rt.current_release(root) == "new"


def test_readonly_diagnose_and_dry_run(setup):
    root, bin_dir, conf, controller = setup
    before = rt.inventory(root, symlinks=True)
    result = rt.activate(root, bin_dir, conf, controller, "new", dry_run=True)
    assert result["state"] == "planned"
    assert rt.inventory(root, symlinks=True) == before
    assert not bin_dir.exists()
    assert (
        rt.diagnose(root, bin_dir, config=conf.parent / "missing")["config"]
        == "missing"
    )
    assert not controller.steps


@pytest.mark.parametrize(
    "kind", ["extra", "modified", "manifest_symlink", "python_changed"]
)
def test_corrupt_release_detected(tmp_path, kind):
    target = release(tmp_path, "test")
    if kind == "extra":
        (target / "extra.py").write_text("surprise")
    elif kind == "modified":
        (target / "payload").write_text("changed")
    elif kind == "manifest_symlink":
        raw = (target / "manifest.json").read_bytes()
        (tmp_path / "manifest").write_bytes(raw)
        (target / "manifest.json").unlink()
        (target / "manifest.json").symlink_to(tmp_path / "manifest")
    else:
        value = json.loads((target / "manifest.json").read_text())
        value["interpreter_sha256"] = "0" * 64
        rt.write_json(target / "manifest.json", value)
    with pytest.raises(rt.RuntimeError):
        rt.verify(target, installed=True)


def test_foreign_launcher_and_bad_current_are_preserved(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    launcher = bin_dir / "oraculo"
    launcher.write_text("foreign")
    with pytest.raises(rt.RuntimeError, match="launcher_conflict"):
        rt.check_launcher(tmp_path, bin_dir)
    assert launcher.read_text() == "foreign"
    (tmp_path / "current").symlink_to("/missing")
    with pytest.raises(rt.RuntimeError, match="current_conflict"):
        rt.current_release(tmp_path)


def test_launcher_exec_preserves_args_stdin_exit_and_signal(tmp_path):
    root = tmp_path / "root ü"
    executable = root / "current/venv/bin/python"
    executable.parent.mkdir(parents=True)
    executable.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\ncat\nexit 17\n')
    executable.chmod(0o755)
    launcher = tmp_path / "oraculo"
    rt.atomic_write(launcher, rt.launcher_bytes(root), 0o755)
    result = subprocess.run(
        [str(launcher), "hello world", "$literal"],
        input="stdin\n",
        capture_output=True,
        text=True,
    )
    assert result.returncode == 17
    assert result.stdout == "-B\n-m\noraculo\nhello world\n$literal\nstdin\n"
    executable.write_text("#!/bin/sh\nkill -TERM $$\n")
    assert subprocess.run([str(launcher)]).returncode == -signal.SIGTERM


def test_runtime_lock_rejects_concurrent_mutation(tmp_path):
    with rt.runtime_lock(tmp_path):
        with pytest.raises(rt.RuntimeError, match="runtime_busy"):
            with rt.runtime_lock(tmp_path):
                pytest.fail("second writer admitted")


def test_source_distribution_allowlist_excludes_private(tmp_path):
    import tarfile
    import zipfile

    subprocess.run(
        ["uv", "build", "--out-dir", str(tmp_path)], check=True, capture_output=True
    )
    with tarfile.open(next(tmp_path.glob("*.tar.gz"))) as source:
        names = source.getnames()
        assert not any(
            "/docs/tasks/" in n or "/docs/contexto/" in n or "/.claude/" in n
            for n in names
        )
        assert any(n.endswith("/uv.lock") for n in names)
    with zipfile.ZipFile(next(tmp_path.glob("*.whl"))) as wheel:
        names = wheel.namelist()
        for suffix in [
            "assets/skills/oraculo/SKILL.md",
            "assets/templates/integration/AGENTS.md",
            "assets/config/oraculo.example.toml",
            "assets/prompts/managed-origin.md",
        ]:
            assert "oraculo/" + suffix in names
        assert not any("docs/tasks" in n or "managed-contexts" in n for n in names)
        for name in names:
            assert b"/Users/" not in wheel.read(name), name


def test_offline_guard_real_state_and_capacity(tmp_path):
    from oraculo.config import Config
    from oraculo.state import State

    root = tmp_path / "runtime"
    release(root, "one")
    release(root, "two")
    rt.switch(root, "one")
    conf = config(tmp_path / "config.toml")
    parsed = Config.load(conf)
    state = State(parsed.db_path, max_executors=3)
    capacity = state.capacity()
    state.close()
    controller = Controller(root, conf, running=False)
    rt.activate(root, tmp_path / "bin", conf, controller, "two")
    after = State(parsed.db_path, max_executors=3)
    try:
        assert after.capacity() == capacity
        assert after.get_meta("runtime_maintenance") is None
    finally:
        after.close()
    assert rt.current_release(root) == "two"
    assert controller.steps == ["install:two"]


def test_first_selection_crash_can_resume(tmp_path):
    root = tmp_path / "runtime"
    release(root, "one")
    controller = Controller(root, tmp_path / "absent", running=False)
    controller.failure = "install"
    with pytest.raises(rt.RuntimeError):
        rt.activate(root, tmp_path / "bin", tmp_path / "absent", controller, "one")
    original_operation = json.loads((root / "operation.json").read_text())[
        "operation_id"
    ]
    controller.failure = None
    result = rt.activate(root, tmp_path / "bin", tmp_path / "absent", controller, "one")
    assert result["operation_id"] == original_operation
    assert result["state"] == "active"


def test_prepare_dry_run_and_unavailable_dependencies(tmp_path, monkeypatch):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "oraculo-0.1.0-py3-none-any.whl").write_bytes(b"synthetic artifact")
    (bundle / "requirements.txt").write_text("oraculo==0.1.0 --hash=sha256:" + "0" * 64)
    rt.create_manifest(bundle, "r1", "b" * 40, 4, 4)
    root, bin_dir = tmp_path / "root", tmp_path / "bin"
    assert rt.prepare(root, bin_dir, bundle, dry_run=True)["state"] == "planned"
    assert not root.exists() and not bin_dir.exists()
    monkeypatch.setattr(rt.shutil, "which", lambda _: None)
    with pytest.raises(rt.RuntimeError, match="uv_missing"):
        rt.prepare(root, bin_dir, bundle)
    assert not (root / "current").exists()
    monkeypatch.setattr(rt.sys, "version_info", (3, 11))
    with pytest.raises(rt.RuntimeError, match="python_incompatible"):
        rt.prepare(root, bin_dir, bundle)


def test_corrupt_bundle_and_path_traversal_inventory_rejected(tmp_path):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "oraculo-0.1.0-py3-none-any.whl").write_bytes(b"wheel")
    (bundle / "requirements.txt").write_text("locked")
    rt.create_manifest(bundle, "r1", "b" * 40, 4, 4)
    value = json.loads((bundle / "manifest.json").read_text())
    value["files"]["../foreign"] = "0" * 64
    rt.write_json(bundle / "manifest.json", value)
    with pytest.raises(rt.RuntimeError, match="release_corrupt"):
        rt.verify(bundle)
    assert not (tmp_path / "foreign").exists()


@pytest.mark.parametrize("field,bad", [("token", "wrong"), ("operation_id", "other")])
def test_recovery_guard_mismatch_preserves_pointer_and_original_token(
    setup, monkeypatch, field, bad
):
    root, bin_dir, conf, controller = setup
    controller.failure = "start"
    with pytest.raises(rt.RuntimeError, match="start_timeout"):
        rt.activate(root, bin_dir, conf, controller, "new")
    journal = json.loads((root / "operation.json").read_text())
    controller.failure = None

    @contextlib.contextmanager
    def guard(config, operation_id):
        yield dict(controller.guard, **{field: bad})

    monkeypatch.setattr(sys.modules["oraculo.maintenance"], "maintenance_guard", guard)
    with pytest.raises(
        rt.RuntimeError, match="maintenance_token_mismatch|maintenance_correlation"
    ):
        rt.activate(root, bin_dir, conf, controller, rollback=True)
    assert rt.current_release(root) == "new"
    assert (
        json.loads((root / "operation.json").read_text())["token"] == journal["token"]
    )


def test_runtime_lock_does_not_follow_foreign_symlink(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    foreign = tmp_path / "foreign"
    foreign.write_text("untouched")
    (root / ".runtime.lock").symlink_to(foreign)
    with pytest.raises(OSError):
        with rt.runtime_lock(root):
            pytest.fail("followed foreign lock")
    assert foreign.read_text() == "untouched"
