"""Bounded admission, writer exclusion and recovery with multiple durable leases."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from test_reconcile import Remote, snapshot

from oraculo.config import Config, ProviderConfig
from oraculo.reconcile import Reconciler
from oraculo.routing import Router
from oraculo.scheduler import Scheduler
from oraculo.state import SCHEMA, StaleGrant, State, StateError


def setup(tmp_path, capacity=3):
    state = State(tmp_path / "db", max_executors=capacity)
    state.register_project("p", "/tmp/p")
    state.register_project("q", "/tmp/q")
    objective = state.submit_objective("p", "parallel work")
    return state, objective


def job(state, objective, paths, *, kind="implementation", dependencies=()):
    payload = {} if paths is None else {"task_spec": {"write_paths": paths}}
    return state.enqueue_job(objective["id"], kind, payload, dependencies)


def acquire(state, job):
    return state.acquire_grant(job["id"], "codex", "m", "owner", "owner-run")


def finish(state, grant, status="succeeded"):
    run = "run-" + grant["id"]
    state.claim_grant(grant["id"], grant["generation"], "thread-" + grant["id"], run)
    assert state.record_execution(
        grant["id"],
        grant["generation"],
        run,
        status,
        pending_children=False,
        tree_confirmed=True,
    )


def test_capacity_and_per_attempt_lifecycle(tmp_path):
    state, objective = setup(tmp_path, 2)
    a, b, c = [job(state, objective, [path]) for path in ("src/a", "src/b", "src/c")]
    first, second = acquire(state, a), acquire(state, b)
    assert first and second and acquire(state, c) is None
    assert state.lease() == state.lease(first["id"])
    assert state.validate_grant(second["id"], second["generation"]) == second
    assert state.acquire_grant(b["id"], "codex", "m", "owner", "run") is None
    before = state.lease(first["id"])
    state.claim_grant(second["id"], second["generation"], "second", "second-run")
    assert state.lease(first["id"]) == before
    assert state.lease(second["id"])["state"] == "claimed"
    assert not state.record_execution(
        second["id"],
        first["generation"],
        "second-run",
        "succeeded",
        pending_children=False,
        tree_confirmed=True,
    )
    assert state.record_execution(
        second["id"],
        second["generation"],
        "second-run",
        "succeeded",
        pending_children=False,
        tree_confirmed=True,
    )
    assert state.lease(first["id"]) == before
    assert acquire(state, c)
    with pytest.raises(StaleGrant):
        state.validate_grant(second["id"], second["generation"])


@pytest.mark.parametrize(
    "a,b,conflict",
    [
        (["src"], ["src/file.py"], True),
        (["src/file.py"], ["src"], True),
        (["src/a"], ["src/a"], True),
        (["src/a"], ["src/ab"], False),
        (["*"], ["docs"], True),
        (["src"], ["*"], True),
        (None, ["src"], True),
        (["src"], None, True),
        ([], ["src"], False),
        (["*"], [], False),
        ([], [], False),
    ],
)
def test_writer_scopes(tmp_path, a, b, conflict):
    state, objective = setup(tmp_path)
    assert acquire(state, job(state, objective, a))
    assert (acquire(state, job(state, objective, b)) is None) == conflict


@pytest.mark.parametrize(
    "paths",
    [
        "src",
        None,
        [""],
        ["/tmp/a"],
        ["../a"],
        ["a/../b"],
        ["./a"],
        ["a//b"],
        ["a/"],
        ["."],
        ["a\\b"],
        ["a/*"],
        ["a\x00b"],
        [1],
        ["C:/src"],
        ["a/[bc]"],
    ],
)
def test_reject_noncanonical_scope(tmp_path, paths):
    state, objective = setup(tmp_path)
    with pytest.raises(StateError):
        state.enqueue_job(
            objective["id"], "implementation", {"task_spec": {"write_paths": paths}}
        )
    assert state.list_jobs() == []


@pytest.mark.parametrize("memory_first", [False, True])
def test_project_isolation_and_global_memory_exclusion(tmp_path, memory_first):
    state, objective = setup(tmp_path)
    other = state.submit_objective("q", "other project")
    if memory_first:
        assert acquire(state, job(state, objective, [], kind="memory"))
        assert acquire(state, job(state, other, [])) is None
    else:
        assert acquire(state, job(state, objective, None))
        assert acquire(state, job(state, other, None))
        assert acquire(state, job(state, other, [], kind="memory")) is None


def test_default_serial_and_capacity_validation(tmp_path):
    state, objective = setup(tmp_path, 1)
    state.close()
    state = State(tmp_path / "db")
    assert acquire(state, job(state, objective, []))
    assert acquire(state, job(state, objective, [])) is None
    for capacity in (0, -1, 5, True, 1.5, "2"):
        with pytest.raises(StateError):
            State(tmp_path / "invalid", max_executors=capacity)


def test_migrate_live_singleton_and_restart_with_lower_capacity(tmp_path):
    legacy = SCHEMA.replace(
        "slot INTEGER PRIMARY KEY CHECK(slot>=1)",
        "singleton INTEGER PRIMARY KEY CHECK(singleton=1)",
    ).replace(
        "attempt_id TEXT NOT NULL UNIQUE REFERENCES",
        "attempt_id TEXT NOT NULL REFERENCES",
    )
    with sqlite3.connect(tmp_path / "db") as db:
        db.executescript(legacy)
        db.execute("INSERT INTO projects VALUES('p','/tmp/p','registered',NULL)")
        db.execute(
            "INSERT INTO objectives(id,project_id,text,origin,evidence,priority,created_at) VALUES('o','p','work','human','null',100,0)"
        )
        db.execute(
            "INSERT INTO jobs(id,objective_id,kind,payload,payload_hash,state,created_at) VALUES('j','o','implementation','{}','hash','running',0)"
        )
        db.execute(
            "INSERT INTO attempts(id,job_id,number,generation,provider,model,owner_thread_id,owner_run_id,thread_id,run_id,status,created_at) VALUES('a','j',1,7,'codex','m','owner','r','child','run','running',0)"
        )
        db.execute("INSERT INTO executor_lease VALUES(1,7,'a','claimed')")
        db.execute("INSERT INTO metadata VALUES('grant_generation','7')")
    state = State(tmp_path / "db", max_executors=3)
    assert state.lease() == {
        "slot": 1,
        "generation": 7,
        "attempt_id": "a",
        "state": "claimed",
    }
    assert state.validate_grant("a", 7)["run_id"] == "run"
    state.register_project("q", "/tmp/q")
    other = state.submit_objective("q", "other")
    grant = acquire(state, job(state, other, []))
    assert grant["generation"] == 8
    original = state.leases()
    state.close()
    state = State(tmp_path / "db")
    assert state.leases() == original
    assert acquire(state, job(state, other, [])) is None
    assert state.connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_dependencies_and_scheduler_skip_conflicts(tmp_path):
    state, objective = setup(tmp_path)
    first_job = job(state, objective, ["src"])
    blocked = job(state, objective, ["src/a"])
    dependent = job(state, objective, ["tests"], dependencies=[first_job["id"]])
    independent = job(state, objective, ["docs"])
    router = Router(
        Config(
            providers={
                "codex": ProviderConfig(
                    instance_id="codex", billing_verified=True, models=["m"]
                )
            }
        )
    )
    scheduler = Scheduler(state, router)
    catalog = {"providers": [{"providerInstanceId": "codex", "models": ["m"]}]}
    first = scheduler.admit_next("owner", "run", catalog)
    assert first["job_id"] == first_job["id"]
    second = scheduler.admit_next("owner", "run", catalog)
    assert second["job_id"] == independent["id"]
    assert len(state.leases()) == 2
    assert acquire(state, dependent) is None
    assert acquire(state, blocked) is None
    finish(state, first)
    assert acquire(state, dependent)


@pytest.mark.parametrize("conflicting", [False, True])
def test_atomic_admission_across_connections(tmp_path, conflicting):
    state, objective = setup(tmp_path, 2)
    jobs = [
        job(state, objective, ["same" if conflicting else str(i)]) for i in range(4)
    ]
    barrier = Barrier(4)

    def admit(candidate):
        connection = State(tmp_path / "db", max_executors=2)
        try:
            barrier.wait(timeout=10)
            return acquire(connection, candidate)
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=4) as pool:
        grants = list(pool.map(admit, jobs))
    assert sum(grant is not None for grant in grants) == (1 if conflicting else 2)
    assert len(state.leases()) == (1 if conflicting else 2)


async def test_cancel_all_leases_and_dependents_after_restart(tmp_path):
    state, objective = setup(tmp_path)
    first_job, second_job = [job(state, objective, [path]) for path in ("src", "docs")]
    successor = job(
        state, objective, [], dependencies=[first_job["id"], second_job["id"]]
    )
    grants = [acquire(state, candidate) for candidate in (first_job, second_job)]
    state.claim_grant(grants[0]["id"], grants[0]["generation"], "child", "run")
    state.cancel_objective(objective["id"])
    state.bind_cancelled_execution(
        grants[1]["id"], grants[1]["generation"], "second", "second-run"
    )
    assert state.lease(grants[0]["id"])["state"] == "claimed"
    state.close()
    state = State(tmp_path / "db")
    remote = Remote()
    remote.snapshots["second"] = snapshot("second", "second-run")
    reconciler = Reconciler(state, remote)
    results = await reconciler.reconcile()
    assert isinstance(results, list) and len(results) == 2
    assert all(result["state"] == "cancel_requested" for result in results)
    assert set(remote.interruptions) == {("child", "run"), ("second", "second-run")}
    remote.snapshots["second"] = snapshot("second", "second-run", "interrupted")
    assert [r["state"] for r in await reconciler.reconcile()] == [
        "cancel_requested",
        "stopped",
    ]
    assert state.get_objective(objective["id"])["status"] == "cancel_requested"
    assert len(state.leases()) == 1
    remote.snapshots["child"] = snapshot("child", "run", "interrupted")
    assert (await reconciler.reconcile())[0]["state"] == "stopped"
    assert state.get_objective(objective["id"])["status"] == "stopped"
    assert state.get_job(successor["id"])["state"] == "stopped"
    assert acquire(state, successor) is None
    assert await reconciler.reconcile() == []
    assert state.connection.execute("SELECT count(*) FROM attempts").fetchone()[0] == 2


async def test_uncertain_lease_does_not_prevent_other_reconciliation(tmp_path):
    state, objective = setup(tmp_path, 2)
    first = acquire(state, job(state, objective, ["src"]))
    second = acquire(state, job(state, objective, ["docs"]))
    state.claim_grant(second["id"], second["generation"], "child", "run")
    remote = Remote()
    remote.snapshots["child"] = snapshot("child", "run", "failed")
    results = await Reconciler(state, remote).reconcile()
    assert [r["state"] for r in results] == ["uncertain", "failed"]
    assert state.lease(first["id"])
    assert state.lease(second["id"]) is None
    assert acquire(state, job(state, objective, ["src/file"])) is None
    assert acquire(state, job(state, objective, ["docs/new"]))


def test_fail_unstarted_targets_second_lease_only(tmp_path):
    state, objective = setup(tmp_path)
    first = acquire(state, job(state, objective, ["src"]))
    second = acquire(state, job(state, objective, ["docs"]))
    before = state.lease(first["id"])
    state.fail_unstarted(
        second["id"],
        second["generation"],
        {
            "definitive_no_child": True,
            "error_kind": "quota",
            "client_request_id": "request",
            "tool_error": "quota exceeded",
        },
    )
    assert state.lease(first["id"]) == before
    assert state.lease(second["id"]) is None
    assert state.get_objective(objective["id"])["status"] == "running"


async def test_late_remote_error_cannot_reopen_released_attempt(tmp_path):
    state, objective = setup(tmp_path)
    first = acquire(state, job(state, objective, ["src"]))
    second = acquire(state, job(state, objective, ["docs"]))
    state.claim_grant(first["id"], first["generation"], "child", "run")
    remote = Remote()

    async def release_then_disconnect(thread_id, **kwargs):
        assert state.record_execution(
            first["id"],
            first["generation"],
            "run",
            "failed",
            pending_children=False,
            tree_confirmed=True,
        )
        raise ConnectionError("response lost after another reconciliation")

    remote.read_thread = release_then_disconnect
    results = await Reconciler(state, remote).reconcile()
    assert len(results) == 2
    assert state.get_attempt(first["id"])["status"] == "failed"
    assert state.get_job(first["job_id"])["state"] == "failed"
    assert state.lease(first["id"]) is None
    assert state.lease(second["id"])["state"] == "uncertain"
    assert acquire(state, state.get_job(second["job_id"])) is None


def test_cancel_and_fail_unstarted_waits_for_every_lease(tmp_path):
    state, objective = setup(tmp_path)
    grants = [acquire(state, job(state, objective, [path])) for path in ("src", "docs")]
    state.cancel_objective(objective["id"])
    for index, grant in enumerate(grants):
        state.fail_unstarted(
            grant["id"],
            grant["generation"],
            {
                "definitive_no_child": True,
                "error_kind": "validation",
                "client_request_id": str(index),
                "tool_error": "invalid model",
            },
        )
        assert state.get_objective(objective["id"])["status"] == (
            "cancel_requested" if index == 0 else "stopped"
        )
    assert state.leases() == []


@pytest.mark.parametrize("layout", ["same_root", "nested_root", "symlink"])
def test_writer_exclusion_uses_physical_paths_across_projects(tmp_path, layout):
    state, objective = setup(tmp_path)
    root = tmp_path / "workspace"
    (root / "src").mkdir(parents=True)
    state.register_project("p", root)
    if layout == "same_root":
        state.register_project("q", root)
        other_path = "src/file"
    elif layout == "nested_root":
        state.register_project("q", root / "src")
        other_path = "file"
    else:
        alias = tmp_path / "alias"
        alias.mkdir()
        (alias / "source").symlink_to(root / "src", target_is_directory=True)
        state.register_project("q", alias)
        other_path = "source/file"
    other = state.submit_objective("q", "other project, shared files")
    first = acquire(state, job(state, objective, ["src"]))
    assert first
    candidate = job(state, other, [other_path])
    assert acquire(state, candidate) is None
    finish(state, first)
    assert acquire(state, candidate)


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize(
    "first_path,second_path",
    [
        ("Src/a", "src/A"),
        ("Src", "src/future.py"),
        ("Café/a", "Cafe\u0301/A"),
    ],
)
def test_case_and_unicode_aliases_conflict_before_files_exist(
    tmp_path, existing, first_path, second_path
):
    state, objective = setup(tmp_path)
    root = tmp_path / "Workspace"
    root.mkdir()
    state.register_project("p", root)
    if existing:
        target = root / first_path
        if first_path == "Src":
            target.mkdir()
        else:
            target.parent.mkdir(parents=True)
            target.touch()
    first = acquire(state, job(state, objective, [first_path]))
    assert first
    candidate = job(state, objective, [second_path])
    assert acquire(state, candidate) is None
    finish(state, first)
    assert acquire(state, candidate)


def test_case_aliases_across_project_roots_conflict(tmp_path):
    state, objective = setup(tmp_path)
    state.register_project("p", tmp_path / "Workspace")
    state.register_project("q", tmp_path / "workspace")
    other = state.submit_objective("q", "aliased root")
    assert acquire(state, job(state, objective, ["Src/a"]))
    assert acquire(state, job(state, other, ["src/A"])) is None
    assert acquire(state, job(state, other, ["src/ab"]))


def test_explicit_hardlink_aliases_conflict(tmp_path):
    state, objective = setup(tmp_path)
    root = tmp_path / "workspace"
    root.mkdir()
    state.register_project("p", root)
    original = root / "original.py"
    original.touch()
    (root / "alias.py").hardlink_to(original)
    first = acquire(state, job(state, objective, ["original.py"]))
    assert first
    candidate = job(state, objective, ["alias.py"])
    assert acquire(state, candidate) is None
    finish(state, first)
    assert acquire(state, candidate)


@pytest.mark.parametrize(
    "thread,run,task", [("", "r", None), ("t", " ", None), ("t", "r", "")]
)
def test_claim_requires_nonempty_execution_identity(tmp_path, thread, run, task):
    state, objective = setup(tmp_path)
    grant = acquire(state, job(state, objective, []))
    with pytest.raises(StateError, match="execution identities required"):
        state.claim_grant(grant["id"], grant["generation"], thread, run, task)
    assert state.lease(grant["id"])["state"] == "reserved"


def test_claim_cannot_replace_task_identity(tmp_path):
    state, objective = setup(tmp_path)
    grant = acquire(state, job(state, objective, []))
    state.claim_grant(grant["id"], grant["generation"], "t", "r", "task")
    with pytest.raises(StaleGrant, match="another task"):
        state.claim_grant(grant["id"], grant["generation"], "t", "r", "other")
    assert (
        state.claim_grant(grant["id"], grant["generation"], "t", "r")["task_id"]
        == "task"
    )


@pytest.mark.parametrize("cancelled", [False, True])
@pytest.mark.parametrize("duplicate", ["run", "task"])
def test_parallel_grants_cannot_claim_same_remote_execution(
    tmp_path, cancelled, duplicate
):
    state, objective = setup(tmp_path)
    first = acquire(state, job(state, objective, []))
    second = acquire(state, job(state, objective, []))
    state.claim_grant(first["id"], first["generation"], "t", "r", "task")
    claim = state.claim_grant
    if cancelled:
        state.cancel_objective(objective["id"])
        claim = state.bind_cancelled_execution
    with pytest.raises(StaleGrant, match="another live grant"):
        claim(
            second["id"],
            second["generation"],
            "t" if duplicate == "run" else "other-thread",
            "r" if duplicate == "run" else "other-run",
            "other-task" if duplicate == "run" else "task",
        )
    assert state.get_attempt(second["id"])["thread_id"] is None
    assert len(state.leases()) == 2


def test_cancel_mixed_failed_running_and_blocked_jobs_is_stopped(tmp_path):
    state, objective = setup(tmp_path)
    first = acquire(state, job(state, objective, ["src"]))
    second = acquire(state, job(state, objective, ["docs"]))
    blocked = job(state, objective, [], dependencies=[first["job_id"]])
    state.connection.execute(
        "UPDATE jobs SET state='blocked' WHERE id=?", (blocked["id"],)
    )
    finish(state, first, "failed")
    state.cancel_objective(objective["id"])
    assert state.get_job(blocked["id"])["state"] == "stopped"
    state.bind_cancelled_execution(
        second["id"], second["generation"], "second", "second-run"
    )
    assert state.record_execution(
        second["id"],
        second["generation"],
        "second-run",
        "stopped",
        pending_children=False,
        tree_confirmed=True,
    )
    assert state.get_objective(objective["id"])["status"] == "stopped"
    assert acquire(state, blocked) is None


async def test_lost_response_restart_holds_only_original_scope(tmp_path):
    state, objective = setup(tmp_path, 2)
    first_job = job(state, objective, ["src"])
    second_job = job(state, objective, ["docs"])
    successor = job(state, objective, ["src"], dependencies=[first_job["id"]])
    first, second = acquire(state, first_job), acquire(state, second_job)
    state.mark_uncertain(first["id"], "delegate response lost")
    state.close()
    state = State(tmp_path / "db", max_executors=2)
    results = await Reconciler(state, Remote()).reconcile()
    assert all(result["state"] == "uncertain" for result in results)
    assert acquire(state, first_job) is None
    assert acquire(state, successor) is None
    # A delayed coordinator receipt binds the original grant after restart.
    state.claim_grant(first["id"], first["generation"], "child", "run", "task")
    remote = Remote()
    remote.snapshots["child"] = snapshot("child", "run", "completed")
    state.set_meta(
        f"execution_report:{first['id']}",
        {
            "generation": first["generation"],
            "run_id": "run",
            "status": "succeeded",
            "evidence": {"tests": "passed"},
        },
    )
    results = await Reconciler(state, remote).reconcile()
    assert results[0]["released"] is True
    assert state.lease(second["id"])["state"] == "uncertain"
    assert acquire(state, successor)
    assert (
        state.connection.execute(
            "SELECT count(*) FROM attempts WHERE job_id=?", (first_job["id"],)
        ).fetchone()[0]
        == 1
    )


@pytest.mark.parametrize("invalid_snapshot", [{}, {"thread": None}])
async def test_malformed_remote_snapshot_retains_lease_and_reconciles_others(
    tmp_path, invalid_snapshot
):
    state, objective = setup(tmp_path, 2)
    first = acquire(state, job(state, objective, ["src"]))
    second = acquire(state, job(state, objective, ["docs"]))
    state.claim_grant(first["id"], first["generation"], "first", "run1")
    state.claim_grant(second["id"], second["generation"], "second", "run2")
    remote = Remote()
    remote.snapshots = {
        "first": invalid_snapshot,
        "second": snapshot("second", "run2", "failed"),
    }
    results = await Reconciler(state, remote).reconcile()
    assert [result["state"] for result in results] == ["uncertain", "failed"]
    assert state.lease(first["id"])
    assert state.lease(second["id"]) is None


async def test_project_mismatch_never_releases_lease(tmp_path):
    state, objective = setup(tmp_path)
    grant = acquire(state, job(state, objective, []))
    state.claim_grant(grant["id"], grant["generation"], "child", "run")
    remote = Remote()
    remote.snapshots["child"] = snapshot("child", "run", "failed")
    remote.snapshots["child"]["thread"]["projectId"] = "q"
    assert (await Reconciler(state, remote).reconcile())[0]["state"] == "uncertain"
    assert state.lease(grant["id"])


async def test_planned_report_is_revalidated_after_restart(tmp_path):
    from oraculo.plans import Plans

    state, _ = setup(tmp_path)
    plans = Plans(state)
    plans.submit(
        "p",
        {
            "text": "verified work",
            "tasks": [
                {
                    "id": identifier,
                    "owner": "executor",
                    "brief": "bounded work",
                    "write_paths": [],
                    "criteria": [{"id": "proof", "description": "test"}],
                    "depends_on": dependencies,
                }
                for identifier, dependencies in [("first", []), ("review", ["first"])]
            ],
        },
        "verified-plan",
        {"implementation": {}},
    )
    objective = state.list_objectives()[-1]
    first_job, successor = state.list_jobs(objective["id"])
    grant = acquire(state, first_job)
    state.claim_grant(grant["id"], grant["generation"], "child", "run", "task")
    state.set_meta(
        f"execution_report:{grant['id']}",
        {
            "generation": grant["generation"],
            "run_id": "run",
            "status": "succeeded",
            "evidence": {"memory_checkpoint": "succeeded"},
        },
    )
    state.close()
    state = State(tmp_path / "db", max_executors=3)
    remote = Remote()
    remote.snapshots["child"] = snapshot("child", "run", "completed")
    result = (await Reconciler(state, remote).reconcile())[0]
    assert result == {"state": "uncertain", "reason": "invalid_execution_report"}
    assert state.lease(grant["id"])
    assert acquire(state, successor) is None
    assert state.get_objective(objective["id"])["status"] != "succeeded"
