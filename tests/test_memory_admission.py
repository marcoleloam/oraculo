"""ORA-CONT-002 rejects ambiguous memory tasks before admitting any work."""

import json

import pytest
from test_service import command

from oraculo.cli import main


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "memory", "text": "Registrar checkpoint"},
        {"kind": "memory", "text": "Registrar checkpoint", "memory_operation": "query"},
        {
            "kind": "memory",
            "text": "Registrar checkpoint",
            "memory_operation": "query",
            "query": "  ",
        },
        {
            "kind": "memory",
            "text": "Registrar checkpoint",
            "memory_operation": "ingest",
        },
    ],
)
async def test_api_rejects_underdefined_memory_without_job_or_objective(
    running_service, payload
):
    service = running_service
    before = (service.state.list_objectives(), service.state.list_jobs())
    result = await command(service, "objective.submit", payload)
    assert not result["ok"]
    assert "memory" in result["error"]["message"]
    assert (service.state.list_objectives(), service.state.list_jobs()) == before
    assert service.state.lease() is None


def test_cli_submit_memory_requires_explicit_operation_before_socket(
    service_config, monkeypatch, capsys
):
    _, path = service_config

    async def forbidden(*args):
        raise AssertionError("Invalid memory request reached the socket")

    monkeypatch.setattr("oraculo.cli.request", forbidden)
    assert (
        main(
            [
                "--config",
                str(path),
                "submit",
                "alpha",
                "Registrar checkpoint",
                "--kind",
                "memory",
            ]
        )
        == 1
    )
    error = json.loads(capsys.readouterr().err)
    assert "memory ingest" in error["message"]
    assert "memory query" in error["message"]


@pytest.mark.parametrize(
    "payload",
    [
        {"memory_operation": "query", "query": "Quais pendências?"},
        {"memory_operation": "lint"},
        {
            "memory_operation": "ingest",
            "execution_evidence": {
                "status": "PARTIAL",
                "source_content": "fonte integral",
            },
        },
    ],
)
async def test_valid_memory_contracts_are_admitted_unchanged(running_service, payload):
    service = running_service
    payload = {"text": "Operação explícita", "kind": "memory", **payload}
    result = await command(service, "objective.submit", payload)
    assert result["ok"]
    job = service.state.list_jobs()[0]
    assert json.loads(job["payload"]) == payload
    assert len(service.state.list_objectives()) == 1
