"""F5 uses the real disposable Unix socket; no installed service or private vault."""

import asyncio
import hashlib
import json
from argparse import Namespace

import pytest

from oraculo.cli import make_envelope, request
from oraculo.knowledge_cli import command as brain_command
from oraculo.protocol import canonical_json


async def call(service, action, data, project="alpha"):
    return await request(
        service.config, make_envelope("knowledge." + action, project, data)
    )


async def test_capture_query_and_explicit_save_over_socket(running_service):
    service = running_service
    source = service.config.projects_root / "alpha" / "decisão.md"
    text = "A reunião registrou que o projeto manterá as filas."
    source.write_text(text)
    before = service.publisher.git.head()
    planned = await call(service, "capture.plan", {"files": [str(source)]})
    assert planned["ok"], planned
    assert service.publisher.git.head() == before
    plan = planned["data"]
    applied = await call(
        service,
        "capture.apply",
        {"plan": plan, "confirmation": plan["manifest_sha256"]},
    )
    assert applied["ok"], applied
    assert (
        await call(
            service,
            "capture.apply",
            {"plan": plan, "confirmation": plan["manifest_sha256"]},
        )
    )["data"]["receipt_replay"]
    after_capture = service.publisher.git.head()
    query = await call(service, "query", {"query": "registrou filas"})
    assert query["ok"] and query["data"]["canonical_changed"] is False, query
    assert service.publisher.git.head() == after_capture
    span = {
        "path": applied["data"]["sources"][0]["path"],
        "sha256": hashlib.sha256(text.encode()).hexdigest(),
        "start": 0,
        "end": len(text.encode()),
        "quote": text,
    }
    data = {
        "title": "Filas",
        "text": "Foi relatada a manutenção das filas.",
        "evidence": [span],
    }
    proposed = await call(service, "save", data)
    assert proposed["ok"] and proposed["data"]["state"] == "proposal", proposed
    assert service.publisher.git.head() == after_capture
    saved = await call(service, "save", dict(data, apply=True))
    assert saved["ok"] and saved["data"]["state"] == "saved", saved
    after_save = service.publisher.git.head()
    assert (await call(service, "save", dict(data, apply=True)))["data"][
        "receipt_replay"
    ]
    for action in ("query", "audit", "report", "routine.status", "pilot.status"):
        response = await call(
            service, action, {"query": "filas"} if action == "query" else {}
        )
        assert response["ok"], response
        assert service.publisher.git.head() == after_save
    assert service.state.get_meta("second_brain:routines:alpha") is None


async def test_scope_query_budgets_and_maintenance_are_checked(running_service):
    service = running_service
    source = service.config.projects_root / "alpha" / "nota.md"
    source.write_text("nota")
    for action, data in (
        ("query", {"query": "x", "limit": True}),
        ("query", {"query": "x", "detail_tokens": 32001}),
        ("capture.plan", {"files": [str(source.parent / ".." / "outside.md")]}),
        ("save", {"apply": "yes"}),
    ):
        result = await call(service, action, data)
        assert not result["ok"], (action, result)
    assert not (await call(service, "report", {}, "unregistered"))["ok"]
    service.state.set_meta("runtime_maintenance", {"active": True})
    result = await call(service, "routine.configure", {"enabled": True})
    assert not result["ok"] and "maintenance" in str(result)
    assert (await call(service, "report", {}))["ok"]


async def test_oversize_reference_opens_exact_scoped_source(running_service):
    from test_memory_schema import page, proposal

    service = running_service
    text = "caisquarenta: evidência completa com acentuação.\n" * 100
    raw = service.publisher.snapshot("alpha", text)
    change = proposal(page(body=text))
    service.publisher.publish(change)
    path = change["changes"][0]["path"]
    expected = (service.publisher.vault / path).read_bytes().decode()
    private = service.publisher.snapshot("beta", "CANARY_OTHER_PROJECT")
    before = service.publisher.git.head()
    query = await call(service, "query", {"query": "caisquarenta", "detail_tokens": 1})
    assert query["ok"], query
    refs = query["data"]["context"]["selection"]["oversize"]
    reference = next(item for item in refs if item["path"] == path)
    opened = await call(service, "source", reference)
    assert opened["ok"] and opened["data"]["content"] == expected, opened
    raw_result = await call(
        service,
        "source",
        {"path": raw, "sha256": hashlib.sha256(text.encode()).hexdigest()},
    )
    assert raw_result["ok"] and raw_result["data"]["content"] == text
    assert not (await call(service, "source", {"path": path, "sha256": "0" * 64}))["ok"]
    refused = await call(service, "source", {"path": private, "sha256": "0" * 64})
    assert not refused["ok"] and "CANARY_OTHER_PROJECT" not in json.dumps(refused)
    assert service.publisher.git.head() == before


async def test_thread_local_commands_keep_provider_capacity_policy(running_service):
    from oraculo.knowledge_service import _local

    service = running_service
    service.config.max_executors = 2
    service.state.max_executors = 2
    service.state.provider_limits = {"codex": 1}
    service.state.reserve_managed("occupied-provider-only", "codex")
    local = _local(service)
    try:
        assert local.state.provider_limits == {"codex": 1}
        assert local.state.capacity()["available"] > 0
        assert local.router.select("memory", local.catalog) is None
        assert local.router.last_reason == "provider_capacity"
    finally:
        local.state.close()


async def test_large_report_paginates_and_cli_verifies_scope_hash(
    running_service, tmp_path
):
    service = running_service
    # A large deterministic response exercises the same transport as a large corpus.
    service.publisher.snapshot("alpha", "fonte")
    from unittest.mock import patch

    from oraculo import knowledge_service

    result = {"state": "reported", "project_id": "alpha", "entries": ["á" * 600000]}
    output = tmp_path / "report.json"
    args = Namespace(
        action="report",
        project="alpha",
        file=None,
        source_file=None,
        query=None,
        confirmation=None,
        apply=False,
        output=output,
    )
    with patch.object(knowledge_service, "knowledge_report", return_value=result):
        observed = await brain_command(service.config, args)
    assert observed == result
    assert json.loads(output.read_text()) == result
    assert output.stat().st_mode & 0o777 == 0o600
    with patch.object(knowledge_service, "knowledge_report", return_value=result):
        with pytest.raises(FileExistsError):
            await brain_command(service.config, args)


@pytest.mark.parametrize("operation", ["query", "lint"])
async def test_query_and_weekly_memory_grants_cannot_publish(
    running_service, operation
):
    service = running_service
    submitted = await request(
        service.config,
        make_envelope(
            "objective.submit",
            "alpha",
            {
                "text": "Memória somente leitura",
                "kind": "memory",
                "memory_operation": operation,
                "query": "filas" if operation == "query" else "",
            },
        ),
    )
    assert submitted["ok"], submitted
    grant = make_envelope(
        "grant.acquire", "alpha", {"job_id": submitted["data"]["job"]}
    )
    grant["origin"] = {"thread_id": "coordinator", "run_id": "coordinator-run"}
    granted = await request(service.config, grant)
    assert granted["ok"] and "attempt" in granted["data"], granted
    attempt = granted["data"]["attempt"]
    claim = make_envelope(
        "grant.claim",
        "alpha",
        {
            "attempt_id": attempt["id"],
            "generation": attempt["generation"],
            "thread_id": "child-" + operation,
            "run_id": "run-" + operation,
            "task_id": "task-" + operation,
        },
    )
    claim["origin"] = grant["origin"]
    assert (await request(service.config, claim))["ok"]
    proposed = make_envelope(
        "memory.propose",
        "alpha",
        {
            "attempt_id": attempt["id"],
            "generation": attempt["generation"],
            "artifact": "not-read.json",
            "sha256": "0" * 64,
        },
    )
    proposed["origin"] = {
        "thread_id": "child-" + operation,
        "run_id": "run-" + operation,
    }
    result = await request(service.config, proposed)
    assert not result["ok"] and "read_only_memory_operation" in canonical_json(
        result
    ), result


@pytest.mark.parametrize(
    "field,value",
    [
        ("payload", "CANARY_SOURCE_7f3a9c"),
        ("kind", "CANARY_SOURCE_7f3a9c"),
        ("origin", ["CANARY_SOURCE_7f3a9c"]),
    ],
)
async def test_invalid_envelopes_never_echo_received_payload(
    running_service, field, value
):
    service = running_service
    envelope = make_envelope("knowledge.report", "alpha", {})
    envelope[field] = value
    reader, writer = await asyncio.open_unix_connection(str(service.config.socket_path))
    try:
        writer.write(json.dumps(envelope).encode() + b"\n")
        await writer.drain()
        result = json.loads(await reader.readline())
    finally:
        writer.close()
        await writer.wait_closed()
    assert (
        not result["ok"]
        and result["error"]["message"] == "invalid_envelope_or_proposal"
    )
    assert "CANARY_SOURCE_7f3a9c" not in json.dumps(result)
