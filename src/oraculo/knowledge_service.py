"""Service-owned Second Brain entrypoints. No client opens the live vault or DB."""

from __future__ import annotations

import asyncio
from time import perf_counter
from types import SimpleNamespace

from oraculo.knowledge import (
    capture_apply,
    capture_plan,
    capture_text_apply,
    capture_text_plan,
    knowledge_report,
    save_answer,
)
from oraculo.memory.context import Context
from oraculo.memory.routines import KnowledgeRoutines
from oraculo.memory.schema import ConflictError, MemoryError, digest, project_key
from oraculo.memory.transport import MemoryArtifacts
from oraculo.protocol import MAX_ENVELOPE_BYTES, canonical_json, content_hash
from oraculo.routing import Router
from oraculo.state import State, StateError


class KnowledgeCommands:
    def __init__(self, service):
        self.service = service
        self.state = service.state

    def command(self, envelope):
        project_id = envelope.project_id
        project = self.service.project(project_id)
        action = envelope.kind.removeprefix("knowledge.")
        payload = envelope.payload
        publisher = self.service.publisher
        if action == "read":
            return MemoryArtifacts(self.service.config.state_dir).read(
                payload["artifact"], project_id, "knowledge", payload.get("offset", 0)
            )
        if action == "source":
            path, expected = payload.get("path"), payload.get("sha256")
            if not isinstance(path, str) or not isinstance(expected, str):
                raise MemoryError("knowledge_source_reference_required")
            with publisher.locked():
                publisher._check_integrity()
                files = Context(publisher.vault).files(project_id)
                if path not in files:
                    raise MemoryError("knowledge_source_outside_scope")
                content = files[path]
                if digest(content) != expected:
                    raise ConflictError("knowledge_source_changed")
            return {
                "state": "opened",
                "project_id": project_id,
                "path": path,
                "sha256": expected,
                "content": content,
                "canonical_changed": False,
            }
        if action == "query":
            from oraculo.memory.retrieval import retrieve

            query = payload.get("query")
            if not isinstance(query, str) or not query.strip():
                raise MemoryError("knowledge_query_required")
            for key, default, maximum in (
                ("limit", 20, 100),
                ("detail_tokens", 6000, 32000),
                ("brief_tokens", 800, 4000),
            ):
                value = payload.get(key, default)
                if type(value) is not int or not 0 <= value <= maximum:
                    raise MemoryError("knowledge_query_budget_invalid")
            started = perf_counter()
            with publisher.locked():
                publisher._check_integrity()
                result = retrieve(
                    Context(publisher.vault),
                    project_id,
                    query,
                    limit=payload.get("limit", 20),
                    detail_tokens=payload.get("detail_tokens", 6000),
                    brief_tokens=payload.get("brief_tokens", 800),
                )
            telemetry_key = "knowledge:observations:" + project_key(project_id)
            history = self.state.get_meta(telemetry_key, [])
            selection = result.get("selection", {})
            history.append(
                {
                    "latency_ms": (perf_counter() - started) * 1000,
                    "documents": len(result.get("documents", [])),
                    "bytes": sum(
                        len(document["content"].encode())
                        for document in result.get("documents", [])
                    ),
                    "abstained": selection.get("abstained"),
                    "citations": len(result.get("documents", [])),
                    "citation_kind": "selected_source_references_not_semantic_verification",
                }
            )
            self.state.set_meta(telemetry_key, history[-200:])
            return {
                "state": "retrieved",
                "project_id": project_id,
                "context": result,
                "canonical_changed": False,
            }
        if action == "capture.plan":
            if "text" in payload:
                return capture_text_plan(
                    project_id,
                    payload["text"],
                    source_kind=payload.get("source_kind", "document"),
                    sanitized=payload.get("sanitized", False),
                )
            return capture_plan(
                project_id,
                project["path"],
                payload.get("files"),
                source_kind=payload.get("source_kind", "document"),
                sanitized=payload.get("sanitized", False),
            )
        if action == "capture.apply":
            confirmation = payload.get("confirmation")
            if not isinstance(confirmation, str) or len(confirmation) != 64:
                raise MemoryError("capture_manifest_confirmation_required")
            key = "knowledge:capture:" + project_key(project_id) + ":" + confirmation
            previous = self.state.get_meta(key)
            if previous:
                return dict(previous, receipt_replay=True)
            if "text" in payload:
                result = capture_text_apply(
                    publisher,
                    project_id,
                    payload["text"],
                    confirmation,
                    source_kind=payload.get("source_kind", "document"),
                    sanitized=payload.get("sanitized", False),
                )
            else:
                result = capture_apply(
                    publisher,
                    project_id,
                    project["path"],
                    payload.get("plan"),
                    confirmation,
                )
            self.state.set_meta(key, result)
            return result
        if action == "save":
            if type(payload.get("apply", False)) is not bool:
                raise MemoryError("answer_explicit_boolean_required")
            fingerprint = content_hash(payload)
            key = "knowledge:answer:" + project_key(project_id) + ":" + fingerprint
            previous = self.state.get_meta(key) if payload.get("apply") else None
            if previous:
                return dict(previous, receipt_replay=True)
            result = save_answer(
                publisher,
                project_id,
                title=payload.get("title"),
                text=payload.get("text"),
                evidence=payload.get("evidence", []),
                as_hypothesis=payload.get("as_hypothesis", False),
                apply=payload.get("apply", False),
            )
            if result["state"] in {"saved", "no_changes"}:
                self.state.set_meta(key, result)
            return result
        if action == "report":
            result = knowledge_report(publisher, project_id)
            result["observations"] = self.state.get_meta(
                "knowledge:observations:" + project_key(project_id), []
            )
            return result
        if action == "audit":
            from oraculo.memory.freshness import freshness_audit

            with publisher.locked():
                publisher._check_integrity()
                files = Context(publisher.vault).files(project_id)
            return {
                "state": "audited",
                "project_id": project_id,
                "findings": freshness_audit(files, project_id),
                "canonical_changed": False,
            }
        if action == "compile":
            from oraculo.memory.compiler import compile_knowledge

            if type(payload.get("apply", False)) is not bool:
                raise MemoryError("knowledge_explicit_boolean_required")
            return compile_knowledge(
                publisher,
                project_id,
                payload.get("sources", []),
                concept=payload.get("concept"),
                title=payload.get("title"),
                version="1",
                apply=payload.get("apply", False),
            )
        if action == "review":
            from oraculo.memory.freshness import build_review_proposal

            if type(payload.get("apply", False)) is not bool:
                raise MemoryError("knowledge_explicit_boolean_required")
            with publisher.locked():
                publisher._check_integrity()
                files = Context(publisher.vault).files(project_id)
                result = build_review_proposal(
                    files, project_id, payload.get("decisions", [])
                )
            if payload.get("apply") and result.get("proposal"):
                result["receipt"] = publisher.publish(result["proposal"])
                result["state"] = "saved"
            return result
        if action == "migration.plan":
            from oraculo.memory.migration import plan_migration

            return plan_migration(publisher, project_id)
        if action == "migration.apply":
            from oraculo.memory.migration import apply_migration

            plan = payload.get("plan")
            if not isinstance(plan, dict) or plan.get("project_id") != project_id:
                raise MemoryError("migration_project_mismatch")
            return apply_migration(publisher, plan)
        if action.startswith("routine."):
            routines = KnowledgeRoutines(self.service)
            verb = action.removeprefix("routine.")
            if verb == "status":
                return routines.status(project_id)
            if verb == "cancel":
                return routines.cancel(project_id)
            if verb == "configure":
                return routines.configure(project_id, **payload)
            if verb == "tick":
                return routines.tick(project_id)
        if action.startswith("pilot."):
            routines = KnowledgeRoutines(self.service)
            verb = action.removeprefix("pilot.")
            if verb == "start":
                return routines.pilot_start(project_id, **payload)
            if verb == "review":
                return routines.pilot_review(project_id, **payload)
            if verb == "status":
                return {
                    "project_id": project_id,
                    "pilot": routines.status(project_id)["pilot"],
                }
        raise MemoryError("unknown_knowledge_operation")


def _local(service):
    state = State(
        service.config.db_path,
        max_executors=service.config.max_executors,
        provider_limits=service.state.provider_limits,
    )

    def project(project_id):
        row = state.get_project(project_id)
        if (
            not row
            or row["status"] != "registered"
            or not service.config.project_in_scope(row["path"])
        ):
            raise StateError("project_not_registered_or_outside_scope")
        return row

    return SimpleNamespace(
        config=service.config,
        state=state,
        publisher=service.publisher,
        project=project,
        catalog=service.catalog,
        router=Router(service.config, state),
    )


async def handle(service, envelope):
    read_only = envelope.kind in {
        "knowledge.query",
        "knowledge.read",
        "knowledge.source",
        "knowledge.audit",
        "knowledge.report",
        "knowledge.capture.plan",
        "knowledge.migration.plan",
        "knowledge.routine.status",
        "knowledge.pilot.status",
    }
    if not read_only and service.state.get_meta("runtime_maintenance"):
        raise StateError("maintenance_admission_closed")

    def work():
        local = _local(service)
        try:
            result = KnowledgeCommands(local).command(envelope)
            if (
                envelope.kind != "knowledge.read"
                and len(canonical_json(result).encode()) >= MAX_ENVELOPE_BYTES - 4096
            ):
                reference = MemoryArtifacts(service.config.state_dir).create(
                    {
                        "project_id": envelope.project_id,
                        "job_id": "knowledge",
                        "payload": {},
                        "context": {
                            "result": result,
                            "bundle": {
                                "project_id": envelope.project_id,
                                "documents": [],
                            },
                            "context": {},
                        },
                    }
                )
                return {
                    "state": "artifact",
                    "project_id": envelope.project_id,
                    "artifact": reference,
                }
            return result
        finally:
            local.state.close()

    service.memory_operations += 1
    try:
        async with service.memory_lock:
            data = await asyncio.to_thread(work)
    finally:
        service.memory_operations -= 1
    return {
        "ok": True,
        "request_id": envelope.request_id,
        "state": data.get("state", "ok"),
        "data": data,
    }


async def tick(service):
    projects = [
        row
        for row in service.state.list_projects()
        if row["status"] == "registered"
        and service.config.project_in_scope(row["path"])
        and service.state.get_meta(
            "second_brain:routines:" + project_key(row["id"]), {}
        ).get("enabled")
        is True
    ]
    for project in projects:

        def work(project_id=project["id"]):
            local = _local(service)
            try:
                return KnowledgeRoutines(local).tick(project_id)
            finally:
                local.state.close()

        async with service.memory_lock:
            try:
                result = await asyncio.to_thread(work)
            except Exception as exc:
                # One bad vault page must not abort the scheduler before dispatch.
                service.log.warning(
                    "knowledge routine deferred: %s project=%s",
                    type(exc).__name__,
                    project["id"],
                )
            else:
                if result.get("state") == "scheduled":
                    service.log.info(
                        "knowledge routine queued %d bounded jobs",
                        len(result["results"]),
                    )
