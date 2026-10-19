import asyncio
import hashlib
import json
import sys

import pytest
from test_continuity import completed_work

from oraculo.cli import download_memory, make_envelope, request
from oraculo.memory.schema import MemoryError
from oraculo.memory.transport import (
    MAX_MESSAGE_UNITS,
    MemoryArtifacts,
    memory_message,
    utf16_units,
)
from oraculo.protocol import canonical_json
from oraculo.service import Service
from oraculo.state import StateError


class Transport:
    def __init__(self, lose_response=False):
        self.calls = []
        self.lose_response = lose_response

    async def send(self, thread, message, key, **kwargs):
        assert utf16_units(message) <= MAX_MESSAGE_UNITS
        self.calls.append((thread, message, key, kwargs))
        if self.lose_response:
            self.lose_response = False
            raise ConnectionError("response lost")
        return {"runId": "dispatch-run"}


def queue_memory(service, text="🧪 verified\n" * 12000):
    attempt, report = completed_work(service, text)
    assert service.state.record_execution(
        attempt["id"],
        attempt["generation"],
        "run",
        "succeeded",
        pending_children=False,
        tree_confirmed=True,
        evidence=report,
    )
    service.after_execution()
    job = next(j for j in service.state.list_jobs() if j["kind"] == "memory")
    return job, canonical_json(json.loads(job["payload"])["execution_evidence"])


def read_envelope(service, job, artifact):
    attempt = service.state.acquire_grant(
        job["id"], "codex", "current", "coordinator", "owner-run"
    )
    service.state.claim_grant(
        attempt["id"],
        attempt["generation"],
        "memory-child",
        "memory-run",
        "memory-task",
    )
    envelope = make_envelope(
        "memory.read",
        "alpha",
        {
            "artifact": artifact,
            "attempt_id": attempt["id"],
            "generation": attempt["generation"],
            "offset": 0,
        },
    )
    envelope["origin"] = {"thread_id": "memory-child", "run_id": "memory-run"}
    return envelope


async def test_external_context_cli_roundtrip_preserves_sources_and_hashes(
    running_service, tmp_path
):
    service = running_service
    # Another client's source must never enter the issued snapshot.
    service.publisher.initialize_project("beta")
    service.publisher.snapshot("beta", "PRIVATE-BETA-ONLY", {})
    shared = service.publisher.snapshot("shared", "General shared learning", {})
    job, source = queue_memory(service)
    transport = Transport()
    await service.dispatch(transport)
    body = json.loads(transport.calls[0][1])
    reference = body["context"]
    artifact = reference["artifact"]
    assert artifact == reference["sha256"]
    assert "execution_evidence" not in body["payload"]
    assert "memory read" in body["memory_instructions"]
    envelope = read_envelope(service, job, artifact)
    output = tmp_path / "download.json"
    p = envelope["payload"]
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "oraculo",
        "--config",
        str(service.config_path),
        "memory",
        "read",
        "alpha",
        "--artifact",
        artifact,
        "--attempt-id",
        p["attempt_id"],
        "--generation",
        str(p["generation"]),
        "--thread-id",
        "memory-child",
        "--run-id",
        "memory-run",
        "--file",
        str(output),
        cwd=tmp_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    assert process.returncode == 0, stderr.decode()
    receipt = json.loads(stdout)
    assert receipt["ok"] and receipt["sha256"] == artifact
    raw = output.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == artifact
    assert output.stat().st_mode & 0o777 == 0o600
    snapshot = json.loads(raw)
    assert snapshot["job_id"] == job["id"]
    assert snapshot["context"]["source_content"] == source
    source_path = snapshot["context"]["raw_path"]
    assert (service.config.vault_root / source_path).read_bytes() == source.encode()
    assert snapshot["payload"]["execution_source"] == {
        "path": source_path,
        "sha256": hashlib.sha256(source.encode()).hexdigest(),
    }
    assert "PRIVATE-BETA-ONLY" not in raw.decode()
    sources = snapshot["context"]["context"]["raw_sources"]
    assert shared in {s["path"] for s in sources}
    assert snapshot["context"]["bundle"]["base_commit"]
    for document in snapshot["context"]["bundle"]["documents"] + sources:
        assert (
            hashlib.sha256(document["content"].encode()).hexdigest()
            == document["sha256"]
        )


async def test_context_retry_after_restart_uses_same_snapshot_and_transport(
    running_service, tmp_path
):
    service = running_service
    job, source = queue_memory(service)
    remote = Transport(lose_response=True)
    with pytest.raises(ConnectionError):
        await service.dispatch(remote)
    body = json.loads(remote.calls[0][1])
    artifact = body["context"]["artifact"]
    stored = MemoryArtifacts(service.config.state_dir).load(
        artifact, "alpha", job["id"]
    )
    # A changed vault cannot silently replace a previously emitted snapshot.
    service.publisher.snapshot("alpha", "NEW AFTER DISPATCH", {})
    restarted = Service(service.config, service.config_path)
    restarted.catalog = service.catalog
    try:
        await restarted.dispatch(remote)
        assert remote.calls[0] == remote.calls[1]
        assert (
            MemoryArtifacts(service.config.state_dir).load(artifact, "alpha", job["id"])
            == stored
        )
        envelope = read_envelope(restarted, job, artifact)
        output = tmp_path / "recovered.json"
        receipt = await download_memory(service.config, envelope, output)
        assert receipt["ok"]
        assert json.loads(output.read_bytes())["context"]["source_content"] == source
        assert "NEW AFTER DISPATCH" not in output.read_text()
    finally:
        restarted.state.close()


@pytest.mark.parametrize(
    "invalid", ["project", "run", "generation", "artifact", "cancel", "offset"]
)
async def test_context_reader_rejects_wrong_scope_identity_or_cancel(
    running_service, invalid
):
    service = running_service
    job, _ = queue_memory(service)
    remote = Transport()
    await service.dispatch(remote)
    artifact = json.loads(remote.calls[0][1])["context"]["artifact"]
    envelope = read_envelope(service, job, artifact)
    assert (await request(service.config, envelope))["ok"]
    if invalid == "project":
        service.state.register_project(
            "beta", str(service.config.projects_root / "beta")
        )
        envelope["project_id"] = "beta"
    elif invalid == "run":
        envelope["origin"]["run_id"] = "wrong-run"
    elif invalid == "generation":
        envelope["payload"]["generation"] += 1
    elif invalid == "artifact":
        envelope["payload"]["artifact"] = "0" * 64
    elif invalid == "offset":
        envelope["payload"]["offset"] = -1
    else:
        service.state.cancel_objective(job["objective_id"])
    # Reusing a successful read request_id cannot bypass current authorization.
    assert not (await request(service.config, envelope))["ok"]


async def test_context_tampering_is_rechecked_even_on_read_replay(
    running_service, tmp_path
):
    service = running_service
    job, _ = queue_memory(service)
    remote = Transport()
    await service.dispatch(remote)
    artifact = json.loads(remote.calls[0][1])["context"]["artifact"]
    envelope = read_envelope(service, job, artifact)
    assert (await request(service.config, envelope))["ok"]
    path = service.config.state_dir / "memory-contexts" / (artifact + ".json")
    path.write_text("tampered")
    response = await request(service.config, envelope)
    assert not response["ok"]
    assert "integrity" in response["error"]["message"]
    output = tmp_path / "partial.json"
    with pytest.raises(ValueError, match="integrity"):
        await download_memory(service.config, envelope, output)
    assert not output.exists()


def test_artifact_rejects_paths_symlinks_and_other_job(tmp_path):
    store = MemoryArtifacts(tmp_path)
    body = {
        "project_id": "alpha",
        "job_id": "job",
        "payload": {},
        "context": {"bundle": {"documents": []}, "context": {}},
    }
    reference = store.create(body)
    artifact = reference["artifact"]
    for project, job in [("beta", "job"), ("alpha", "another-job")]:
        with pytest.raises(StateError, match="scope_mismatch"):
            store.load(artifact, project, job)
    for name in ["../escape", "/etc/passwd", "f" * 63, "f" * 64 + ".json"]:
        with pytest.raises(StateError, match="invalid_memory_artifact"):
            store.load(name, "alpha", "job")
    path = store.root / (artifact + ".json")
    target = tmp_path / "target"
    path.rename(target)
    path.symlink_to(target)
    with pytest.raises(MemoryError, match="symlink"):
        store.load(artifact, "alpha", "job")
    path.unlink()
    store.root.rmdir()
    store.root.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(MemoryError, match="symlink"):
        store.create(body)


@pytest.mark.parametrize(
    "path",
    [
        "projects/beta/raw/private.md",
        "projects/alpha/../../beta/x",
        "projects/alpha//raw/x",
        "shared/../private",
        "/shared/x",
    ],
)
def test_artifact_manifest_rejects_other_project_and_traversal(tmp_path, path):
    store = MemoryArtifacts(tmp_path)
    with pytest.raises(StateError, match="outside_scope"):
        store.create(
            {
                "project_id": "alpha",
                "job_id": "job",
                "payload": {},
                "context": {
                    "bundle": {"documents": [{"path": path, "content": "secret"}]},
                    "context": {},
                },
            }
        )


async def test_legacy_oversized_outbox_is_not_rewritten_or_resent(running_service):
    service = running_service
    job, _ = queue_memory(service)
    key = f"dispatch:{job['id']}:0"
    payload = {"thread_id": "coordinator", "message": "🧪" * 60001}
    service.state.enqueue_outbox(key, payload, job["objective_id"])
    previous = service.state._one("SELECT * FROM outbox WHERE id=?", (key,))
    remote = Transport()
    await service.dispatch(remote)
    assert not remote.calls
    assert service.health == "legacy_dispatch_oversize_requires_reconciliation"
    assert service.state._one("SELECT * FROM outbox WHERE id=?", (key,)) == previous


def test_message_budget_counts_prompt_and_utf16_not_codepoints(tmp_path):
    body = {
        "project_id": "alpha",
        "job_id": "job",
        "payload": {},
        "executor_instructions": "🧪" * 60000,
        "context": {"operation": "query", "bundle": {"documents": []}, "context": {}},
    }
    with pytest.raises(StateError, match="instructions_too_large"):
        memory_message(body, MemoryArtifacts(tmp_path))
