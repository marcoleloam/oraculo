import asyncio
import copy
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import httpx2
import pytest
from pydantic import ValidationError

from oraculo.config import DecisionsConfig, DecisionsTariff
from oraculo.decisions import (
    CAUSES,
    PREFIX,
    Decisions,
    HTTPDecisionTransport,
    Snapshot,
    triage_request,
)
from oraculo.state import State, StateError

FIXTURE = Path(__file__).parent / "fixtures/decisions/choice.json"


def answer():
    return json.loads(FIXTURE.read_text())


def config(**changes):
    return DecisionsConfig(
        enabled=True,
        mode="shadow",
        daily_budget_microusd=1000000,
        run_budget_microusd=1000000,
        credential_reference="ref:synthetic",
        tariff=DecisionsTariff(
            version="test-upper-v1",
            authorization_reference="ref:test-approval",
            bound_reference="ref:test-byte-ceiling",
            input_nanousd=1000,
            multiplier_milli=1000,
            framing_tokens=1024,
        ),
        **changes,
    )


def snapshot(**changes):
    d = dict(
        project="p",
        repository="example/repo",
        head="a" * 40,
        source="a" * 40,
        task="t",
        attempt="a",
        generation=1,
        event="e",
        ci_run="run",
        ci_attempt=1,
        revision=1,
        state_hash="1" * 64,
        policy_hash="2" * 64,
        catalog_hash="3" * 64,
    )
    d.update(changes)
    return Snapshot(**d)


class Clock:
    def __init__(self):
        self.now = 10000.0

    def __call__(self):
        return self.now


class Transport:
    def __init__(self, value=None, status=200, effect=None):
        self.calls = []
        self.value = value if value is not None else answer()
        self.status, self.effect = status, effect

    async def post(self, path, body, **kwargs):
        self.calls.append((path, copy.deepcopy(body), kwargs))
        if self.effect:
            result = self.effect()
            if hasattr(result, "__await__"):
                await result
        return self.status, self.value


async def advise(engine, snap=None):
    snap = snap or snapshot()
    return await engine.advise(
        snap,
        "Synthetic runner lacks an executable",
        lambda: snap.model_dump(mode="json"),
    )


@pytest.mark.parametrize(
    "setting",
    [
        DecisionsConfig(),
        config().model_copy(update={"enabled": False}),
        config().model_copy(update={"daily_budget_microusd": 0}),
        config().model_copy(update={"run_budget_microusd": 0}),
        config().model_copy(update={"tariff": None}),
        config().model_copy(update={"mode": "off"}),
    ],
)
async def test_default_budget_and_financial_contract_never_send(tmp_path, setting):
    state, transport = State(tmp_path / "state"), Transport()
    assert (await advise(Decisions(state, setting, transport=transport)))[
        "state"
    ] == "unknown"
    assert transport.calls == []
    assert not (state.get_meta(PREFIX + "book") or {}).get("unknown_cost")


async def test_contract_usage_accounting_and_replay_after_restart(tmp_path):
    state, t = State(tmp_path / "state"), Transport()
    engine = Decisions(state, config(), transport=t)
    r = await advise(engine)
    assert r["state"] == "answered" and r["recommendation"] == "environment"
    assert r["cost_microusd"] == 351
    assert r["binding"] is False
    _, body, _ = t.calls[0]
    assert body["model"] == "gpt-6-luna"
    assert body["questions"][0]["choices"] == [
        {"value": c, "description": c.replace("_", " ")} for c in CAUSES
    ]
    assert t.calls[0][0] == "/v1/decisions"
    state.close()
    restarted = Decisions(State(tmp_path / "state"), config(), transport=t)
    assert (await advise(restarted))["replay"]
    assert len(t.calls) == 1
    entry = restarted.entries()[0]
    assert entry["charge"] < entry["reserved"]
    assert entry["state"] == "answered" and entry["request_hash"]


async def test_concurrent_workers_reserve_before_io_and_do_not_hold_transaction(
    tmp_path,
):
    path = tmp_path / "state"
    state = State(path)
    other = State(path)
    entered, release = asyncio.Event(), asyncio.Event()

    async def pause():
        assert not state.connection.in_transaction
        assert not other.connection.in_transaction
        assert len(Decisions(other, config()).entries()) == 1
        entered.set()
        await release.wait()

    transport = Transport(effect=pause)
    a, b = (
        Decisions(state, config(), transport=transport),
        Decisions(other, config(), transport=transport),
    )
    running = asyncio.create_task(advise(a))
    await entered.wait()
    replay = await advise(b)
    assert replay["state"] == "unknown"
    release.set()
    assert (await running)["state"] == "answered"
    assert len(transport.calls) == 1


def test_process_style_writer_race_uses_sqlite_cas(tmp_path):
    path = tmp_path / "state"
    State(path).close()
    barrier = Barrier(8)

    def worker(index):
        state = State(path)
        barrier.wait(timeout=20)
        won = state.compare_meta("cas:test", None, index)
        state.close()
        return won

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(worker, range(8))) == 1


@pytest.mark.parametrize("phase", ["intent", "sent"])
async def test_crash_persistent_intent_never_resends(tmp_path, phase):
    state, t, clock = State(tmp_path / "state"), Transport(), Clock()
    a = Decisions(state, config(), transport=t, clock=clock, monotonic=clock)
    await advise(a)
    entry = a.entries()[0]
    entry.update(state=phase, charge=entry["reserved"])
    entry.pop("result", None)
    state.set_meta(PREFIX + "entry:" + entry["key"], entry)
    state.close()
    clock.now += 10
    b = Decisions(
        State(tmp_path / "state"), config(), transport=t, clock=clock, monotonic=clock
    )
    assert (await advise(b))["state"] == "unknown"
    assert len(t.calls) == 1
    assert (await advise(b, snapshot(event="new")))["reason"] == "unknown_cost"
    assert len(t.calls) == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("head", "b" * 40),
        ("attempt", "new"),
        ("ci_attempt", 2),
        ("policy_hash", "4" * 64),
        ("catalog_hash", "5" * 64),
        ("revision", 2),
        ("event", "new"),
        ("state_hash", "6" * 64),
    ],
)
async def test_snapshot_mutated_during_api_never_consumes(tmp_path, field, value):
    state = State(tmp_path / "state")
    s = snapshot()
    current = s.model_dump(mode="json")

    def change():
        current[field] = value

    t = Transport(effect=change)
    r = await Decisions(state, config(), transport=t).advise(
        s, "synthetic", lambda: current
    )
    assert r["state"] == "unknown" and r["reason"] == "snapshot_changed"
    assert state.get_meta(PREFIX + "book")["unknown_cost"]


async def test_ttl_and_late_response_consumption_deadline(tmp_path):
    clock = Clock()
    state = State(tmp_path / "state")
    t = Transport()
    engine = Decisions(state, config(), transport=t, clock=clock, monotonic=clock)
    assert (await advise(engine))["state"] == "answered"
    clock.now += 300
    assert (await advise(engine))["reason"] == "expired"
    assert len(t.calls) == 1

    def late():
        clock.now += 3.001

    engine.transport = Transport(effect=late)
    r = await advise(engine, snapshot(event="late"))
    assert r["reason"] == "deadline" and r["state"] == "unknown"
    assert all(
        e["charge"] == e["reserved"]
        for e in engine.entries()
        if e["state"] == "unknown"
    )


async def test_actual_timeout_is_bounded_and_never_retries(tmp_path):
    async def wait():
        await asyncio.sleep(1)

    c = config().model_copy(update={"deadline_seconds": 0.01})
    t = Transport(effect=wait)
    engine = Decisions(State(tmp_path / "state"), c, transport=t)
    assert (await advise(engine))["state"] == "unknown"
    assert (await advise(engine))["state"] == "unknown"
    assert len(t.calls) == 1


@pytest.mark.parametrize("status", [401, 403, 429, 500, 502, 503, 504, 301])
async def test_http_failures_preserve_reserve_block_next_and_redact(tmp_path, status):
    t = Transport(status=status, value={"error": "secret that must not persist"})
    engine = Decisions(State(tmp_path / "state"), config(), transport=t)
    r = await advise(engine)
    assert r["state"] == "unknown"
    assert (await advise(engine, snapshot(event="next")))["reason"] == "unknown_cost"
    assert len(t.calls) == 1
    e = engine.entries()[0]
    assert e["charge"] == e["reserved"]
    assert "secret" not in json.dumps(e)


@pytest.mark.parametrize(
    "mutation",
    [
        "model",
        "missing_usage",
        "negative_usage",
        "bool_usage",
        "total_usage",
        "over_bound",
        "cache_usage",
        "nan",
        "probabilities",
        "refusal",
        "extra",
        "choice",
    ],
)
async def test_response_schema_refusal_and_usage_fail_closed(tmp_path, mutation):
    raw = answer()
    if mutation == "model":
        raw["model"] = "other"
    elif mutation == "missing_usage":
        raw.pop("usage")
    elif mutation == "negative_usage":
        raw["usage"]["input_tokens"] = -1
    elif mutation == "bool_usage":
        raw["usage"]["input_tokens"] = True
    elif mutation == "total_usage":
        raw["usage"]["total_tokens"] = 352
    elif mutation == "over_bound":
        raw["usage"]["input_tokens"] = raw["usage"]["total_tokens"] = 1000000
    elif mutation == "cache_usage":
        raw["usage"]["input_tokens_details"]["cached_tokens"] = 1000
    elif mutation == "nan":
        raw["answers"][0]["confidence"] = float("nan")
    elif mutation == "probabilities":
        raw["answers"][0]["probabilities"].pop()
    elif mutation == "refusal":
        raw["answers"] = [{"type": "refusal", "name": "ci_cause"}]
    elif mutation == "extra":
        raw["untrusted"] = "do something"
    elif mutation == "choice":
        raw["answers"][0]["choice"] = "deploy"
    engine = Decisions(State(tmp_path / "state"), config(), transport=Transport(raw))
    assert (await advise(engine))["state"] == "unknown"
    entry = engine.entries()[0]
    assert entry["charge"] == entry["reserved"]


async def test_kill_switch_ignores_inflight_and_survives_restart(tmp_path):
    state = State(tmp_path / "state")
    engine = Decisions(state, config())
    t = Transport(effect=lambda: engine.kill(True))
    engine.transport = t
    assert (await advise(engine))["state"] == "unknown"
    assert len(t.calls) == 1
    other = Decisions(State(tmp_path / "state"), config(), transport=t)
    assert (await advise(other, snapshot(event="next")))["reason"] == "disabled"
    assert len(t.calls) == 1


async def test_calls_and_budget_persist_after_restart(tmp_path):
    state = State(tmp_path / "state")
    t = Transport()
    c = config().model_copy(update={"max_calls": 1})
    assert (await advise(Decisions(state, c, transport=t)))["state"] == "answered"
    state.close()
    engine = Decisions(State(tmp_path / "state"), c, transport=t)
    assert (await advise(engine, snapshot(event="next")))["reason"] == "call_limit"
    assert len(t.calls) == 1


async def test_circuit_persistent_window_and_no_send(tmp_path):
    state = State(tmp_path / "state")
    clock = Clock()
    t = Transport()
    state.set_meta(
        PREFIX + "book",
        dict(
            unknown_cost=False,
            failures=[True] * 4 + [False] * 16,
            consecutive=0,
            open_until=clock() + 900,
        ),
    )
    state.close()
    engine = Decisions(
        State(tmp_path / "state"), config(), transport=t, clock=clock, monotonic=clock
    )
    assert (await advise(engine))["reason"] == "circuit_open"
    assert t.calls == []
    clock.now += 901
    assert (await advise(engine))["state"] == "answered"


@pytest.mark.parametrize("status", [200, 301, 401, 403, 429, 500])
async def test_real_http_adapter_exact_endpoint_no_retry_or_redirect(status):
    calls = []
    refs = []

    def handle(request):
        calls.append(request)
        assert str(request.url) == "https://api.openai.com/v1/decisions"
        assert request.method == "POST"
        body = json.loads(request.content)
        assert isinstance(body["questions"][0]["choices"][0], dict)
        return httpx2.Response(
            status, json=answer(), headers={"location": "https://attacker.invalid"}
        )

    def resolve(ref):
        refs.append(ref)
        return "synthetic-token"

    transport = HTTPDecisionTransport(
        resolve, http_transport=httpx2.MockTransport(handle)
    )
    result = await transport.post(
        "/v1/decisions",
        triage_request("synthetic"),
        timeout=1,
        credential_reference="ref:test",
    )
    assert result[0] == status and len(calls) == 1 and refs == ["ref:test"]


async def test_http_credential_and_raw_error_never_escape():
    def broken(ref):
        raise ValueError("raw secret")

    t = HTTPDecisionTransport(broken)
    with pytest.raises(StateError, match="^decision_transport_failed$") as e:
        await t.post(
            "/v1/decisions",
            triage_request("synthetic"),
            timeout=1,
            credential_reference="ref:test",
        )
    assert "secret" not in str(e.value)
    with pytest.raises(ValidationError):
        DecisionsConfig(credential_reference="raw-key")


async def test_default_lazy_keychain_alias_uses_fake_reader_only_after_authorized_reservation(
    tmp_path, monkeypatch
):
    import threading

    import oraculo.decisions as module

    calls = []
    loop_thread = threading.get_ident()

    def fake_reader():
        calls.append(threading.get_ident())
        return "synthetic-token"

    monkeypatch.setattr(module, "_keychain_password", fake_reader)
    monkeypatch.setattr(module.sys, "platform", "darwin")

    def handle(request):
        assert len(calls) == 1
        return httpx2.Response(200, json=answer())

    transport = HTTPDecisionTransport(http_transport=httpx2.MockTransport(handle))
    c = config().model_copy(
        update={"credential_reference": "ref:oraculo-openai-decisions"}
    )
    state = State(tmp_path / "state")
    for setting in (
        DecisionsConfig(),
        c.model_copy(update={"daily_budget_microusd": 0}),
    ):
        assert (await advise(Decisions(state, setting, transport=transport)))[
            "state"
        ] == "unknown"
        assert calls == []
    assert (await advise(Decisions(state, c, transport=transport)))[
        "state"
    ] == "answered"
    assert len(calls) == 1 and calls[0] != loop_thread
    with pytest.raises(StateError):
        await module.resolve_credential("ref:wrong-alias")
    assert len(calls) == 1


async def test_kill_on_then_off_still_invalidates_inflight_response(tmp_path):
    engine = Decisions(State(tmp_path / "state"), config())

    def toggle():
        engine.kill(True)
        engine.kill(False)

    engine.transport = Transport(effect=toggle)
    assert (await advise(engine))["state"] == "unknown"


async def test_two_inflight_keys_are_reserved_and_third_is_blocked(tmp_path):
    state = State(tmp_path / "state")
    entered = asyncio.Queue()
    release = asyncio.Event()

    async def wait():
        await entered.put(True)
        await release.wait()

    t = Transport(effect=wait)
    engine = Decisions(state, config(), transport=t)
    first = asyncio.create_task(advise(engine, snapshot(event="one")))
    await entered.get()
    second = asyncio.create_task(advise(engine, snapshot(event="two")))
    await entered.get()
    assert (await advise(engine, snapshot(event="three")))["reason"] == "concurrency"
    assert len(t.calls) == 2
    assert sum(e["charge"] for e in engine.entries()) == sum(
        e["reserved"] for e in engine.entries()
    )
    release.set()
    await asyncio.gather(first, second)


async def test_real_crash_after_post_leaves_intent_unknown_and_never_reposts(tmp_path):
    class Crash(BaseException):
        pass

    def crash():
        raise Crash()

    state = State(tmp_path / "state")
    clock = Clock()
    t = Transport(effect=crash)
    engine = Decisions(state, config(), transport=t, clock=clock, monotonic=clock)
    with pytest.raises(Crash):
        await advise(engine)
    assert engine.entries()[0]["state"] == "sent"
    state.close()
    clock.now += 4
    restarted = Decisions(
        State(tmp_path / "state"), config(), transport=t, clock=clock, monotonic=clock
    )
    assert (await advise(restarted))["state"] == "unknown"
    assert (await advise(restarted, snapshot(event="other")))[
        "reason"
    ] == "unknown_cost"
    assert restarted.entries()[0]["state"] == "unknown" and len(t.calls) == 1


async def test_spend_and_ceil_persist_and_budget_cannot_be_oversubscribed(tmp_path):
    from oraculo.protocol import canonical_json

    diagnostic = "Synthetic runner lacks an executable"
    reserve = len(canonical_json(triage_request(diagnostic)).encode()) + 1024
    c = config().model_copy(update={"daily_budget_microusd": reserve})
    t = Transport()
    state = State(tmp_path / "state")
    assert (await advise(Decisions(state, c, transport=t)))["state"] == "answered"
    state.close()
    e = Decisions(State(tmp_path / "state"), c, transport=t)
    assert (await advise(e, snapshot(event="second")))["reason"] == "budget"
    assert len(t.calls) == 1
    c.tariff.input_nanousd = 101
    c.tariff.multiplier_milli = 1001
    assert e.cost(1) == 1 and e.cost(1000) == 102


@pytest.mark.parametrize(
    "book",
    [
        dict(unknown_cost=False, failures=[True, True], consecutive=2, open_until=0),
        dict(
            unknown_cost=False,
            failures=[True] * 3 + [False] * 16,
            consecutive=0,
            open_until=0,
        ),
    ],
)
async def test_failures_open_persistent_circuit_at_declared_threshold(tmp_path, book):
    state = State(tmp_path / "state")
    clock = Clock()
    state.set_meta(PREFIX + "book", book)
    e = Decisions(
        state, config(), transport=Transport(status=503), clock=clock, monotonic=clock
    )
    assert (await advise(e))["state"] == "unknown"
    assert state.get_meta(PREFIX + "book")["open_until"] == clock() + 900


async def test_tampered_saved_response_invalidates_replay_without_resend(tmp_path):
    t = Transport()
    engine = Decisions(State(tmp_path / "state"), config(), transport=t)
    first = await advise(engine)
    Path(first["response_artifact"]["reference"]).write_text("tampered")
    replay = await advise(engine)
    assert replay["state"] == "unknown" and replay["reason"] == "artifact_changed"
    assert len(t.calls) == 1


async def test_reservation_time_is_part_of_deadline_and_expired_intent_never_posts(
    tmp_path, monkeypatch
):
    state = State(tmp_path / "state")
    clock = Clock()
    t = Transport()
    compare = state.compare_meta

    def delayed(*args, **kwargs):
        result = compare(*args, **kwargs)
        clock.now += 4
        return result

    monkeypatch.setattr(state, "compare_meta", delayed)
    e = Decisions(state, config(), transport=t, clock=clock, monotonic=clock)
    r = await advise(e)
    assert r["state"] == "unknown" and r["reason"] == "deadline" and t.calls == []


@pytest.mark.parametrize("boundary", ["response_fsync", "commit_ack"])
@pytest.mark.parametrize("change", ["deadline", "kill", "config", "snapshot"])
async def test_durable_finish_revalidates_deadline_and_all_gates_before_consumption(
    tmp_path, monkeypatch, boundary, change
):
    from contextlib import contextmanager

    state = State(tmp_path / "state")
    clock = Clock()
    transport = Transport()
    engine = Decisions(
        state, config(), transport=transport, clock=clock, monotonic=clock
    )
    snap = snapshot()
    current = snap.model_dump(mode="json")
    changed = False

    def mutate():
        nonlocal changed
        if changed:
            return
        changed = True
        if change == "deadline":
            clock.now += 3.1
        elif change == "kill":
            engine.kill(True)
            engine.kill(False)
        elif change == "config":
            engine.config.tariff.version = "changed"
        else:
            current["revision"] += 1

    original_artifact = engine.artifact

    def artifact(key, name, value):
        reference = original_artifact(key, name, value)
        if boundary == "response_fsync" and name == "response.json":
            mutate()
        return reference

    monkeypatch.setattr(engine, "artifact", artifact)
    transaction = state.transaction

    @contextmanager
    def commit_ack():
        outer = not state.connection.in_transaction
        with transaction() as connection:
            yield connection
        if (
            outer
            and boundary == "commit_ack"
            and not changed
            and any(e["state"] == "answered" for e in engine.entries())
        ):
            mutate()

    monkeypatch.setattr(state, "transaction", commit_ack)
    r = await engine.advise(snap, "synthetic log", lambda: current)
    assert r["state"] == "unknown" and r["recommendation"] == "unknown", r
    entry = engine.entries()[0]
    assert entry["state"] == "unknown" and entry["charge"] == entry["reserved"]
    assert state.get_meta(PREFIX + "book")["unknown_cost"]
    assert (await engine.advise(snap, "synthetic log", lambda: current))[
        "state"
    ] == "unknown"
    assert len(transport.calls) == 1


async def test_cache_lookup_has_its_own_consumption_deadline(tmp_path, monkeypatch):
    clock = Clock()
    transport = Transport()
    engine = Decisions(
        State(tmp_path / "state"),
        config(),
        transport=transport,
        clock=clock,
        monotonic=clock,
    )
    assert (await advise(engine))["state"] == "answered"
    clock.now += 10
    # Originally timely cache remains reusable under its TTL, with a fresh
    # per-consumption budget; an expensive read cannot exceed that new budget.
    assert (await advise(engine))["state"] == "answered"
    valid = engine.artifact_valid

    def delayed(reference):
        clock.now += 3.1
        return valid(reference)

    monkeypatch.setattr(engine, "artifact_valid", delayed)
    assert (await advise(engine))["state"] == "unknown"
    assert len(transport.calls) == 1
