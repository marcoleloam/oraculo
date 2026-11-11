"""Laboratory recovery tests using disposable State/socket and mocked API only."""

import asyncio
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import httpx2
import pytest
from test_decisions import Clock, Transport, advise, answer, config, snapshot
from test_service import command

from oraculo import decision_credentials
from oraculo.decision_recovery import (
    ACK,
    acknowledge,
    cost,
    financial_status,
    validate_entry,
)
from oraculo.decisions import PREFIX, Decisions, HTTPDecisionTransport
from oraculo.protocol import canonical_json, content_hash
from oraculo.state import State, StateError


def submission(entry, **changes):
    result = dict(
        key=entry["key"],
        expected_entry_digest=content_hash(entry),
        idempotency_key="ref:lab-ack-" + entry["key"],
        actor_reference="ref:lab-operator",
        reason="conservatively_accounted_upper_bound",
        evidence_reference="ref:lab-diagnosis",
        evidence_sha256="e" * 64,
    )
    result.update(changes)
    return result


async def unknown(tmp_path, *, transport=None, clock=None):
    engine = Decisions(
        State(tmp_path / "state"),
        config(),
        transport=transport or Transport(status=503),
        **({"clock": clock, "monotonic": clock} if clock else {}),
    )
    await advise(engine)
    return engine, engine.entries()[0]


async def test_upper_ack_preserves_original_result_events_totals_restart_and_never_retries_key(
    tmp_path,
):
    engine, entry = await unknown(tmp_path)
    raw = canonical_json(entry)
    audits = list(engine.state.connection.execute("SELECT * FROM audit_events"))
    total = financial_status(engine, "p")
    receipt = acknowledge(engine, "p", submission(entry))
    assert receipt["state"] == "accounted_upper_bound" and not receipt["replay"]
    assert canonical_json(engine.entries()[0]) == raw
    assert (
        list(engine.state.connection.execute("SELECT * FROM audit_events"))[
            : len(audits)
        ]
        == audits
    )
    after = financial_status(engine, "p")
    assert not after["unknown_cost"] and after["entries"][0]["acknowledged"]
    for field in ["calls_total", "charge_total_microusd", "daily_charge_microusd"]:
        assert total[field] == after[field]
    assert total["charge_total_microusd"] == entry["reserved"]
    assert acknowledge(engine, "p", submission(entry))["replay"]
    assert engine.state.schema_version() == 8
    engine.state.close()
    with pytest.raises(StateError, match="state_schema_newer_than_runtime"):
        State(tmp_path / "state", schema_max=7)
    restarted = Decisions(State(tmp_path / "state"), config(), transport=Transport())
    assert not restarted.status("p")["financial"]["unknown_cost"]
    assert (await advise(restarted))["state"] == "unknown"
    assert restarted.transport.calls == []
    assert (await advise(restarted, snapshot(event="new")))["state"] == "answered"
    assert len(restarted.transport.calls) == 1
    assert restarted.entries()[0]["charge"] == entry["reserved"]


async def test_stale_digest_cas_reused_identity_and_conflicting_replay_no_mutation(
    tmp_path, monkeypatch
):
    engine, entry = await unknown(tmp_path)
    before = canonical_json(engine.entries())
    with pytest.raises(StateError, match="digest_stale"):
        acknowledge(engine, "p", submission(entry, expected_entry_digest="0" * 64))
    original = engine.state.compare_meta
    monkeypatch.setattr(engine.state, "compare_meta", lambda *a: False)
    with pytest.raises(StateError, match="cas_lost"):
        acknowledge(engine, "p", submission(entry))
    assert (
        engine.state.schema_version() == 7
        and engine.state.get_meta(ACK + entry["key"]) is None
    )
    monkeypatch.setattr(engine.state, "compare_meta", original)
    acknowledge(engine, "p", submission(entry))
    with pytest.raises(StateError, match="conflict"):
        acknowledge(engine, "p", submission(entry, evidence_sha256="f" * 64))
    assert canonical_json(engine.entries()) == before
    assert (
        engine.state.connection.execute(
            "SELECT count(*) FROM audit_events WHERE kind='decisions_unknown_upper_acknowledged'"
        ).fetchone()[0]
        == 1
    )


async def test_concurrent_ack_sqlite_cas_idempotent_across_connections(tmp_path):
    engine, entry = await unknown(tmp_path)
    engine.state.close()
    barrier = Barrier(6)

    def worker(_):
        state = State(tmp_path / "state")
        advisor = Decisions(state, config(), transport=Transport())
        barrier.wait(timeout=10)
        result = acknowledge(advisor, "p", submission(entry))
        state.close()
        return result["replay"]

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(worker, range(6)))
    assert results.count(False) == 1 and results.count(True) == 5


async def test_unresolved_other_project_blocks_and_book_flag_cannot_override(tmp_path):
    entered = asyncio.Queue()
    release = asyncio.Event()

    async def pause():
        await entered.put(True)
        await release.wait()

    engine = Decisions(
        State(tmp_path / "state"),
        config(),
        transport=Transport(status=503, effect=pause),
    )
    first = asyncio.create_task(advise(engine))
    await entered.get()
    second = asyncio.create_task(
        advise(engine, snapshot(project="other", event="other"))
    )
    await entered.get()
    release.set()
    await asyncio.gather(first, second)
    entries = engine.entries()
    acknowledge(engine, "p", submission(entries[0]))
    assert engine.state.get_meta(PREFIX + "book")["unknown_cost"]
    book = engine.state.get_meta(PREFIX + "book")
    book["unknown_cost"] = False
    engine.state.set_meta(PREFIX + "book", book)
    assert (await advise(engine, snapshot(event="new")))["reason"] == "unknown_cost"
    assert len(engine.transport.calls) == 2
    with pytest.raises(StateError, match="project_mismatch"):
        acknowledge(engine, "p", submission(entries[1]))
    acknowledge(engine, "other", submission(entries[1]))
    assert not engine.status("p")["financial"]["unknown_cost"]


@pytest.mark.parametrize(
    "changes",
    [
        {"reason": "schema"},
        {"reason": "usage"},
        {"reason": "persistence_uncertain"},
        {"state": "intent"},
        {"state": "sent"},
        {"state": "answered"},
        {"reserved": 0},
        {"reserved": -1},
        {"reserved": True},
        {"charge": 0},
        {"token_bound": 99999999},
        {"logical_task": "wrong"},
        {"days": []},
        {"deadline": float("inf")},
        {"tariff": {}},
        {"tariff": {"output_nanousd": 100}},
    ],
)
async def test_invalid_corrupt_nonterminal_and_unbounded_entries_never_ack(
    tmp_path, changes
):
    engine, entry = await unknown(tmp_path)
    entry.update(changes)
    if "reason" in changes:
        entry["result"]["reason"] = changes["reason"]
    # Bypass the canonical serializer ONLY to emulate corrupt storage.
    engine.state.connection.execute(
        "UPDATE metadata SET value=? WHERE key=?",
        (json.dumps(entry), PREFIX + "entry:" + entry["key"]),
    )
    with pytest.raises((StateError, ValueError)):
        acknowledge(engine, "p", submission(entry))
    assert engine.state.get_meta(ACK + entry["key"]) is None
    assert engine.state.schema_version() == 7


@pytest.mark.parametrize(
    "changes",
    [
        {"actor_reference": ""},
        {"evidence_reference": "/tmp/evidence"},
        {"evidence_sha256": "bad"},
        {"reason": "refund"},
        {"idempotency_key": "raw-value"},
        {"unexpected": True},
        {"key": True},
    ],
)
async def test_invalid_ack_values_refs_rejected_without_write(tmp_path, changes):
    engine, entry = await unknown(tmp_path)
    with pytest.raises(StateError, match="acknowledgement_invalid"):
        acknowledge(engine, "p", submission(entry, **changes))
    assert engine.state.get_meta(ACK + entry["key"]) is None


async def test_corrupt_request_other_entry_or_orphan_flag_cannot_be_cleared(tmp_path):
    engine, entry = await unknown(tmp_path)
    from pathlib import Path

    Path(entry["request_artifact"]["reference"]).write_text("{}")
    with pytest.raises(StateError, match="not_acknowledgeable"):
        acknowledge(engine, "p", submission(entry))
    assert engine.status("p")["financial"]["unknown_cost"]
    engine.state.set_meta(PREFIX + "entry:" + "z" * 64, {"state": "unknown"})
    assert engine.status("p")["financial"]["reason"] == "decision_accounting_corrupt"
    with pytest.raises(StateError, match="corrupt"):
        acknowledge(engine, "p", submission(entry))


async def test_unknown_prevents_call_limit_or_budget_refund(tmp_path):
    engine, entry = await unknown(tmp_path)
    acknowledge(engine, "p", submission(entry))
    engine.config.max_calls = 1
    assert (await advise(engine, snapshot(event="other")))["reason"] == "call_limit"
    engine.config.max_calls = 80
    engine.config.task_budget_microusd = entry["reserved"]
    assert (await advise(engine, snapshot(event="other")))["reason"] == "budget"
    assert len(engine.transport.calls) == 1


async def test_lab_public_cli_socket_prepare_status_ack_and_origin_auth(
    running_service, tmp_path
):
    service = running_service
    refs, sends = [], []

    async def resolve(ref):
        refs.append(ref)
        raise decision_credentials.CredentialUnavailable("access_denied_or_missing")

    transport = HTTPDecisionTransport(
        resolve, http_transport=httpx2.MockTransport(lambda r: sends.append(True))
    )
    service.config.decisions = config()
    service.decisions = Decisions(
        service.state, service.config.decisions, transport=transport
    )
    snap = snapshot(project="alpha")
    await advise(service.decisions, snap)
    entry = service.decisions.entries()[0]
    before = len(refs)

    async def cli(action, payload=None):
        argv = [
            sys.executable,
            "-m",
            "oraculo",
            "--config",
            str(service.config_path),
            "decisions",
            action,
            "--project",
            "alpha",
        ]
        if payload is not None:
            path = tmp_path / (action + ".json")
            path.write_text(json.dumps(payload))
            argv += ["--file", str(path)]
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        assert proc.returncode == 0, stderr.decode()
        return json.loads(stdout)

    status = await cli("status")
    assert status["data"]["financial"]["unknown_cost"] and len(refs) == before
    for kind in ["decisions.prepare", "decisions.status", "decisions.acknowledge"]:
        denied = await command(
            service,
            kind,
            submission(entry) if kind.endswith("acknowledge") else {},
            origin={"thread_id": "coordinator", "run_id": "run"},
        )
        assert not denied["ok"]
    prepared = await cli("prepare")
    assert (
        prepared["data"]["state"] == "unavailable" and prepared["data"]["tokens"] == 0
    )
    assert "/usr/bin/security" in prepared["data"]["human_action"] and sends == []
    result = await cli("acknowledge", submission(entry))
    assert result["data"]["retained_charge_microusd"] == entry["reserved"]
    assert (await cli("acknowledge", submission(entry)))["data"]["replay"]
    status = await cli("status")
    assert not status["data"]["financial"]["unknown_cost"]
    assert service.decisions.entries()[0] == entry and sends == []


async def test_exact_432_upper_charge_legacy_unknown_not_rewritten_or_resubmitted(
    tmp_path,
):
    from test_operational_decisions import active

    class Failure:
        calls = 0

        async def post(self, *args, **kwargs):
            self.calls += 1
            raise RuntimeError("synthetic-private-error")

    transport = Failure()
    clock = Clock()
    engine = Decisions(
        State(tmp_path / "state"),
        active(),
        transport=transport,
        clock=clock,
        monotonic=clock,
    )
    snap = snapshot()
    await engine.choose(
        snap, 2, {"kind": "implementation"}, lambda: snap.model_dump(mode="json")
    )
    entry = engine.entries()[0]
    # The historical 0.3.0 transport did not record a stage. Model that shape,
    # rather than backfilling a before-send claim from a later diagnosis.
    entry.pop("stage")
    entry["result"].pop("stage")
    engine.state.set_meta(PREFIX + "entry:" + entry["key"], entry)
    assert entry["reserved"] == entry["charge"] == 432
    original = canonical_json(entry)
    acknowledge(engine, "p", submission(entry))
    await engine.choose(
        snap, 2, {"kind": "implementation"}, lambda: snap.model_dump(mode="json")
    )
    assert transport.calls == 1 and canonical_json(engine.entries()[0]) == original
    row = engine.status("p")["financial"]["entries"][0]
    assert row["stage"] == "unknown" and row["charge_microusd"] == 432
    assert engine.status("p")["financial"]["calls_total"] == 1


@pytest.mark.parametrize("phase", ["before_http", "possible_send", "response"])
async def test_crash_retains_stage_restart_marks_unknown_without_resend(
    tmp_path, phase
):
    class Crash(BaseException):
        pass

    class Body(httpx2.AsyncByteStream):
        async def __aiter__(self):
            raise Crash()
            yield b""

    async def resolve(_):
        if phase == "before_http":
            raise Crash()
        return "synthetic-lab-token"

    def handle(_):
        if phase == "possible_send":
            raise Crash()
        return httpx2.Response(200, stream=Body())

    clock = Clock()
    engine = Decisions(
        State(tmp_path / "state"),
        config(),
        transport=HTTPDecisionTransport(
            resolve, http_transport=httpx2.MockTransport(handle)
        ),
        clock=clock,
        monotonic=clock,
    )
    with pytest.raises(Crash):
        await advise(engine)
    assert (
        engine.entries()[0]["stage"] == phase and engine.entries()[0]["state"] == "sent"
    )
    engine.state.close()
    clock.now += 4
    restarted = Decisions(
        State(tmp_path / "state"),
        config(),
        transport=Transport(),
        clock=clock,
        monotonic=clock,
    )
    assert restarted.status("p")["financial"]["unknown_cost"]
    assert (await advise(restarted, snapshot(event="new")))["reason"] == "unknown_cost"
    entry = restarted.entries()[0]
    assert entry["state"] == "unknown" and entry["stage"] == phase
    acknowledge(restarted, "p", submission(entry))
    assert (await advise(restarted))[
        "state"
    ] == "unknown" and restarted.transport.calls == []


async def test_received_usage_over_bound_cannot_hide_behind_snapshot_change(tmp_path):
    current = snapshot().model_dump(mode="json")
    raw = answer()
    raw["usage"]["input_tokens"] = raw["usage"]["total_tokens"] = 999999
    transport = Transport(raw, effect=lambda: current.update(revision=2))
    engine = Decisions(State(tmp_path / "state"), config(), transport=transport)
    result = await engine.advise(snapshot(), "synthetic", lambda: current)
    assert result["reason"] == "usage"
    with pytest.raises(StateError, match="not_acknowledgeable"):
        acknowledge(engine, "p", submission(engine.entries()[0]))


async def test_legacy_writer_ceiling_and_failed_audit_leave_ack_atomic(
    tmp_path, monkeypatch
):
    engine, entry = await unknown(tmp_path)
    engine.state.schema_max = 7
    with pytest.raises(StateError, match="schema_unsupported"):
        acknowledge(engine, "p", submission(entry))
    engine.state.schema_max = 8

    def fail(*a):
        raise OSError("synthetic-storage-failure")

    monkeypatch.setattr(engine.state, "audit", fail)
    with pytest.raises(OSError):
        acknowledge(engine, "p", submission(entry))
    assert (
        engine.state.schema_version() == 7
        and engine.state.get_meta(ACK + entry["key"]) is None
    )
    assert engine.state.get_meta(PREFIX + "book")["unknown_cost"]


async def test_ack_corrupt_metadata_or_missing_audit_cannot_unblock(tmp_path):
    engine, entry = await unknown(tmp_path)
    acknowledge(engine, "p", submission(entry))
    engine.state.connection.execute(
        "DELETE FROM audit_events WHERE kind='decisions_unknown_upper_acknowledged'"
    )
    assert engine.status("p")["financial"]["unknown_cost"]
    assert (await advise(engine, snapshot(event="new")))["reason"] == "unknown_cost"
    assert len(engine.transport.calls) == 1


async def test_malformed_storage_blocks_status_reservation_and_ack(tmp_path):
    engine, entry = await unknown(tmp_path)
    engine.state.connection.execute(
        "UPDATE metadata SET value=? WHERE key=?",
        ("{malformed", PREFIX + "entry:" + entry["key"]),
    )
    assert engine.status("p")["financial"]["reason"] == "decision_accounting_corrupt"
    assert (await advise(engine, snapshot(event="new")))[
        "reason"
    ] == "decision_accounting_corrupt"
    with pytest.raises(ValueError):
        acknowledge(engine, "p", submission(entry))
    assert len(engine.transport.calls) == 1


@pytest.mark.parametrize(
    "book",
    [
        None,
        [],
        {},
        {"unknown_cost": False},
        {"unknown_cost": False, "failures": [1], "consecutive": 0, "open_until": 0},
        {"unknown_cost": False, "failures": [], "consecutive": -1, "open_until": 0},
        {
            "unknown_cost": False,
            "failures": [],
            "consecutive": 0,
            "open_until": float("nan"),
        },
    ],
)
async def test_corrupt_book_not_recoverable_by_ack(tmp_path, book):
    engine, entry = await unknown(tmp_path)
    engine.state.connection.execute(
        "UPDATE metadata SET value=? WHERE key=?", (json.dumps(book), PREFIX + "book")
    )
    assert engine.status("p")["financial"]["reason"] == "decision_accounting_corrupt"
    with pytest.raises(StateError, match="corrupt"):
        acknowledge(engine, "p", submission(entry))
    assert (await advise(engine, snapshot(event="new")))[
        "reason"
    ] == "decision_accounting_corrupt"


async def test_n_decisions_2_nonzero_output_tokens_stay_readable(tmp_path):
    """N-decisions-2: real output_tokens must not lock validate_entry."""
    raw = answer()
    raw["usage"]["output_tokens"] = 3
    raw["usage"]["output_tokens_details"]["reasoning_tokens"] = 1
    raw["usage"]["total_tokens"] = raw["usage"]["input_tokens"] + 3
    engine = Decisions(
        State(tmp_path / "state"),
        config(),
        transport=Transport(raw),
    )
    first = await advise(engine)
    assert first["state"] == "answered", first
    stored = engine.entries()[0]
    assert stored["result"]["usage"]["output_tokens"] == 3
    assert stored["charge"] == cost(
        stored["result"]["usage"]["input_tokens"], engine.config.tariff
    )
    validate_entry(engine, stored)
    second = await advise(engine, snapshot(event="later"))
    assert second["state"] == "answered", second
    financial = engine.status("p")["financial"]
    assert financial["state"] == "ready"
    assert financial["reason"] == "accounting_ready"


async def test_unknown_result_contradictory_usage_never_ack(tmp_path):
    engine, entry = await unknown(tmp_path)
    entry["result"]["usage"] = {
        "input_tokens": entry["token_bound"] + 1,
        "output_tokens": 0,
    }
    engine.state.set_meta(PREFIX + "entry:" + entry["key"], entry)
    with pytest.raises(StateError, match="corrupt"):
        acknowledge(engine, "p", submission(entry))
