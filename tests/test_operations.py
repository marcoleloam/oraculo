import pytest

from oraculo.operations import Operations
from oraculo.state import State, StateError


def prepared(tmp_path):
    state = State(tmp_path / "db")
    state.register_project("p", "/tmp/p")
    objective = state.submit_objective("p", "work")
    job = state.enqueue_job(objective["id"], "implementation", {})
    grant = state.acquire_grant(job["id"], "codex", "m", "p", "r")
    return state, grant, Operations(state)


async def test_lost_deploy_response(tmp_path):
    state, grant, operations = prepared(tmp_path)
    expected = {"commit": "abc", "target": "test", "artifact": "digest"}
    operations.prepare(
        "op",
        grant["id"],
        "deploy",
        expected,
        generation=grant["generation"],
        checks_passed=True,
    )
    applied = []

    async def effect():
        applied.append(expected)
        raise TimeoutError("remote applied, response lost")

    with pytest.raises(TimeoutError):
        await operations.dispatch("op", effect)
    assert operations.get("op")["state"] == "uncertain"
    await operations.dispatch("op", effect)
    assert len(applied) == 1
    assert state.lease()
    assert (
        await operations.reconcile("op", lambda target: dict(applied[0], healthy=True))
    )["state"] == "confirmed"


def test_failed_checks_block_delivery(tmp_path):
    _, grant, operations = prepared(tmp_path)
    with pytest.raises(StateError):
        operations.prepare(
            "op",
            grant["id"],
            "deploy",
            {"commit": "a", "target": "test", "artifact": "x"},
            generation=grant["generation"],
            checks_passed=False,
        )
    assert operations.get("op") is None
    operations.prepare(
        "op",
        grant["id"],
        "deploy",
        {"commit": "a", "target": "test", "artifact": "x"},
        generation=grant["generation"],
        checks_passed=True,
    )
    assert operations.report("op", {"failed": True}, verified=True)["state"] == "failed"


def test_failed_deploy_cannot_complete_successfully(tmp_path):
    state, grant, operations = prepared(tmp_path)
    state.claim_grant(grant["id"], grant["generation"], "child", "run")
    operations.prepare(
        "op",
        grant["id"],
        "deploy",
        {"commit": "a", "target": "test", "artifact": "x"},
        generation=grant["generation"],
        checks_passed=True,
    )
    operations.report("op", {"failed": True}, verified=True)
    assert state.record_execution(
        grant["id"],
        grant["generation"],
        "run",
        "succeeded",
        pending_children=False,
        tree_confirmed=True,
    )
    assert state.get_job(grant["job_id"])["state"] == "failed"
