"""Read-only, service-owned snapshot; callers never open operational databases."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from oraculo.memory.context import Context
from oraculo.memory.transport import MemoryArtifacts
from oraculo.protocol import MAX_ENVELOPE_BYTES, canonical_json
from oraculo.task_store import TaskStore


def snapshot(service, project_id):
    project = service.project(project_id)  # registration + allowlist gate
    with service.publisher.locked():
        service.publisher._check_integrity()
        documents = Context(service.publisher.vault).files(project_id)
        base = service.publisher.git.head()
    store = service.managed_service.store if service.managed_service else None
    owned = False
    ledger = service.config.state_dir / "managed-tasks.sqlite3"
    if store is None and ledger.exists():
        store = TaskStore(ledger, read_only=True)
        owned = True
    tasks = []
    try:
        if store:
            for row in store.db.execute("SELECT id FROM managed_tasks ORDER BY rowid"):
                task = store.get(row[0])
                if (task.get("project") or {}).get("project_id") != project_id:
                    continue
                tasks.append(
                    {
                        k: task.get(k)
                        for k in (
                            "id",
                            "phase",
                            "blocked_reason",
                            "project",
                            "child",
                            "origin",
                            "work_status",
                            "observed_at",
                            "snapshot_revision",
                            "memory",
                            "completion_evidence",
                        )
                    }
                    | {"title": (task.get("request") or {}).get("title")}
                )
                # No host transcripts, context, launch args, report journals or
                # child launch receipt are required to render the presentation.
                tasks[-1]["child"] = {
                    "childThreadId": (task.get("child") or {}).get("childThreadId")
                }
                tasks[-1]["project"] = {"project_id": project_id}
                tasks[-1]["origin"] = {
                    "thread_id": (task.get("origin") or {}).get("thread_id")
                }
                tasks[-1]["memory"] = {"id": (task.get("memory") or {}).get("id")}
                tasks[-1]["completion_evidence"] = bool(task.get("completion_evidence"))
    finally:
        if owned:
            store.close()
    cycles = [
        {
            k: c.get(k)
            for k in (
                "id",
                "project_id",
                "read_state",
                "read_receipt",
                "write_state",
                "write_receipt",
            )
        }
        for c in service.memory_cycles.list(project_id)
    ]
    return dict(
        schema_version=1,
        project_id=project_id,
        observed_at=datetime.now(timezone.utc).isoformat(),
        base_commit=base,
        projects=[dict(id=project_id, title=Path(project["path"]).name)],
        tasks=tasks,
        cycles=cycles,
        documents=[dict(path=p, content=c) for p, c in sorted(documents.items())],
    )


def response(service, project_id, payload):
    service.project(project_id)
    artifacts = MemoryArtifacts(service.config.state_dir)
    if payload.get("artifact"):
        return artifacts.read(
            payload["artifact"], project_id, "experience", payload.get("offset", 0)
        )
    value = snapshot(service, project_id)
    if len(canonical_json(value).encode()) < MAX_ENVELOPE_BYTES - 4096:
        return value
    reference = artifacts.create(
        dict(
            project_id=project_id,
            job_id="experience",
            payload={},
            context={"bundle": value, "context": {}},
        )
    )
    return dict(
        schema_version=1,
        project_id=project_id,
        observed_at=value["observed_at"],
        artifact=reference,
    )
