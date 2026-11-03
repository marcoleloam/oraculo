"""Laboratory subprocess/HTTP tests: never reads the real Keychain or API."""

import asyncio
import json
import sys
import threading
import time

import httpx2
import pytest
from test_decisions import advise, answer, config, snapshot

from oraculo import decision_credentials as credentials
from oraculo.config import DecisionsConfig
from oraculo.decisions import Decisions, HTTPDecisionTransport
from oraculo.state import State


def security_lab(monkeypatch, code):
    original = asyncio.create_subprocess_exec
    processes, calls = [], []

    async def spawn(*argv, **kwargs):
        calls.append((argv, kwargs))
        assert argv == (
            "/usr/bin/security",
            "find-generic-password",
            "-s",
            "oraculo.openai",
            "-a",
            "decisions-api",
            "-w",
        )
        assert kwargs["env"] == {} and kwargs["stderr"] == asyncio.subprocess.DEVNULL
        process = await original(sys.executable, "-c", code, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(credentials.sys, "platform", "darwin")
    monkeypatch.setattr(credentials.asyncio, "create_subprocess_exec", spawn)
    return processes, calls


@pytest.mark.parametrize("cancel", [False, True])
async def test_blocked_security_killed_reaped_without_worker_or_loop_block(
    monkeypatch, cancel
):
    before = {t.ident for t in threading.enumerate()}
    processes, calls = security_lab(monkeypatch, "import time; time.sleep(30)")
    started = time.monotonic()
    task = asyncio.create_task(
        credentials.resolve_credential(credentials.REFERENCE, timeout=0.05)
    )
    while not processes:
        await asyncio.sleep(0.001)
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(credentials.CredentialUnavailable) as exc:
            await task
        assert exc.value.reason == "timeout"
    assert time.monotonic() - started < 1
    assert len(calls) == 1 and processes[0].returncode is not None
    assert {t.ident for t in threading.enumerate()} == before


@pytest.mark.parametrize(
    "code,reason",
    [
        (
            'import sys; sys.stderr.write("synthetic-private-error"); sys.exit(44)',
            "access_denied_or_missing",
        ),
        ('print("")', "invalid_value"),
        ('print("bad value")', "invalid_value"),
        ('print("x" * 9000)', "invalid_value"),
    ],
)
async def test_denied_or_invalid_security_fails_fast_redacted(
    monkeypatch, code, reason
):
    processes, _ = security_lab(monkeypatch, code)
    start = time.monotonic()
    with pytest.raises(credentials.CredentialUnavailable) as exc:
        await credentials.resolve_credential(credentials.REFERENCE)
    assert exc.value.reason == reason and "synthetic-private" not in str(exc.value)
    assert time.monotonic() - start < 1 and processes[0].returncode is not None


@pytest.mark.parametrize(
    "reference", [None, "", "raw-token", "ref:other", "ref:oraculo-openai-decisions\n"]
)
async def test_wrong_reference_never_starts_process(monkeypatch, reference):
    _, calls = security_lab(monkeypatch, "raise AssertionError()")
    with pytest.raises(credentials.CredentialUnavailable):
        await credentials.resolve_credential(reference)
    assert calls == []


async def test_prepare_and_status_local_only_no_cache_no_ledger_mutation(
    tmp_path, monkeypatch
):
    _, calls = security_lab(monkeypatch, 'print("synthetic-lab-token")')
    sends = []
    transport = HTTPDecisionTransport(
        http_transport=httpx2.MockTransport(lambda r: sends.append(r))
    )
    engine = Decisions(
        State(tmp_path / "state"),
        config().model_copy(update={"credential_reference": credentials.REFERENCE}),
        transport=transport,
    )
    assert engine.status("p")["credential"]["reason"] == "not_prepared"
    assert calls == []
    assert (await engine.prepare()) == dict(
        state="available", reason="resolved", stage="before_http", api_calls=0, tokens=0
    )
    assert engine.status("p")["credential"]["state"] == "available"
    assert (await engine.prepare())["state"] == "available"
    assert len(calls) == 2 and sends == [] and engine.entries() == []
    assert engine.state.schema_version() == 4
    assert "synthetic-lab-token" not in json.dumps(engine.status("p"))


@pytest.mark.parametrize(
    "setting",
    [
        DecisionsConfig(),
        config().model_copy(update={"daily_budget_microusd": 0}),
        config().model_copy(update={"task_budget_microusd": 0}),
        config().model_copy(update={"run_budget_microusd": 0}),
        config().model_copy(update={"max_calls": 0}),
        config().model_copy(update={"tariff": None}),
    ],
)
async def test_disabled_zero_budget_prepare_and_advice_never_resolve(tmp_path, setting):
    def forbidden(_):
        pytest.fail("credential resolution forbidden")

    transport = HTTPDecisionTransport(forbidden)
    engine = Decisions(State(tmp_path / "state"), setting, transport=transport)
    assert (await engine.prepare())["state"] == "unavailable"
    assert (await advise(engine))["state"] == "unknown"
    engine.status("p")
    assert engine.entries() == []


@pytest.mark.parametrize(
    "phase", ["credential_denied", "credential_timeout", "possible_send", "response"]
)
async def test_durable_transport_stage_timeout_and_safe_reason(tmp_path, phase):
    sends = []

    async def resolve(_):
        if phase == "credential_denied":
            raise credentials.CredentialUnavailable("access_denied_or_missing")
        if phase == "credential_timeout":
            await asyncio.sleep(5)
        return "synthetic-lab-token"

    class SlowBody(httpx2.AsyncByteStream):
        async def __aiter__(self):
            await asyncio.sleep(5)
            yield b"{}"

    async def handle(request):
        sends.append(True)
        if phase == "possible_send":
            await asyncio.sleep(5)
        return httpx2.Response(200, stream=SlowBody())

    transport = HTTPDecisionTransport(
        resolve, http_transport=httpx2.MockTransport(handle)
    )
    engine = Decisions(
        State(tmp_path / "state"),
        config().model_copy(update={"deadline_seconds": 0.1}),
        transport=transport,
    )
    result = await advise(engine)
    expected = "before_http" if phase.startswith("credential_") else phase
    assert result["state"] == "unknown" and result["stage"] == expected
    entry = engine.entries()[0]
    assert entry["stage"] == expected and entry["charge"] == entry["reserved"]
    assert len(sends) == (0 if phase.startswith("credential_") else 1)
    assert "synthetic-lab-token" not in json.dumps(entry)
    assert (await advise(engine, snapshot(event="next")))["reason"] == "unknown_cost"


async def test_success_resolves_lazily_for_every_new_call_no_token_cache(tmp_path):
    refs = []

    async def resolve(ref):
        refs.append(ref)
        return "synthetic-lab-token"

    transport = HTTPDecisionTransport(
        resolve,
        http_transport=httpx2.MockTransport(
            lambda r: httpx2.Response(200, json=answer())
        ),
    )
    engine = Decisions(State(tmp_path / "state"), config(), transport=transport)
    assert (await advise(engine))["stage"] == "response"
    assert (await advise(engine, snapshot(event="new")))["state"] == "answered"
    assert len(refs) == 2
