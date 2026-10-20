"""ORA-CONT-001: the coordinator never retranscribes memory source bytes."""

import hashlib
import json

import pytest
from test_memory_transport import Transport, queue_memory, read_envelope

from oraculo.cli import download_memory
from oraculo.memory.transport import MemoryArtifacts, memory_message


@pytest.mark.parametrize("operation", ["ingest", "query", "lint"])
def test_small_context_always_uses_integrity_checked_artifact(tmp_path, operation):
    literal = "SOURCE-MUST-NOT-PASS-THROUGH-COORDINATOR: ação 🧪\r\n"
    body = {
        "project_id": "alpha",
        "job_id": "job",
        "payload": {"memory_operation": operation, "query": "precise criteria"},
        "context": {
            "operation": operation,
            "source_content": literal,
            "raw_path": "projects/alpha/raw/source.md",
            "bundle": {"documents": []},
            "context": {"raw_sources": []},
        },
    }
    store = MemoryArtifacts(tmp_path)
    message, reference = memory_message(body, store)
    assert reference is not None
    assert "SOURCE-MUST-NOT-PASS-THROUGH-COORDINATOR" not in message
    dispatch = json.loads(message)
    assert dispatch["context"] == reference
    assert dispatch["payload"] == {"kind": "memory", "memory_operation": operation}
    snapshot = json.loads(store.load(reference["artifact"], "alpha", "job"))
    assert snapshot["payload"] == body["payload"]
    assert snapshot["context"] == body["context"]


async def test_short_execution_source_roundtrips_via_authorized_read(
    running_service, tmp_path
):
    service = running_service
    job, source = queue_memory(service, "ORA-CONT-001 resultado: ação 🧪\r\nPASS\n")
    transport = Transport()
    await service.dispatch(transport)
    body = json.loads(transport.calls[0][1])
    assert "artifact" in body["context"]
    assert "ORA-CONT-001 resultado" not in transport.calls[0][1]
    artifact = body["context"]["artifact"]
    output = tmp_path / "short-context.json"
    receipt = await download_memory(
        service.config, read_envelope(service, job, artifact), output
    )
    raw = output.read_bytes()
    assert receipt["ok"]
    assert hashlib.sha256(raw).hexdigest() == artifact
    assert json.loads(raw)["context"]["source_content"] == source
    assert output.stat().st_mode & 0o777 == 0o600
    assert (
        service.config.vault_root / json.loads(raw)["context"]["raw_path"]
    ).read_bytes() == source.encode()
