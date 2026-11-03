import asyncio
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from oraculo.cli import main, make_envelope, request
from oraculo.config import Config
from oraculo.managed_tasks import ManagedTasks
from oraculo.memory.schema import MemoryError
from oraculo.project_registration import (
    LOCK_NAME,
    POLICY_NAME,
    ProjectRegistrationError,
    apply_registration,
    confirmation_for,
    content_hash,
    empty_policy_sha256,
    plan_registration,
)
from oraculo.protocol import MAX_ENVELOPE_BYTES, Envelope
from oraculo.service import Service
from oraculo.state import StateError

ALPHA = "proj-alpha"
BETA = "proj-beta"
GAMMA = "proj-gamma"


def write_config(tmp_path: Path, *, allow=("alpha",), executors: int = 33):
    root = tmp_path / "projects"
    alpha = root / "alpha"
    beta = root / "beta"
    gamma = root / "gamma"
    alpha.mkdir(parents=True)
    beta.mkdir()
    gamma.mkdir()
    vault = tmp_path / "vault"
    state = tmp_path / "state"
    vault.mkdir()
    state.mkdir()
    lines = [
        f"projects_root = {json.dumps(str(root))}",
        f"vault_root = {json.dumps(str(vault))}",
        f"state_dir = {json.dumps(str(state))}",
        f"max_executors = {executors}",
    ]
    if allow is not None:
        mapping = {"alpha": alpha, "beta": beta, "gamma": gamma}
        rendered = ", ".join(json.dumps(str(mapping[name])) for name in allow)
        lines.append(f"allowed_projects = [{rendered}]")
    path = tmp_path / "config.toml"
    path.write_text("\n".join(lines) + "\n")
    return Config.load(path), path, root, alpha, beta, gamma


def catalog(*items: tuple[str, Path], deleted: str | None = None) -> list[dict]:
    rows = [
        {"id": project_id, "workspaceRoot": str(path)} for project_id, path in items
    ]
    if deleted:
        for row in rows:
            if row["id"] == deleted:
                row["deletedAt"] = "2026-01-01T00:00:00Z"
    return rows


def plan(config, path, project_id, directory):
    return plan_registration(
        config,
        catalog((project_id, directory)),
        project_id,
        str(directory),
        config_path=path,
    )


def apply(config, config_path, project_id, directory, confirmation, rows=None):
    return apply_registration(
        config,
        rows if rows is not None else catalog((project_id, directory)),
        project_id,
        str(directory),
        confirmation,
        config_path=config_path,
    )


def test_plan_does_not_publish_and_apply_is_exact(tmp_path):
    config, path, _root, alpha, beta, _gamma = write_config(tmp_path, allow=())
    before = path.read_bytes()
    prepared = plan(config, path, BETA, beta)
    assert prepared["state"] == "confirmation_required"
    assert prepared["publishes"] is False
    assert prepared["path"] == str(beta.resolve())
    assert not (config.state_dir / POLICY_NAME).exists()
    assert not (config.state_dir / LOCK_NAME).exists()
    published = apply(config, path, BETA, beta, prepared["confirmation"])
    assert published["state"] == "published"
    assert published["changed"] is True
    assert published["projects"] == [BETA]
    assert published["publishes_config"] is False
    assert published["restarts_service"] is False
    assert path.read_bytes() == before
    assert config.project_in_scope(beta)
    assert not config.project_in_scope(alpha)
    assert not config.project_in_scope(config.projects_root)
    replay = apply(config, path, BETA, beta, prepared["confirmation"])
    assert replay["state"] == "replayed"
    assert replay["changed"] is False
    again = plan(config, path, BETA, beta)
    assert again["state"] == "already_authorized"
    assert again["confirmation"] == prepared["confirmation"]


def test_static_scope_remains_when_policy_is_absent(tmp_path):
    config, _path, root, alpha, beta, _gamma = write_config(tmp_path)
    assert config.project_in_scope(alpha)
    assert not config.project_in_scope(beta)
    assert not config.project_in_scope(root)
    open_config, _open_path, _root, open_alpha, open_beta, _gamma = write_config(
        tmp_path / "open", allow=None
    )
    assert open_config.project_in_scope(open_alpha)
    assert open_config.project_in_scope(open_beta)
    assert not open_config.project_in_scope(tmp_path / "open" / "outside")


def test_invalid_identity_refuses_without_a_file(tmp_path):
    config, path, root, alpha, beta, gamma = write_config(tmp_path, allow=())
    outside = tmp_path / "outside"
    outside.mkdir()
    cases = [
        (catalog((BETA, beta)), BETA, str(root), "registration_path_is_root"),
        (
            catalog((BETA, beta)),
            BETA,
            str(root / ".." / "outside"),
            "registration_path_invalid",
        ),
        (
            catalog((BETA, outside)),
            BETA,
            str(outside),
            "registration_path_outside_root",
        ),
        (catalog(), BETA, str(beta), "registration_project_unknown"),
        (
            catalog((BETA, beta), deleted=BETA),
            BETA,
            str(beta),
            "registration_project_deleted",
        ),
        (
            catalog((BETA, beta), (BETA, gamma)),
            BETA,
            str(beta),
            "registration_project_ambiguous",
        ),
        (
            catalog((BETA, gamma)),
            BETA,
            str(beta),
            "registration_identity_mismatch",
        ),
        ([{"id": BETA}], BETA, str(beta), "registration_catalog_invalid"),
        (
            catalog((BETA, beta)),
            "../beta",
            str(beta),
            "registration_project_id_invalid",
        ),
    ]
    file_path = alpha / "notes.txt"
    file_path.write_text("x")
    cases.append(
        (
            catalog((BETA, file_path)),
            BETA,
            str(file_path),
            "registration_path_not_directory",
        )
    )
    link = root / "alias"
    link.symlink_to(outside, target_is_directory=True)
    cases.append((catalog((BETA, link)), BETA, str(link), "registration_path_symlink"))
    inside = root / "inside-link"
    inside.symlink_to(beta, target_is_directory=True)
    cases.append(
        (catalog((BETA, inside)), BETA, str(inside), "registration_path_symlink")
    )
    for rows, project_id, raw_path, code in cases:
        with pytest.raises(ProjectRegistrationError, match=f"^{code}$"):
            plan_registration(config, rows, project_id, raw_path, config_path=path)
        assert not (config.state_dir / POLICY_NAME).exists()


def test_changed_confirmation_catalog_and_config_do_not_publish(tmp_path):
    config, path, _root, _alpha, beta, gamma = write_config(tmp_path, allow=())
    prepared = plan(config, path, BETA, beta)
    with pytest.raises(
        ProjectRegistrationError, match="^registration_confirmation_invalid$"
    ):
        apply(config, path, BETA, beta, prepared["confirmation"].upper())
    flipped = prepared["confirmation"][:-1] + (
        "0" if prepared["confirmation"][-1] != "0" else "1"
    )
    with pytest.raises(
        ProjectRegistrationError, match="^registration_confirmation_mismatch$"
    ):
        apply(config, path, BETA, beta, flipped)
    with pytest.raises(
        ProjectRegistrationError, match="^registration_identity_mismatch$"
    ):
        apply(
            config,
            path,
            BETA,
            beta,
            prepared["confirmation"],
            rows=catalog((BETA, gamma)),
        )
    original = path.read_bytes()
    path.write_text(original.decode() + "\n")
    with pytest.raises(ProjectRegistrationError, match="^registration_config_changed$"):
        apply(config, path, BETA, beta, prepared["confirmation"])
    path.write_bytes(original)
    bare = Config(
        projects_root=config.projects_root,
        vault_root=config.vault_root,
        state_dir=config.state_dir,
        allowed_projects=[],
    )
    with pytest.raises(
        ProjectRegistrationError, match="^registration_config_unpinned$"
    ):
        plan_registration(bare, catalog((BETA, beta)), BETA, str(beta))
    assert not (config.state_dir / POLICY_NAME).exists()


def test_second_add_keeps_the_first_and_stale_plan_refuses(tmp_path):
    config, path, _root, _alpha, beta, gamma = write_config(tmp_path, allow=())
    first = plan(config, path, BETA, beta)
    apply(config, path, BETA, beta, first["confirmation"])
    stale = plan_registration(
        config, catalog((GAMMA, gamma)), GAMMA, str(gamma), config_path=path
    )
    # Rebuild the stale confirmation against the empty policy, as a lost race.
    stale_confirmation = confirmation_for(
        {
            "project_id": GAMMA,
            "path": str(gamma.resolve()),
            "catalog_sha256": stale["catalog_sha256"],
        },
        empty_policy_sha256(),
        config._loaded_sha256,
    )
    with pytest.raises(
        ProjectRegistrationError, match="^registration_confirmation_mismatch$"
    ):
        apply(config, path, GAMMA, gamma, stale_confirmation)
    published = apply(config, path, GAMMA, gamma, stale["confirmation"])
    assert published["projects"] == [BETA, GAMMA]
    assert config.project_in_scope(beta)
    assert config.project_in_scope(gamma)
    with pytest.raises(ProjectRegistrationError, match="^registration_duplicate$"):
        plan_registration(
            config,
            catalog((BETA, gamma)),
            BETA,
            str(gamma),
            config_path=path,
        )
    assert {
        item["project_id"]
        for item in json.loads((config.state_dir / POLICY_NAME).read_text())["projects"]
    } == {BETA, GAMMA}


def test_malformed_local_edit_and_symlink_grant_nothing_dynamic(tmp_path):
    config, path, root, alpha, beta, gamma = write_config(tmp_path)
    first = plan(config, path, BETA, beta)
    apply(config, path, BETA, beta, first["confirmation"])
    second = plan(config, path, GAMMA, gamma)
    apply(config, path, GAMMA, gamma, second["confirmation"])
    policy = config.state_dir / POLICY_NAME
    original = policy.read_bytes()
    assert config.project_in_scope(alpha)
    assert config.project_in_scope(beta)
    policy.write_bytes(original.replace(b"proj-beta", b"proj-XXXX"))
    assert config.project_in_scope(alpha)
    assert not config.project_in_scope(beta)
    assert not config.project_in_scope(gamma)
    with pytest.raises(ProjectRegistrationError, match="^registration_policy_invalid$"):
        plan(config, path, BETA, beta)
    policy.write_bytes(original)
    outside = tmp_path / "outside"
    outside.mkdir()
    beta_resolved = str(beta.resolve())
    forged = {
        "project_id": "proj-forged",
        "path": str(outside.resolve()),
        "catalog_sha256": "a" * 64,
        "base_policy_sha256": empty_policy_sha256(),
        "config_sha256": config._loaded_sha256,
    }
    forged["confirmation"] = confirmation_for(
        forged, forged["base_policy_sha256"], forged["config_sha256"]
    )
    valid = json.loads(original)["projects"]
    body = {"schema_version": 1, "projects": [forged, *valid]}
    body["document_sha256"] = content_hash(
        {"schema_version": 1, "projects": body["projects"]}
    )
    policy.write_text(json.dumps(body))
    assert not config.project_in_scope(beta)
    assert not config.project_in_scope(outside)
    assert config.project_in_scope(alpha)
    policy.write_bytes(original)
    linked = config.state_dir / "elsewhere.json"
    linked.write_text(original.decode())
    policy.unlink()
    policy.symlink_to(linked)
    assert not config.project_in_scope(beta)
    assert config.project_in_scope(alpha)
    policy.unlink()
    policy.write_bytes(original)
    published_beta = Path(beta_resolved)
    published_beta.rename(root / "beta-real")
    published_beta.symlink_to(outside, target_is_directory=True)
    assert not config.project_in_scope(published_beta)
    assert config.project_in_scope(gamma)
    assert policy.read_bytes() == original


def test_concurrent_apply_keeps_one_document(tmp_path):
    config, path, _root, _alpha, beta, gamma = write_config(tmp_path, allow=())
    beta_plan = plan(config, path, BETA, beta)
    gamma_plan = plan(config, path, GAMMA, gamma)
    barrier = threading.Barrier(2)
    results: list = []

    def worker(project_id, directory, confirmation):
        barrier.wait()
        try:
            results.append(
                apply(config, path, project_id, directory, confirmation)["state"]
            )
        except ProjectRegistrationError as exc:
            results.append(exc.code)

    threads = [
        threading.Thread(target=worker, args=(BETA, beta, beta_plan["confirmation"])),
        threading.Thread(
            target=worker, args=(GAMMA, gamma, gamma_plan["confirmation"])
        ),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results) == ["published", "registration_confirmation_mismatch"]
    document = json.loads((config.state_dir / POLICY_NAME).read_text())
    assert len(document["projects"]) == 1
    assert document["document_sha256"] == content_hash(
        {"schema_version": 1, "projects": document["projects"]}
    )


def test_lock_timeout_writes_nothing(tmp_path):
    config, path, _root, _alpha, beta, _gamma = write_config(tmp_path, allow=())
    prepared = plan(config, path, BETA, beta)
    lock = config.state_dir / LOCK_NAME
    descriptor = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    import fcntl

    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(
            ProjectRegistrationError, match="^registration_lock_timeout$"
        ):
            apply(config, path, BETA, beta, prepared["confirmation"])
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
    assert not (config.state_dir / POLICY_NAME).exists()


def test_registration_preserves_capacity_and_reaches_existing_scope_paths(tmp_path):
    short = Path("/tmp") / f"oraculo-reg-{os.getpid()}"
    short.mkdir()
    try:
        config, path, _root, alpha, beta, gamma = write_config(short)
        # Move the prepared tree under the short directory. write_config used short.
        original = path.read_bytes()
        service = Service(config, path)
        service.publisher.initialize()
        service.state.register_project(ALPHA, str(alpha.resolve()))
        service.health = "disconnected"
        service.catalog = {}
        service.state.connection.execute(
            "INSERT INTO managed_reservations VALUES(?,?,?,'reserved',NULL)",
            ("reserve-synthetic", "codex", 9),
        )
        capacity = service.state.capacity()
        assert capacity["configured_ceiling"] == 33
        assert capacity["occupied"] == 9
        assert capacity["available"] == 24
        providers = config.model_dump()["providers"]
        profiles = config.model_dump()["profiles"]
        meta = [
            row[0]
            for row in service.state.connection.execute("SELECT key FROM metadata")
        ]
        names = {item.name for item in config.state_dir.iterdir()}
        prepared = plan(config, path, BETA, beta)
        published = apply(config, path, BETA, beta, prepared["confirmation"])
        assert published["config_sha256"] == config._loaded_sha256
        assert path.read_bytes() == original
        assert service.config_sha256 == config._loaded_sha256
        assert service.health == "disconnected"
        assert service.catalog == {}
        assert service.state.capacity() == capacity
        assert providers == config.model_dump()["providers"]
        assert profiles == config.model_dump()["profiles"]
        assert config.additional_spend_allowed is False
        assert [
            row[0]
            for row in service.state.connection.execute("SELECT key FROM metadata")
        ] == meta
        assert {item.name for item in config.state_dir.iterdir()} - names == {
            POLICY_NAME,
            LOCK_NAME,
        }
        assert (
            service.state.connection.execute("SELECT count(*) FROM jobs").fetchone()[0]
            == 0
        )
        assert (
            service.state.connection.execute(
                "SELECT count(*) FROM attempts"
            ).fetchone()[0]
            == 0
        )
        assert service.state.get_project(BETA) is None
        assert service.config.project_in_scope(beta.resolve())
        asyncio.run(_running_scope(service, config, beta, gamma))
        assert service.state.get_project(BETA)["status"] == "registered"
        assert service.state.get_project(ALPHA)["status"] == "registered"
        assert service.state.capacity() == capacity
        assert service.health == "disconnected"
        assert (
            service.state.connection.execute("SELECT count(*) FROM jobs").fetchone()[0]
            == 0
        )
        service.state.close()
        reloaded = Config.load(path)
        assert reloaded._loaded_sha256 == config._loaded_sha256
        assert path.read_bytes() == original
        restarted = Service(reloaded, path)
        try:
            assert restarted.config.project_in_scope(beta.resolve())
            assert restarted.project(BETA)["status"] == "registered"
            assert restarted.project(ALPHA)["path"] == str(alpha.resolve())
            assert restarted.state.capacity()["occupied"] == 9
            assert restarted.config_sha256 == config._loaded_sha256
        finally:
            restarted.state.close()
    finally:
        import shutil

        shutil.rmtree(short, ignore_errors=True)


async def _running_scope(service, config, beta: Path, gamma: Path):
    class Host:
        async def call(self, name, arguments):
            assert name == "t3_project_list"
            return {
                "projects": [{"id": BETA, "workspaceRoot": str(beta)}],
                "nextCursor": None,
            }

    managed = ManagedTasks(config, store=None, host=Host(), memory=None)
    with pytest.raises(StateError, match="managed_project_scope_revoked"):
        await managed._scope(
            {
                "project": {
                    "project_id": GAMMA,
                    "project_path": str(gamma.resolve()),
                    "repository_common_dir": None,
                }
            }
        )
    await managed._scope(
        {
            "project": {
                "project_id": BETA,
                "project_path": str(beta.resolve()),
                "repository_common_dir": None,
            }
        }
    )
    envelope = Envelope.model_validate(
        {
            "schema_version": 1,
            "request_id": "synthetic-memory",
            "kind": "memory.cycle.status",
            "project_id": BETA,
            "origin": {"thread_id": "thread-synthetic", "run_id": "run-synthetic"},
            "payload": {"cycle_id": "missing-cycle"},
        }
    )
    with pytest.raises(MemoryError, match="memory_cycle_not_found"):
        await service.direct_memory_command(envelope)
    from oraculo.experience_service import snapshot

    service.publisher.initialize_project(BETA)
    observed = snapshot(service, BETA)
    assert observed["project_id"] == BETA
    assert observed["projects"] == [{"id": BETA, "title": beta.resolve().name}]
    server = await asyncio.start_unix_server(
        service.connection,
        path=str(config.socket_path),
        limit=MAX_ENVELOPE_BYTES + 1,
    )
    async with server:
        status = await request(config, make_envelope("status.get", BETA, {}))
    assert status["ok"] is True
    assert status["data"]["health"] == "disconnected"
    assert status["data"]["runtime"]["pid"] == os.getpid()
    assert status["data"]["runtime"]["config_sha256"] == config._loaded_sha256
    listed = {row["id"]: row["status"] for row in status["data"]["projects"]}
    assert listed[ALPHA] == "registered"
    assert listed[BETA] == "registered"
    assert (
        service.state.connection.execute("SELECT count(*) FROM objectives").fetchone()[
            0
        ]
        == 0
    )


def test_later_catalog_unregistration_is_not_resurrected(tmp_path):
    config, path, _root, _alpha, beta, gamma = write_config(tmp_path)
    service = Service(config, path)
    try:
        prepared = plan(config, path, BETA, beta)
        apply(config, path, BETA, beta, prepared["confirmation"])
        assert service.state.get_project(BETA) is None
        assert service.project(BETA)["status"] == "registered"
        service.state.register_project(BETA, str(beta), "unregistered")
        service.catalog = {"projects": [{"id": BETA, "workspaceRoot": str(beta)}]}
        listed = {row["id"]: row["status"] for row in service.status(BETA)["projects"]}
        assert listed[BETA] == "unregistered"
        assert service.state.get_project(BETA)["status"] == "unregistered"
        service.catalog = {
            "projects": [
                {
                    "id": BETA,
                    "workspaceRoot": str(beta),
                    "deletedAt": "2026-01-01T00:00:00Z",
                }
            ]
        }
        service.status("*")
        assert service.state.get_project(BETA)["status"] == "unregistered"
        with pytest.raises(
            StateError, match="^project_not_registered_or_outside_scope$"
        ):
            service.project(BETA)
        assert service.state.get_project(BETA)["status"] == "unregistered"
        envelope = Envelope.model_validate(
            {
                "schema_version": 1,
                "request_id": "synthetic-memory-unregistered",
                "kind": "memory.cycle.status",
                "project_id": BETA,
                "origin": {
                    "thread_id": "thread-synthetic",
                    "run_id": "run-synthetic",
                },
                "payload": {"cycle_id": "missing-cycle"},
            }
        )
        with pytest.raises(
            StateError, match="^project_not_registered_or_outside_scope$"
        ):
            asyncio.run(service.direct_memory_command(envelope))
        assert service.state.get_project(BETA)["status"] == "unregistered"
        second = plan(config, path, GAMMA, gamma)
        apply(config, path, GAMMA, gamma, second["confirmation"])
        service.status("*")
        assert service.state.get_project(GAMMA)["status"] == "registered"
        assert service.state.get_project(BETA)["status"] == "unregistered"
        service.state.register_project(BETA, str(gamma), "unregistered")
        service.status(BETA)
        stored = service.state.get_project(BETA)
        assert stored["status"] == "unregistered"
        assert stored["path"] == str(gamma)
        assert (
            service.state.connection.execute("SELECT count(*) FROM jobs").fetchone()[0]
            == 0
        )
    finally:
        service.state.close()


def test_cli_plan_and_apply_use_a_fresh_catalog(tmp_path, monkeypatch, capsys):
    config, path, _root, _alpha, beta, gamma = write_config(tmp_path, allow=())
    calls = {"n": 0, "rows": catalog((BETA, beta))}

    async def fake(_config):
        calls["n"] += 1
        return calls["rows"]

    monkeypatch.setattr("oraculo.project_registration.load_t3_catalog", fake)
    assert (
        main(
            [
                "--config",
                str(path),
                "project",
                "register",
                "plan",
                "--id",
                BETA,
                "--path",
                str(beta),
            ]
        )
        == 0
    )
    prepared = json.loads(capsys.readouterr().out)
    assert prepared["confirmation"]
    calls["rows"] = catalog((BETA, gamma))
    assert (
        main(
            [
                "--config",
                str(path),
                "project",
                "register",
                "apply",
                "--id",
                BETA,
                "--path",
                str(beta),
                "--confirmation",
                prepared["confirmation"],
            ]
        )
        == 1
    )
    error = json.loads(capsys.readouterr().err)
    assert error["message"] == "registration_identity_mismatch"
    assert calls["n"] == 2
    assert not (config.state_dir / POLICY_NAME).exists()
    calls["rows"] = catalog((BETA, beta))
    assert (
        main(
            [
                "--config",
                str(path),
                "project",
                "register",
                "apply",
                "--id",
                BETA,
                "--path",
                str(beta),
                "--confirmation",
                prepared["confirmation"],
            ]
        )
        == 0
    )
    published = json.loads(capsys.readouterr().out)
    assert published["state"] == "published"
    assert Config.load(path).project_in_scope(beta)


def test_cli_help_states_the_boundary():
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "oraculo",
            "project",
            "register",
            "plan",
            "--help",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0
    assert "Não publica" in result.stdout
    apply_help = subprocess.run(
        [sys.executable, "-m", "oraculo", "project", "register", "apply", "--help"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    assert apply_help.returncode == 0
    assert "config.toml" in apply_help.stdout
    register_help = subprocess.run(
        [sys.executable, "-m", "oraculo", "project", "register", "--help"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    assert "Somente adição" in register_help.stdout
    assert "Não remove" in register_help.stdout


async def test_dynamic_project_keeps_ci_auth_outcomes_and_schema_fences(tmp_path):
    from test_ci_events import binding, event, observer
    from test_outcomes import source
    from test_plan_service import task

    from oraculo.state import State
    from oraculo.task_store import TaskStore

    config, path, _root, _alpha, beta, _gamma = write_config(tmp_path, allow=())
    original = path.read_bytes()
    service = Service(config, path)
    try:
        assert service.state.schema_version() == 4
        prepared = plan(config, path, BETA, beta)
        apply(config, path, BETA, beta, prepared["confirmation"])
        service.project(BETA)
        assert service.state.schema_version() == 4
        sha = source(beta)
        bound = binding(head=sha)
        spec = task("build")
        spec["payload"] = {"ci_binding": bound.model_dump()}
        import hashlib

        spec["criteria"][0]["outcome"] = {
            "version": 2,
            "commit": sha,
            "argv": [
                sys.executable,
                "-c",
                "from pathlib import Path; Path('proof.txt').write_text('proof')",
            ],
            "artifact": "proof.txt",
            "sha256": hashlib.sha256(b"proof").hexdigest(),
        }
        submitted = service.plans.submit(
            BETA,
            {"text": "Synthetic dynamic project", "tasks": [spec]},
            "dynamic-orchestration",
            {"implementation": ["codex"]},
        )
        assert service.state.schema_version() == 6
        service.state.set_coordinator(BETA, "coordinator")
        job = submitted["tasks"][0]["job_id"]
        assert (
            service.state.acquire_grant(job, "codex", "m", "coordinator", "run") is None
        )

        def envelope(kind, payload, thread="coordinator", run="run"):
            return Envelope.model_validate(
                {
                    **make_envelope(kind, BETA, payload),
                    "origin": {"thread_id": thread, "run_id": run},
                }
            )

        with pytest.raises(StateError, match="orchestration_payload_fields_invalid"):
            await service.handle(
                envelope("ci.observe", {"job_id": job, "authenticated": True})
            )
        service.ci_observer = observer(event(head=sha), valid=False)
        with pytest.raises(StateError):
            await service.handle(envelope("ci.observe", {"job_id": job}))
        service.ci_observer = observer(event(head=sha))
        with pytest.raises(StateError, match="coordinator_identity_required"):
            await service.handle(
                envelope("ci.observe", {"job_id": job}, thread="stranger")
            )
        assert (
            service.state.acquire_grant(job, "codex", "m", "coordinator", "run") is None
        )
        assert (await service.handle(envelope("ci.observe", {"job_id": job})))["ok"]
        attempt = service.state.acquire_grant(job, "codex", "m", "coordinator", "run")
        service.state.claim_grant(
            attempt["id"], attempt["generation"], "executor", "execution"
        )
        payload = {
            "attempt_id": attempt["id"],
            "generation": attempt["generation"],
            "criterion_id": "proof",
        }
        with pytest.raises(StateError, match="grant_owner_mismatch"):
            await service.handle(envelope("outcome.verify", payload, thread="stranger"))
        (beta / "source.py").write_text("changed without a new HEAD\n")
        dirty = await service.handle(
            envelope("outcome.verify", payload, thread="executor", run="execution")
        )
        assert dirty["data"]["status"] == "pending"
        assert not (beta / "proof.txt").exists()
        subprocess.run(["git", "restore", "source.py"], cwd=beta, check=True)
        verified = await service.handle(
            envelope("outcome.verify", payload, thread="executor", run="execution")
        )
        assert verified["data"]["status"] == "passed"
        assert verified["data"]["command"]["exit"] == 0
        assert path.read_bytes() == original
        assert config.additional_spend_allowed is False
        assert config.decisions.enabled is False and config.decisions.mode == "off"
        assert (
            config.decisions.daily_budget_microusd
            == config.decisions.run_budget_microusd
            == 0
        )
        assert service.project(BETA)["path"] == str(beta.resolve())
    finally:
        service.state.close()
    with pytest.raises(StateError, match="state_schema_newer_than_runtime"):
        State(config.db_path, schema_max=4)
    # Managed deliverables promote their own ledger, independently of main state.
    ledger = config.state_dir / "managed-tasks.sqlite3"
    writer = TaskStore(ledger)
    record = writer.create(
        {"thread_id": "synthetic-origin"},
        "validation",
        {"deliverable_kind": "validation"},
    )
    writer.close()
    before = ledger.read_bytes()
    reader = TaskStore(ledger, read_only=True, schema_max=6)
    assert reader.get(record["id"])["request"]["deliverable_kind"] == "validation"
    assert (
        reader.db.execute("SELECT max(version) FROM schema_migrations").fetchone()[0]
        == 6
    )
    import sqlite3

    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        reader.db.execute("DELETE FROM managed_tasks")
    reader.close()
    assert ledger.read_bytes() == before
    with pytest.raises(StateError, match="managed_ledger_read_unavailable"):
        TaskStore(ledger, read_only=True, schema_max=5)
    assert ledger.read_bytes() == before


@pytest.mark.parametrize("phase", ["plan", "apply"])
async def test_registration_catalog_auth_failure_cannot_publish_or_mutate_config(
    tmp_path, monkeypatch, phase
):
    from types import SimpleNamespace

    from oraculo.project_registration import cli_command
    from oraculo.t3.auth import AuthRequired

    config, path, _root, _alpha, beta, _gamma = write_config(tmp_path, allow=())
    before = path.read_bytes()
    resolved = []

    def unavailable(endpoint):
        resolved.append(endpoint)
        raise AuthRequired("synthetic Keychain unavailable")

    monkeypatch.setattr("oraculo.t3.auth.build_auth", unavailable)
    args = SimpleNamespace(
        project_action="register",
        phase=phase,
        id=BETA,
        path=str(beta),
        config=path,
        confirmation="a" * 64,
    )
    with pytest.raises(AuthRequired, match="synthetic Keychain unavailable"):
        await cli_command(config, args)
    assert resolved == [config.t3_mcp_url]
    assert path.read_bytes() == before
    assert list(config.state_dir.iterdir()) == []
