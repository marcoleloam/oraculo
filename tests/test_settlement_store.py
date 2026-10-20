from copy import deepcopy

import pytest

from oraculo.plans import Plans
from oraculo.settlement_store import SettlementStore
from oraculo.state import DedupeConflict, State, StateError


@pytest.fixture
def prepared(tmp_path):
    state = State(tmp_path / "state.db")
    state.register_project("project", tmp_path / "project")
    objective = state.submit_objective("project", "Deliver generic change")
    job = state.enqueue_job(objective["id"], "implementation", {})
    payload = {
        "id": "binding",
        "dedupe_key": "delivery",
        "objective_id": objective["id"],
        "task_id": job["id"],
        "job_id": job["id"],
        "thread_id": "thread",
        "run_id": "run",
        "policy": "all_merged",
        "pull_requests": [
            {
                "host": "github.com",
                "repository": "example/product",
                "number": 1,
                "base_branch": "develop",
                "head_sha": "a" * 40,
            }
        ],
        "authorization": {
            "actor": "operator",
            "reason": "Close this dedicated delivery after verification",
            "scope": "dedicated_task",
            "covers_all_objectives": True,
        },
    }
    yield state, payload
    state.close()


def test_additive_migration_preserves_legacy_live_grant(prepared):
    state, payload = prepared
    attempt = state.acquire_grant(
        payload["job_id"], "codex", "model", "owner", "owner-run"
    )
    old_lease = state.lease()
    assert not state._one("SELECT version FROM schema_migrations WHERE version=3")
    SettlementStore(state)
    assert state.lease() == old_lease
    assert state.get_attempt(attempt["id"]) == attempt
    assert state.get_objective(payload["objective_id"])["status"] == "running"
    SettlementStore(state)
    versions = state.connection.execute(
        "SELECT version FROM schema_migrations"
    ).fetchall()
    assert [r[0] for r in versions] == [1, 2, 3]


def test_binding_is_idempotent_and_detached_from_callers(prepared):
    state, payload = prepared
    store = SettlementStore(state)
    original = store.bind("project", payload)
    assert store.bind("project", deepcopy(payload)) == original
    payload["pull_requests"][0]["number"] = 2
    original["binding"]["authorization"]["reason"] = "modified externally"
    assert store.get("binding")["pull_requests"][0]["number"] == 1
    assert store.get("binding")["authorization"]["reason"] != "modified externally"
    assert len(store.events("binding")) == 1
    assert len(store.list("project")) == 1
    assert store.list("other") == []


@pytest.mark.parametrize(
    "change",
    [
        {"run_id": "other-run"},
        {"thread_id": "other-thread"},
        {"id": "other-id"},
        {"dedupe_key": "other-key"},
    ],
)
def test_conflicting_identity_is_never_rebound(prepared, change):
    state, payload = prepared
    store = SettlementStore(state)
    store.bind("project", payload)
    payload.update(change)
    with pytest.raises(DedupeConflict):
        store.bind("project", payload)
    assert len(store.list()) == 1


def test_reordered_stack_is_same_complete_set(prepared):
    state, payload = prepared
    payload["pull_requests"].append(
        {
            "host": "github.com",
            "repository": "example/product",
            "number": 2,
            "base_branch": "develop",
        }
    )
    store = SettlementStore(state)
    first = store.bind("project", payload)
    payload["pull_requests"].reverse()
    assert store.bind("project", payload) == first
    payload["pull_requests"].pop()
    with pytest.raises(DedupeConflict):
        store.bind("project", payload)


def test_permanent_coordinator_for_any_project_is_forbidden(prepared):
    state, payload = prepared
    state.register_project("other", "/tmp/other")
    state.set_coordinator("other", payload["thread_id"])
    with pytest.raises(StateError, match="permanent coordinator"):
        SettlementStore(state).bind("project", payload)


def test_objective_job_and_execution_identity_are_verified(prepared):
    state, payload = prepared
    state.register_project("other", "/tmp/other")
    other = state.submit_objective("other", "Other objective")
    other_job = state.enqueue_job(other["id"], "implementation", {})
    store = SettlementStore(state)
    with pytest.raises(StateError, match="exact project"):
        store.bind("other", payload)
    with pytest.raises(StateError, match="job must belong"):
        store.bind("project", {**payload, "job_id": other_job["id"]})
    attempt = state.acquire_grant(
        other_job["id"], "codex", "model", "owner", "owner-run"
    )
    state.claim_grant(
        attempt["id"], attempt["generation"], "thread", "run", "delegated-task"
    )
    with pytest.raises(StateError, match="execution identity"):
        store.bind("project", payload)


@pytest.mark.parametrize(
    "change",
    [
        {"authorization": None},
        {
            "authorization": {
                "actor": "operator",
                "reason": "done",
                "scope": "coordinator",
            }
        },
        {"policy": "first_merged"},
        {"pull_requests": []},
        {"run_id": ""},
        {"thread_id": "  thread"},
        {"permanent_coordinator": True},
    ],
)
def test_missing_or_unsafe_binding_information_fails_closed(prepared, change):
    state, payload = prepared
    with pytest.raises(StateError):
        SettlementStore(state).bind("project", {**payload, **change})


@pytest.mark.parametrize(
    "change",
    [
        {"number": True},
        {"number": -1},
        {"head_sha": "reported merged"},
        {"base_branch": ""},
        {"url": "https://example.invalid/pull/1"},
    ],
)
def test_pull_request_identity_validation(prepared, change):
    state, payload = prepared
    payload["pull_requests"][0].update(change)
    with pytest.raises(StateError):
        SettlementStore(state).bind("project", payload)


def test_duplicate_pr_and_uncovered_objectives_rejected(prepared):
    state, payload = prepared
    store = SettlementStore(state)
    payload["pull_requests"] *= 2
    with pytest.raises(StateError, match="duplicate pull request"):
        store.bind("project", payload)
    payload["pull_requests"] = payload["pull_requests"][:1]
    payload["authorization"]["covers_all_objectives"] = False
    with pytest.raises(StateError, match="covering all objectives"):
        store.bind("project", payload)


def test_history_and_claim_survive_connection_restart(prepared):
    state, payload = prepared
    store = SettlementStore(state)
    store.bind("project", payload)
    store.record("binding", "blocked", {"merged": False}, "PR remains open")
    assert store.claim_request("binding", {"merged": True})
    store.record("binding", "uncertain", {"response_lost": True}, "transport timeout")
    second_state = State(state.path)
    try:
        recovered = SettlementStore(second_state)
        assert recovered.get("binding")["request_claimed"] is True
        assert recovered.get("binding")["last_error"] == "transport timeout"
        assert not recovered.claim_request("binding", {"retry": True})
        recovered.record("binding", "confirmed", {"settled": True})
        recovered.record("binding", "reopened", {"settled": False, "new_message": True})
        assert not recovered.claim_request("binding", {})
        assert [event["state"] for event in recovered.events("binding")] == [
            "pending",
            "blocked",
            "requested",
            "uncertain",
            "confirmed",
            "reopened",
        ]
        assert recovered.get("binding")["last_error"] is None
    finally:
        second_state.close()


def test_second_connection_cannot_duplicate_request(prepared):
    state, payload = prepared
    first = SettlementStore(state)
    first.bind("project", payload)
    second_state = State(state.path)
    try:
        second = SettlementStore(second_state)
        assert first.claim_request("binding", {"verified": True})
        assert not second.claim_request("binding", {"verified": True})
        assert len(first.events("binding")) == 2
    finally:
        second_state.close()


def test_auto_settled_observation_never_creates_request(prepared):
    state, payload = prepared
    store = SettlementStore(state)
    store.bind("project", payload)
    store.record("binding", "confirmed", {"settled": True, "source": "t3_read"})
    assert not store.claim_request("binding", {})
    assert store.get("binding")["request_claimed"] is False
    with pytest.raises(StateError, match="claimed"):
        store.record("binding", "requested")
    with pytest.raises(StateError, match="invalid settlement state"):
        store.record("binding", "merged")


def test_arbitrary_task_cannot_use_existing_job_as_identity(prepared):
    state, payload = prepared
    payload["task_id"] = "invented"
    with pytest.raises(StateError, match="resolve"):
        SettlementStore(state).bind("project", payload)


def test_bound_job_requires_its_actual_execution(prepared):
    state, payload = prepared
    attempt = state.acquire_grant(
        payload["job_id"], "codex", "model", "owner", "owner-run"
    )
    state.claim_grant(
        attempt["id"], attempt["generation"], "actual-thread", "actual-run", "delegated"
    )
    store = SettlementStore(state)
    with pytest.raises(StateError, match="actual job execution"):
        store.bind("project", payload)
    payload.update(thread_id="actual-thread", run_id="actual-run")
    assert store.bind("project", payload)["job_id"] == payload["job_id"]


def test_delegated_task_infers_job(prepared):
    state, payload = prepared
    attempt = state.acquire_grant(
        payload["job_id"], "codex", "model", "owner", "owner-run"
    )
    state.claim_grant(
        attempt["id"], attempt["generation"], "thread", "run", "delegated"
    )
    expected_job = payload.pop("job_id")
    payload["task_id"] = "delegated"
    assert SettlementStore(state).bind("project", payload)["job_id"] == expected_job


def test_plan_task_names_can_repeat_across_objectives(prepared):
    state, payload = prepared
    plans = Plans(state)
    store = SettlementStore(state)
    for suffix in ("one", "two"):
        plan = plans.submit(
            "project",
            {
                "text": "Generic objective " + suffix,
                "tasks": [
                    {
                        "id": "implement",
                        "owner": "worker",
                        "brief": "Generic implementation",
                        "criteria": [{"id": "pass", "description": "Verified result"}],
                    }
                ],
            },
            "plan-" + suffix,
            {"implementation": {}},
        )
        objective_id = plan["objective_id"]
        binding = {
            **payload,
            "id": suffix,
            "dedupe_key": suffix,
            "objective_id": objective_id,
            "task_id": "implement",
            "thread_id": "thread-" + suffix,
        }
        binding.pop("job_id")
        result = store.bind("project", binding)
        assert result["job_id"] == plans.get(objective_id)["job_ids"]["implement"]
    assert len(store.list()) == 2


def test_identical_observations_do_not_grow_history(prepared):
    state, payload = prepared
    store = SettlementStore(state)
    store.bind("project", payload)
    first = store.record("binding", "blocked", {"merged": False}, "still open")
    assert store.record("binding", "blocked", {"merged": False}, "still open") == first
    assert len(store.events("binding")) == 2
    store.record("binding", "blocked", {"merged": False}, "wrong target")
    assert len(store.events("binding")) == 3
    assert store.claim_request("binding", {"intent": "settle"})
    store.record("binding", "requested", {"settlesWhenTurnEnds": True})
    assert [event["state"] for event in store.events("binding")][-2:] == [
        "requested",
        "requested",
    ]
