"""Lazy feature barrier: old main-v4 / ledger-v5 readers must refuse v6."""

import json

import pytest
from test_runtime_install import setup as runtime_setup

from oraculo import maintenance
from oraculo import runtime as rt
from oraculo.operations import Operations
from oraculo.plans import Plans
from oraculo.service import Service
from oraculo.state import State, StateError
from oraculo.task_store import TaskStore


def raw(*, outcome=False, ci=False):
    task = {
        "id": "build",
        "owner": "synthetic",
        "brief": "synthetic work",
        "criteria": [{"id": "proof", "description": "synthetic proof"}],
    }
    if outcome:
        task["criteria"][0]["outcome"] = {
            "version": 2,
            "commit": "a" * 40,
            "argv": ["synthetic"],
            "artifact": "proof.txt",
            "sha256": "b" * 64,
        }
    if ci:
        task["payload"] = {
            "ci_binding": {
                "repository": "example/repo",
                "pr": 1,
                "head": "a" * 40,
                "base": "b" * 40,
                "run": "run",
                "attempt": 1,
            }
        }
    return {"text": "Synthetic barrier", "tasks": [task]}


def state(path):
    s = State(path)
    s.register_project("p", "/tmp/synthetic")
    return s


def test_default_state_and_legacy_replay_remain_main_v4(tmp_path):
    path = tmp_path / "state.sqlite3"
    s = state(path)
    p = Plans(s)
    first = p.submit("p", raw(), "key", {"implementation": ["codex"]})
    assert s.schema_version() == 4
    s.close()
    legacy = State(path, schema_max=4)
    assert (
        Plans(legacy).submit("p", raw(), "key", {"implementation": ["codex"]})[
            "objective_id"
        ]
        == first["objective_id"]
    )
    assert legacy.schema_version() == 4
    legacy.close()


@pytest.mark.parametrize("feature", ["outcome", "ci"])
def test_new_plan_semantics_promote_before_persist_and_old_reader_refuses(
    tmp_path, feature
):
    path = tmp_path / "state.sqlite3"
    s = state(path)
    Plans(s).submit("p", raw(**{feature: True}), "key", {"implementation": ["codex"]})
    assert s.schema_version() == 6
    assert maintenance.status(s)["state_schema"] == 6
    s.close()
    with pytest.raises(StateError, match="state_schema_newer_than_runtime"):
        State(path, schema_max=4)
    modern = State(path)
    assert modern.schema_version() == 6 and len(modern.list_jobs()) == 1
    modern.close()


@pytest.mark.parametrize("feature", ["outcome", "ci"])
def test_old_writer_ceiling_cannot_write_new_semantics_or_partially_migrate(
    tmp_path, feature
):
    s = State(tmp_path / "state", schema_max=4)
    s.register_project("p", "/tmp/synthetic")
    with pytest.raises(StateError, match="orchestration_schema_unsupported"):
        Plans(s).submit(
            "p", raw(**{feature: True}), "key", {"implementation": ["codex"]}
        )
    assert s.schema_version() == 4 and s.list_jobs() == [] and s.list_objectives() == []
    s.close()


@pytest.mark.parametrize("kind", ["validation", "research"])
def test_new_managed_deliverables_promote_only_the_ledger_and_prevent_old_v5_reader(
    tmp_path, kind
):
    path = tmp_path / "managed-tasks.sqlite3"
    store = TaskStore(path)
    assert store.state.schema_version() == 5
    task = store.create({"thread_id": "origin"}, "key", {"deliverable_kind": kind})
    assert store.state.schema_version() == 6
    store.close()
    with pytest.raises(StateError):
        TaskStore(path, read_only=True, schema_max=5)
    with pytest.raises(StateError):
        State(path, schema_max=5)
    reader = TaskStore(path, read_only=True)
    assert reader.get(task["id"])["request"]["deliverable_kind"] == kind
    reader.close()
    main = State(tmp_path / "state.sqlite3")
    guard = maintenance.status(main)
    assert (
        guard["main_schema"] == 4
        and guard["managed_schema"] == guard["state_schema"] == 6
    )
    main.close()


def test_legacy_managed_ledger_stays_readable_by_v5_ceiling(tmp_path):
    path = tmp_path / "managed-tasks.sqlite3"
    store = TaskStore(path)
    t = store.create(
        {"thread_id": "origin"}, "key", {"deliverable_kind": "implementation"}
    )
    store.close()
    legacy = TaskStore(path, read_only=True, schema_max=5)
    assert legacy.get(t["id"])["request"]["deliverable_kind"] == "implementation"
    legacy.close()


def test_new_rollout_operation_promotes_before_intent_and_old_reader_cannot_confirm(
    tmp_path,
):
    path = tmp_path / "state.sqlite3"
    s = state(path)
    obj = s.submit_objective("p", "rollout")
    job = s.enqueue_job(obj["id"], "implementation", {})
    a = s.acquire_grant(job["id"], "codex", "m", "owner", "run")
    expected = {
        "commit": "b" * 40,
        "target": "synthetic",
        "artifact": "c" * 64,
        "rollout": {
            "version": 1,
            "source_commit": "a" * 40,
            "merge_sha": "b" * 40,
            "release_id": "synthetic",
            "artifact_sha256": "c" * 64,
            "environment": "synthetic",
        },
    }
    Operations(s).prepare(
        "op",
        a["id"],
        "deploy",
        expected,
        generation=a["generation"],
        checks_passed=True,
    )
    assert s.schema_version() == 6
    s.close()
    with pytest.raises(StateError):
        State(path, schema_max=4)


def test_future_schema_is_refused_by_modern_writer_and_readers(tmp_path):
    path = tmp_path / "state.sqlite3"
    s = State(path)
    s.connection.execute("INSERT INTO schema_migrations VALUES(8)")
    s.close()
    with pytest.raises(StateError):
        State(path)
    with pytest.raises(StateError):
        State.peek_schema(path)


def test_installed_manifest_ceiling_blocks_new_features_without_migrating_state(
    service_config, tmp_path, monkeypatch
):
    import sys

    config, path = service_config
    release = tmp_path / "installed"
    venv = release / "venv"
    venv.mkdir(parents=True)
    (venv / "bin").mkdir()
    interpreter = venv / "bin/python"
    interpreter.write_bytes(b"laboratory interpreter")
    (release / "manifest.json").write_text(
        json.dumps(
            dict(
                release_id="laboratory",
                schema_version=1,
                python="3.12",
                source_commit="a" * 40,
                schema_min=4,
                schema_max=4,
                interpreter_sha256=rt.digest(interpreter),
                files=rt.inventory(release),
            )
        )
    )
    monkeypatch.setattr(sys, "prefix", str(venv))
    s = Service(config, path)
    s.state.register_project("p", str(config.projects_root / "alpha"))
    try:
        assert s.state.schema_max == 4
        with pytest.raises(StateError):
            s.plans.submit("p", raw(outcome=True), "key", config.profiles)
        assert s.state.schema_version() == 4
    finally:
        s.state.close()


setup = runtime_setup


def test_prepare_and_rollback_respect_persisted_v6_barrier(setup):
    root, bin_dir, conf, controller = setup
    from oraculo.config import Config

    config = Config.load(conf)
    s = State(config.db_path)
    s.require_orchestration_schema()
    s.close()
    bundle = root.parent / "bundle"
    bundle.mkdir()
    (bundle / "oraculo-0.1.0-py3-none-any.whl").write_bytes(b"synthetic wheel")
    (bundle / "requirements.txt").write_text("# fixture\n")
    rt.create_manifest(bundle, "candidate", "a" * 40, 1, 4)
    with pytest.raises(rt.RuntimeError, match="schema_incompatible"):
        rt.prepare(root, bin_dir, bundle, dry_run=True, config_path=conf)
    rt.create_manifest(bundle, "candidate", "a" * 40, 1, 6)
    assert (
        rt.prepare(root, bin_dir, bundle, dry_run=True, config_path=conf)["state"]
        == "planned"
    )
    manifest = json.loads((root / "releases/new/manifest.json").read_text())
    manifest["schema_max"] = 6
    rt.write_json(root / "releases/new/manifest.json", manifest)
    controller.guard["state_schema"] = State.required_schema(config.state_dir)
    assert rt.activate(root, bin_dir, conf, controller, "new")["state"] == "active"
    before = list(controller.steps)
    with pytest.raises(rt.RuntimeError, match="schema_incompatible"):
        rt.activate(root, bin_dir, conf, controller, rollback=True)
    assert controller.steps[len(before) :] == ["maintenance.begin"]
    assert rt.current_release(root) == "new" and State.peek_schema(config.db_path) == 6
