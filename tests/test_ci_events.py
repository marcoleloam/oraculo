import hashlib
import hmac

import pytest
from test_service import command

from oraculo.ci_events import CIBinding, CIEvent, CIEvents, SignedCIObserver
from oraculo.protocol import canonical_json
from oraculo.state import DedupeConflict, State, StateError

KEY = b"synthetic-ci-authentication-key-32-bytes"


def binding(**changes):
    return CIBinding(
        **(
            dict(
                repository="example/repo",
                pr=1,
                head="a" * 40,
                base="b" * 40,
                run="run",
                attempt=1,
            )
            | changes
        )
    )


def event(**changes):
    return CIEvent(
        **(
            binding().model_dump()
            | dict(
                event_id="e",
                sequence=1,
                occurred_at=10000,
                conclusion="passed",
                diagnostic="synthetic missing executable",
            )
            | changes
        )
    )


def observer(value, *, valid=True):
    async def read(bound):
        raw = canonical_json(value.model_dump()).encode()
        return raw, hmac.new(
            KEY if valid else b"wrong", raw, hashlib.sha256
        ).hexdigest()

    return SignedCIObserver(read, lambda ref: KEY, "ref:test-ci-key", "synthetic-host")


def job(state, b=None):
    state.register_project("p", "/tmp/synthetic")
    objective = state.submit_objective("p", "CI-bound work")
    return state.enqueue_job(
        objective["id"], "implementation", {"ci_binding": (b or binding()).model_dump()}
    ), objective


async def test_authenticated_event_is_durable_atomic_and_replayed_once(tmp_path):
    state = State(tmp_path / "db")
    j, _ = job(state)
    events = CIEvents(state)
    assert state.admission_blockers(j)[0]["reason"] == "ci_evidence_pending"
    r = await events.observe("p", binding(), observer(event()))
    assert r["disposition"] == "accepted"
    assert len(list(state.connection.execute("SELECT * FROM outbox"))) == 1
    assert len(list(state.connection.execute("SELECT * FROM inbox"))) == 1
    state.close()
    state = State(tmp_path / "db")
    events = CIEvents(state)
    assert (await events.observe("p", binding(), observer(event())))["replay"]
    assert len(list(state.connection.execute("SELECT * FROM outbox"))) == 1
    assert events.passed("p", binding().model_dump())
    assert state.acquire_grant(j["id"], "codex", "m", "owner", "run")


@pytest.mark.parametrize("valid", [False, None])
async def test_payload_auth_boolean_or_unsigned_host_cannot_open_gate(tmp_path, valid):
    state = State(tmp_path / "db")
    j, _ = job(state)
    with pytest.raises(StateError):
        await CIEvents(state).observe(
            "p",
            binding(),
            observer(event(), valid=False)
            if valid is False
            else {"authenticated": True, "event": event().model_dump()},
        )
    assert state.acquire_grant(j["id"], "codex", "m", "owner", "run") is None
    assert list(state.connection.execute("SELECT * FROM outbox")) == []


async def test_duplicate_conflicting_old_sha_out_of_order_attempt_and_cancel(tmp_path):
    state = State(tmp_path / "db")
    j, obj = job(state)
    events = CIEvents(state)
    await events.observe(
        "p", binding(), observer(event(sequence=10, conclusion="failed"))
    )
    with pytest.raises(DedupeConflict):
        await events.observe(
            "p", binding(), observer(event(sequence=10, conclusion="passed"))
        )
    assert (
        await events.observe(
            "p", binding(), observer(event(event_id="older", sequence=9))
        )
    )["disposition"] == "stale"
    assert not events.passed("p", binding().model_dump())
    assert (
        await events.observe(
            "p",
            binding(),
            observer(event(event_id="old-sha", sequence=11, head="c" * 40)),
        )
    )["disposition"] == "binding_mismatch"
    assert not events.passed("p", binding().model_dump())
    await events.observe(
        "p",
        binding(),
        observer(event(event_id="cancel", sequence=12, conclusion="cancelled")),
    )
    assert state.acquire_grant(j["id"], "codex", "m", "owner", "run") is None
    state.cancel_objective(obj["id"])
    await events.observe(
        "p", binding(), observer(event(event_id="late-pass", sequence=13))
    )
    assert state.acquire_grant(j["id"], "codex", "m", "owner", "run") is None
    assert len(state.list_jobs()) == 1


async def test_service_only_reads_trusted_observer_no_event_payload(running_service):
    service = running_service
    objective = service.state.submit_objective("alpha", "CI")
    j = service.state.enqueue_job(
        objective["id"], "implementation", {"ci_binding": binding().model_dump()}
    )
    origin = {"thread_id": "coordinator", "run_id": "run"}
    denied = await command(
        service, "ci.observe", {"job_id": j["id"], "authenticated": True}, origin=origin
    )
    assert not denied["ok"]
    missing = await command(service, "ci.observe", {"job_id": j["id"]}, origin=origin)
    assert not missing["ok"]
    service.ci_observer = observer(event())
    first = await command(service, "ci.observe", {"job_id": j["id"]}, origin=origin)
    assert first["ok"] and first["data"]["disposition"] == "accepted"
    replay = await command(service, "ci.observe", {"job_id": j["id"]}, origin=origin)
    assert replay["data"]["replay"]


async def test_provider_event_identity_is_scoped_by_project_and_binding(tmp_path):
    state = State(tmp_path / "db")
    events = CIEvents(state)
    a = await events.observe("a", binding(), observer(event()))
    b = await events.observe("b", binding(), observer(event()))
    assert a["disposition"] == b["disposition"] == "accepted"
    assert not b.get("replay")
    new_binding = binding(head="c" * 40, run="new-run", attempt=2)
    changed = event(head="c" * 40, run="new-run", attempt=2, sequence=2)
    c = await events.observe("a", new_binding, observer(changed))
    assert c["disposition"] == "accepted" and not c.get("replay")
    assert not events.passed("a", binding().model_dump())
    assert events.passed("b", binding().model_dump())
    assert events.passed("a", new_binding.model_dump())
