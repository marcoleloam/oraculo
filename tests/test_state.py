import pytest

from oraculo.state import DedupeConflict, StaleGrant, State


def prepare(state, project="p"):
    state.register_project(project, "/tmp/" + project)
    objective = state.submit_objective(project, "work")
    job = state.enqueue_job(objective["id"], "implementation", {})
    return objective, job


def test_global_grant_includes_descendants(tmp_path):
    first = State(tmp_path / "state.db")
    second = State(tmp_path / "state.db")
    _, job1 = prepare(first)
    _, job2 = prepare(second, "q")
    grant = first.acquire_grant(job1["id"], "codex", "m", "parent", "r")
    first.claim_grant(grant["id"], grant["generation"], "child", "childrun")
    assert second.acquire_grant(job2["id"], "grok", "m", "other", "r2") is None
    assert not first.record_execution(
        grant["id"],
        grant["generation"],
        "childrun",
        "succeeded",
        pending_children=True,
        tree_confirmed=True,
    )
    assert second.lease()
    assert not first.record_execution(
        grant["id"],
        grant["generation"],
        "oldrun",
        "succeeded",
        pending_children=False,
        tree_confirmed=True,
    )
    assert first.record_execution(
        grant["id"],
        grant["generation"],
        "childrun",
        "succeeded",
        pending_children=False,
        tree_confirmed=True,
    )
    assert (
        second.acquire_grant(job2["id"], "grok", "m", "other", "r2")["generation"]
        > grant["generation"]
    )
    first.close()
    second.close()


def test_cancel_invalidates_effect_grant(tmp_path):
    state = State(tmp_path / "state.db")
    objective, job = prepare(state)
    grant = state.acquire_grant(job["id"], "codex", "m", "p", "r")
    state.cancel_objective(objective["id"])
    with pytest.raises(StaleGrant):
        state.validate_grant(grant["id"], grant["generation"])
    assert state.lease()


def test_inbox_cursor_atomic_and_conflicts(tmp_path):
    state = State(tmp_path / "state.db")
    with pytest.raises(ValueError):
        state.ingest_items(
            "t",
            [{"itemId": "a", "text": "ok"}, {"itemId": "b", "textTruncated": True}],
            3,
        )
    assert state.connection.execute("SELECT count(*) FROM inbox").fetchone()[0] == 0
    assert (
        state.connection.execute("SELECT count(*) FROM thread_cursors").fetchone()[0]
        == 0
    )
    with state.transaction():
        assert state.request_begin("q", {"x": 1}) is None
        state.request_finish("q", {"ok": True})
    assert state.request_begin("q", {"x": 1}) == {"ok": True}
    with pytest.raises(DedupeConflict):
        state.request_begin("q", {"x": 2})


def test_simultaneous_coordinators_cannot_double_admit(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    path = tmp_path / "concurrent.db"
    state = State(path)
    _, first = prepare(state)
    _, second = prepare(state, "q")
    barrier = Barrier(2)

    def acquire(job_id):
        connection = State(path)
        barrier.wait(timeout=5)
        try:
            return connection.acquire_grant(job_id, "codex", "m", job_id, "run")
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        grants = list(executor.map(acquire, [first["id"], second["id"]]))
    assert sum(grant is not None for grant in grants) == 1
    assert state.connection.execute("SELECT count(*) FROM attempts").fetchone()[0] == 1


def definitive_rejection(kind="quota"):
    return {
        "definitive_no_child": True,
        "error_kind": kind,
        "tool_error": "Provider rejected delegation before creating a child",
        "client_request_id": "delegate-round-1",
    }


@pytest.mark.parametrize("kind", ["quota", "auth", "validation"])
def test_definitive_unstarted_rejection_releases_grant(tmp_path, kind):
    state = State(tmp_path / "db")
    objective, job = prepare(state)
    attempt = state.acquire_grant(job["id"], "codex", "m", "parent", "parent-run")
    state.mark_uncertain(attempt["id"], "awaiting execution identity")
    result = state.fail_unstarted(
        attempt["id"], attempt["generation"], definitive_rejection(kind)
    )
    assert result["status"] == "failed"
    assert result["raw_status"] == "not_started"
    assert state.get_job(job["id"])["state"] == "failed"
    assert state.get_objective(objective["id"])["status"] == "failed"
    assert state.lease() is None
    assert (
        state.connection.execute("SELECT kind FROM audit_events").fetchone()[0]
        == "execution_definitively_not_started"
    )
    with pytest.raises(StaleGrant):
        state.claim_grant(
            attempt["id"], attempt["generation"], "late-child", "late-run"
        )
    _, replacement_job = prepare(state, "other")
    replacement = state.acquire_grant(
        replacement_job["id"], "grok", "g", "other-parent", "r"
    )
    assert replacement["generation"] > attempt["generation"]


def test_unstarted_rejects_claimed_and_wrong_generation(tmp_path):
    state = State(tmp_path / "db")
    _, job = prepare(state)
    attempt = state.acquire_grant(job["id"], "codex", "m", "parent", "r")
    with pytest.raises(StaleGrant):
        state.fail_unstarted(
            attempt["id"], attempt["generation"] + 1, definitive_rejection()
        )
    state.claim_grant(attempt["id"], attempt["generation"], "child", "child-run")
    state.mark_uncertain(attempt["id"], "network unavailable")
    with pytest.raises(StaleGrant):
        state.fail_unstarted(
            attempt["id"], attempt["generation"], definitive_rejection()
        )
    assert state.lease()["attempt_id"] == attempt["id"]
    assert state.get_attempt(attempt["id"])["thread_id"] == "child"


@pytest.mark.parametrize(
    "proof",
    [
        {},
        {**definitive_rejection(), "definitive_no_child": False},
        {**definitive_rejection(), "error_kind": "timeout"},
        {**definitive_rejection(), "tool_error": "response lost"},
        {**definitive_rejection(), "tool_error": "timed out waiting for response"},
        {**definitive_rejection(), "client_request_id": ""},
    ],
)
def test_missing_or_ambiguous_proof_preserves_uncertain_grant(tmp_path, proof):
    from oraculo.state import StateError

    state = State(tmp_path / "db")
    _, job = prepare(state)
    attempt = state.acquire_grant(job["id"], "codex", "m", "parent", "r")
    state.mark_uncertain(attempt["id"], "response missing")
    with pytest.raises(StateError):
        state.fail_unstarted(attempt["id"], attempt["generation"], proof)
    assert state.lease()["state"] == "uncertain"
    assert state.get_job(job["id"])["state"] == "uncertain"


def test_cancel_before_delegation_with_definitive_rejection_stops(tmp_path):
    state = State(tmp_path / "db")
    objective, job = prepare(state)
    grant = state.acquire_grant(job["id"], "codex", "m", "parent", "run")
    state.cancel_objective(objective["id"])
    result = state.fail_unstarted(
        grant["id"], grant["generation"], definitive_rejection()
    )
    assert result["status"] == "stopped"
    assert state.get_objective(objective["id"])["status"] == "stopped"
    assert state.get_job(job["id"])["state"] == "stopped"
    assert state.lease() is None
