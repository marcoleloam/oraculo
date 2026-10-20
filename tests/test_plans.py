import copy
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from pydantic import ValidationError

from oraculo.operations import Operations
from oraculo.plans import Plans, PlanSpec, TaskSpec
from oraculo.state import DedupeConflict, StaleGrant, State, StateError

PROFILES = dict.fromkeys(("implementation", "review", "research", "memory"), {})


def task(identifier="build", **kwargs):
    return {
        "id": identifier,
        "owner": "implementer",
        "brief": "Implement the bounded change and report the observed proof.",
        "write_paths": ["src"],
        "criteria": [{"id": "proof", "description": "Observed proof of this task"}],
        **kwargs,
    }


def plan(*tasks, **kwargs):
    return {"text": "Deliver the scoped objective", "tasks": list(tasks), **kwargs}


@pytest.fixture
def db(tmp_path):
    state = State(tmp_path / "plans.db")
    state.register_project("project", tmp_path)
    try:
        yield state, Plans(state)
    finally:
        state.close()


def submit(plans, *tasks, **kwargs):
    return plans.submit("project", plan(*tasks, **kwargs), "request", PROFILES)


def start(state, description, task_id):
    job = next(t["job_id"] for t in description["tasks"] if t["id"] == task_id)
    attempt = state.acquire_grant(job, "codex", "m", "coordinator", "coordinator-run")
    assert attempt
    return state.claim_grant(
        attempt["id"],
        attempt["generation"],
        "thread-" + attempt["id"],
        "run-" + attempt["id"],
        "task-" + attempt["id"],
    )


def report(attempt, status="succeeded", **kwargs):
    return {
        "generation": attempt["generation"],
        "run_id": attempt["run_id"],
        "status": status,
        "evidence": {
            "criteria": [
                {"id": "proof", "status": "passed", "reference": "tests/result.json"}
            ]
        },
        **kwargs,
    }


def finish(state, plans, attempt, status="succeeded"):
    evidence = report(attempt, status)
    plans.validate_report(attempt, evidence)
    state.set_meta(f"execution_report:{attempt['id']}", evidence)
    assert state.record_execution(
        attempt["id"],
        attempt["generation"],
        attempt["run_id"],
        status,
        tree_confirmed=True,
        pending_children=False,
    )


def test_plan_topological_admission_and_contract_stages(db):
    state, plans = db
    result = submit(
        plans,
        task(
            "review",
            kind="review",
            stage="review",
            depends_on=["build"],
            write_paths=[],
        ),
        task("build"),
    )
    assert result["ready"] == ["build"]
    assert result["tasks"][0]["state"] == "waiting_dependencies"
    assert result["stages"]["deployment"] == "not_requested"
    review = result["tasks"][0]["job_id"]
    assert state.acquire_grant(review, "codex", "m", "parent", "run") is None
    build = start(state, result, "build")
    finish(state, plans, build)
    partial = plans.describe(result["objective_id"])
    assert partial["ready"] == ["review"]
    assert partial["stages"]["implementation"] == "succeeded"
    assert partial["stages"]["review"] == "pending"
    assert not partial["delivery_contract_satisfied"]
    finish(state, plans, start(state, result, "review"))
    final = plans.describe(result["objective_id"])
    assert final["status"] == "succeeded"
    assert final["delivery_contract_satisfied"]
    assert state.get_objective(result["objective_id"])["status"] == "succeeded"
    assert final["tasks"][1]["attempts"][0]["link"] == (
        "t3-thread://v1/" + build["thread_id"]
    )


@pytest.mark.parametrize(
    "tasks",
    [
        [task("same"), task("same")],
        [task(depends_on=["missing"])],
        [task(depends_on=["build"])],
        [task("a", depends_on=["b"]), task("b", depends_on=["a"])],
        [task("a"), task("b", depends_on=["a", "a"])],
    ],
)
def test_invalid_dag_leaves_no_objective_or_jobs(db, tasks):
    state, plans = db
    with pytest.raises(ValidationError):
        submit(plans, *tasks)
    assert state.list_objectives() == []
    assert state.list_jobs() == []


@pytest.mark.parametrize(
    "path",
    [
        "../outside",
        "/absolute",
        "./src",
        "src//a",
        "src/../a",
        "src/",
        ".",
        "a\\b",
        "src/*",
        "src/[a]",
        "src/?",
        "a\x00b",
        "C:drive",
    ],
)
def test_task_rejects_noncanonical_writer_paths(path):
    with pytest.raises(ValidationError):
        TaskSpec.model_validate(task(write_paths=[path]))


def test_task_normalizes_paths_and_checks_reserved_payload_keys():
    assert TaskSpec.model_validate(task(write_paths=["src", "src"])).write_paths == [
        "src"
    ]
    for key in ("task_spec", "kind", "text", "project_id", "objective_id"):
        with pytest.raises(ValidationError, match="reserved"):
            TaskSpec.model_validate(task(payload={key: "override"}))


def test_memory_requires_explicit_operation_and_consistent_stage(db):
    state, plans = db
    with pytest.raises(ValueError, match="memory_operation_required"):
        submit(plans, task(kind="memory", stage="memory"))
    with pytest.raises(ValidationError, match="must agree"):
        TaskSpec.model_validate(task(kind="memory"))
    assert state.list_jobs() == []


def test_submit_atomic_on_enqueue_failure(db, monkeypatch):
    state, plans = db
    enqueue = state.enqueue_job
    calls = 0

    def fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise StateError("injected storage failure")
        return enqueue(*args, **kwargs)

    monkeypatch.setattr(state, "enqueue_job", fail_second)
    with pytest.raises(StateError, match="storage failure"):
        submit(plans, task("a"), task("b", depends_on=["a"]))
    assert not state.list_objectives()
    assert not state.list_jobs()
    assert (
        state.connection.execute("SELECT count(*) FROM objective_plans").fetchone()[0]
        == 0
    )


def test_lost_submission_response_dedupes_across_restart(db):
    state, plans = db
    first = submit(plans, task())
    reopened = State(state.path)
    try:
        second = submit(Plans(reopened), task())
        assert second == first
        assert len(reopened.list_objectives()) == 1
        assert len(reopened.list_jobs()) == 1
        with pytest.raises(StateError, match="plan_dedupe_conflict"):
            submit(Plans(reopened), task(brief="Changed after a lost response"))
    finally:
        reopened.close()


def test_concurrent_duplicate_submission_creates_one_plan(db):
    state, _ = db
    barrier = Barrier(2)

    def submit_on_connection(_):
        connection = State(state.path)
        try:
            plans = Plans(connection)
            barrier.wait(timeout=5)
            return submit(plans, task())["objective_id"]
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        ids = list(executor.map(submit_on_connection, range(2)))
    assert ids[0] == ids[1]
    assert len(state.list_jobs()) == 1


def test_submission_dedupe_cannot_cross_project_or_unplanned_objective(db):
    state, plans = db
    state.register_project("other", "/tmp/other")
    submit(plans, task())
    with pytest.raises(DedupeConflict):
        plans.submit("other", plan(task()), "request", PROFILES)
    legacy = state.submit_objective("project", "legacy", dedupe_key="legacy")
    state.enqueue_job(legacy["id"], "implementation", {})
    with pytest.raises(StateError, match="unplanned_jobs"):
        plans.submit("project", plan(task(), text="legacy"), "legacy", PROFILES)
    assert len(state.list_jobs()) == 2


def test_submission_rejects_unknown_profile_and_empty_dedupe(db):
    state, plans = db
    with pytest.raises(StateError, match="unknown_job_kind"):
        submit(plans, task(kind="paid-new-provider"))
    with pytest.raises(StateError, match="dedupe_key_required"):
        plans.submit("project", plan(task()), "", PROFILES)
    assert not state.list_objectives()


def test_memory_success_never_completes_pending_implementation_or_acceptance(db):
    state, plans = db
    result = submit(
        plans,
        task(
            "checkpoint",
            kind="memory",
            stage="memory",
            payload={"memory_operation": "lint"},
        ),
        task("build", depends_on=["checkpoint"]),
        task("accept", stage="acceptance", write_paths=[], depends_on=["build"]),
    )
    finish(state, plans, start(state, result, "checkpoint"))
    status = plans.describe(result["objective_id"])
    assert status["stages"]["memory"] == "succeeded"
    assert status["stages"]["implementation"] == "pending"
    assert status["stages"]["acceptance"] == "pending"
    assert status["status"] != "succeeded"
    assert not status["delivery_contract_satisfied"]
    assert status["ready"] == ["build"]
    finish(state, plans, start(state, result, "build"))
    assert not plans.describe(result["objective_id"])["delivery_contract_satisfied"]
    finish(state, plans, start(state, result, "accept"))
    assert plans.describe(result["objective_id"])["delivery_contract_satisfied"]


@pytest.mark.parametrize(
    "evidence",
    [
        {},
        {"criteria": "passed"},
        {"criteria": []},
        {"criteria": [{"id": "proof", "status": "passed"}]},
        {"criteria": [{"id": "proof", "status": "failed", "reference": "failing.log"}]},
        {"criteria": [{"id": "proof", "status": "passed", "reference": " "}]},
        {"criteria": [{"id": "wrong", "status": "passed", "reference": "evidence"}]},
        {"criteria": [None]},
        {"criteria": [{"id": ["proof"], "status": "passed", "reference": "evidence"}]},
        {
            "criteria": [{"id": "proof", "status": "passed", "reference": "evidence"}]
            * 2
        },
    ],
)
def test_success_requires_exact_passing_criterion_receipts(db, evidence):
    state, plans = db
    result = submit(plans, task())
    attempt = start(state, result, "build")
    with pytest.raises(StateError):
        plans.validate_report(attempt, report(attempt, evidence=evidence))
    assert state.lease(attempt["id"])
    assert state.get_job(attempt["job_id"])["state"] == "running"


@pytest.mark.parametrize(
    "stage,kind", [("publication", "push"), ("deployment", "deploy")]
)
def test_publication_and_deployment_require_verified_matching_operation(
    db, stage, kind
):
    state, plans = db
    result = submit(plans, task(stage=stage))
    attempt = start(state, result, "build")
    receipts = report(attempt)
    with pytest.raises(StateError, match="delivery_operation_not_confirmed"):
        plans.validate_report(attempt, receipts)
    operations = Operations(state)
    expected = {
        "commit": "abc123",
        "target": "authorized-fixture",
        "artifact": "fixture",
    }
    operations.prepare(
        "delivery",
        attempt["id"],
        kind,
        expected,
        generation=attempt["generation"],
        checks_passed=True,
    )
    operations.mark_dispatched("delivery")
    # A worker's unverified report does not prove publication or deployment.
    operations.report("delivery", dict(expected, healthy=True))
    with pytest.raises(StateError, match="delivery_operation_not_confirmed"):
        plans.validate_report(attempt, receipts)
    operations.report("delivery", dict(expected, healthy=True), verified=True)
    plans.validate_report(attempt, receipts)
    finish(state, plans, attempt)
    assert plans.describe(result["objective_id"])["stages"][stage] == "succeeded"


def test_followup_is_atomic_scoped_idempotent_and_bounded(db):
    state, plans = db
    result = submit(plans, task(), max_followups=1)
    attempt = start(state, result, "build")
    followup = task("fix", write_paths=["src/fix.py"], depends_on=["build"])
    extended = plans.extend(attempt, followup, PROFILES)
    assert extended["followups"] == 1
    assert extended["tasks"][1]["state"] == "waiting_dependencies"
    assert plans.extend(attempt, copy.deepcopy(followup), PROFILES) == extended
    assert len(state.list_jobs()) == 2
    with pytest.raises(StateError, match="followup_id_conflict"):
        plans.extend(attempt, dict(followup, brief="changed"), PROFILES)
    with pytest.raises(StateError, match="followup_budget_exhausted"):
        plans.extend(attempt, task("another", depends_on=["build"]), PROFILES)
    finish(state, plans, attempt)
    assert plans.describe(result["objective_id"])["ready"] == ["fix"]


@pytest.mark.parametrize(
    "updates,error",
    [
        ({"depends_on": []}, "source_dependency"),
        ({"depends_on": ["build", "missing"]}, "source_dependency"),
        ({"write_paths": ["other"]}, "exceeds_write_scope"),
        ({"write_paths": ["*"]}, "exceeds_write_scope"),
        ({"write_paths": ["src-adjacent"]}, "exceeds_write_scope"),
        ({"stage": "publication"}, "new_authorization"),
        ({"stage": "deployment"}, "new_authorization"),
        ({"stage": "acceptance"}, "new_authorization"),
        ({"kind": "unknown"}, "new_authorization"),
    ],
)
def test_followup_cannot_expand_authorization_or_detach_source(db, updates, error):
    state, plans = db
    result = submit(plans, task())
    attempt = start(state, result, "build")
    followup = task(
        "fix",
        depends_on=["build"],
        **{k: v for k, v in updates.items() if k != "depends_on"},
    )
    followup.update(updates)
    with pytest.raises(StateError, match=error):
        plans.extend(attempt, followup, PROFILES)
    assert len(state.list_jobs()) == 1
    assert plans.get(result["objective_id"])["followups"] == 0


def test_followup_requires_live_source_and_cannot_alias_initial_task(db):
    state, plans = db
    result = submit(plans, task())
    attempt = start(state, result, "build")
    with pytest.raises(StateError, match="followup_source_mismatch"):
        plans.extend(attempt, task(), PROFILES)
    state.cancel_objective(result["objective_id"])
    with pytest.raises(StaleGrant):
        plans.extend(attempt, task("late", depends_on=["build"]), PROFILES)
    assert len(state.list_jobs()) == 1


def test_completed_attempt_cannot_extend_plan(db):
    state, plans = db
    result = submit(plans, task())
    attempt = start(state, result, "build")
    finish(state, plans, attempt)
    with pytest.raises(StaleGrant):
        plans.extend(attempt, task("late", depends_on=["build"]), PROFILES)


def test_followup_budget_persists_across_restart(db):
    state, plans = db
    result = submit(plans, task(), max_followups=1)
    attempt = start(state, result, "build")
    followup = task("fix", depends_on=["build"])
    plans.extend(attempt, followup, PROFILES)
    reopened = State(state.path)
    try:
        recovered = Plans(reopened)
        assert recovered.extend(attempt, followup, PROFILES)["followups"] == 1
        with pytest.raises(StateError, match="budget_exhausted"):
            recovered.extend(attempt, task("fix2", depends_on=["build"]), PROFILES)
    finally:
        reopened.close()


def test_retry_reuses_task_id_and_obeys_attempt_limit(db):
    state, plans = db
    result = submit(plans, task(), task("review", depends_on=["build"]))
    first = start(state, result, "build")
    finish(state, plans, first, "failed")
    blocked = plans.describe(result["objective_id"])
    assert blocked["status"] == "blocked"
    assert blocked["ready"] == []
    assert blocked["tasks"][0]["blocker"]["owner"] == "implementer"
    retried = plans.retry(result["objective_id"], "build", 2)
    assert retried["ready"] == ["build"]
    assert retried["tasks"][0]["job_id"] == first["job_id"]
    second = start(state, retried, "build")
    assert second["number"] == 2
    finish(state, plans, second, "failed")
    with pytest.raises(StateError, match="budget_exhausted"):
        plans.retry(result["objective_id"], "build", 2)
    assert plans.describe(result["objective_id"])["ready"] == []


def test_retry_cannot_duplicate_unknown_remote_execution_after_restart(db):
    state, plans = db
    result = submit(plans, task())
    attempt = start(state, result, "build")
    state.mark_uncertain(attempt["id"], "lost response")
    reopened = State(state.path)
    try:
        with pytest.raises(StateError, match="not_retryable"):
            Plans(reopened).retry(result["objective_id"], "build", 3)
        assert reopened.lease(attempt["id"])
        assert (
            reopened.connection.execute("SELECT count(*) FROM attempts").fetchone()[0]
            == 1
        )
        assert Plans(reopened).describe(result["objective_id"])["active"] == ["build"]
    finally:
        reopened.close()


def test_retry_refuses_unresolved_effect_even_if_job_was_marked_failed(db):
    state, plans = db
    result = submit(plans, task())
    attempt = start(state, result, "build")
    finish(state, plans, attempt, "failed")
    # An imported/recovered database may contain an orphaned uncertain operation.
    state.connection.execute(
        "INSERT INTO operations(id,attempt_id,kind,expected_state,state,payload_hash,created_at)"
        " VALUES('orphan',?,'push','{}','uncertain','hash',0)",
        (attempt["id"],),
    )
    with pytest.raises(StateError, match="requires_reconciliation"):
        plans.retry(result["objective_id"], "build", 3)
    assert state.get_job(attempt["job_id"])["state"] == "failed"


def test_cancel_never_advertises_or_admits_successors(db):
    state, plans = db
    result = submit(plans, task(), task("review", depends_on=["build"]))
    attempt = start(state, result, "build")
    state.cancel_objective(result["objective_id"])
    finish(state, plans, attempt)
    status = plans.describe(result["objective_id"])
    assert status["status"] == "stopped"
    assert not status["delivery_contract_satisfied"]
    assert status["ready"] == []
    assert "no successors" in status["next_action"]
    review_job = next(t["job_id"] for t in status["tasks"] if t["id"] == "review")
    assert state.acquire_grant(review_job, "codex", "m", "owner", "run") is None
    with pytest.raises(StateError, match="objective_not_retryable"):
        plans.retry(result["objective_id"], "build", 3)


@pytest.mark.parametrize("reason", ["auth_required", "quota_wait"])
def test_provider_blocker_has_owner_and_action(db, reason):
    state, plans = db
    result = submit(plans, task())
    state.connection.execute(
        "UPDATE jobs SET state=? WHERE id=?",
        (reason, result["tasks"][0]["job_id"]),
    )
    status = plans.describe(result["objective_id"])
    assert status["status"] == "blocked"
    assert status["ready"] == []
    assert status["tasks"][0]["blocker"]["reason"] == reason
    assert status["tasks"][0]["blocker"]["owner"] == "implementer"
    assert status["tasks"][0]["blocker"]["action"]


def test_backoff_is_not_advertised_as_ready(db, monkeypatch):
    state, plans = db
    result = submit(plans, task())
    monkeypatch.setattr("oraculo.plans.time.time", lambda: 100)
    state.connection.execute(
        "UPDATE jobs SET next_attempt_at=200 WHERE id=?",
        (result["tasks"][0]["job_id"],),
    )
    status = plans.describe(result["objective_id"])
    assert status["ready"] == []
    assert status["tasks"][0]["state"] == "waiting_retry"
    assert status["tasks"][0]["blocker"]["retry_at"] == 200


def test_maximum_initial_plan_plus_followups_is_bounded_and_persistent(db):
    state, plans = db
    initial = [task(f"task{i}") for i in range(32)]
    with pytest.raises(ValidationError):
        PlanSpec.model_validate(plan(*initial, task("too-many")))
    result = submit(plans, *initial)
    attempt = start(state, result, "task0")
    for i in range(4):
        result = plans.extend(
            attempt, task(f"followup{i}", depends_on=["task0"]), PROFILES
        )
    assert len(result["tasks"]) == 36
    assert len(state.list_jobs()) == 36
    with pytest.raises(StateError, match="budget_exhausted"):
        plans.extend(attempt, task("excess", depends_on=["task0"]), PROFILES)
    stored = json.loads(
        state.connection.execute("SELECT spec FROM objective_plans").fetchone()[0]
    )
    assert len(stored["tasks"]) == 36
