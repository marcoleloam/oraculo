"""Cooperative direct-thread adapter; this is not a T3 lifecycle hook."""

from __future__ import annotations

from oraculo.memory.transport import MemoryArtifacts
from oraculo.protocol import content_hash
from oraculo.state import StateError


def public_cycle(cycle: dict) -> dict:
    """Status never returns source documents or the private finish report."""
    result = {
        k: v
        for k, v in cycle.items()
        if k
        not in {
            "context",
            "report",
            "proposal",
            "query",
            "evidence",
            "initial_identity",
            "cycle_key",
        }
    }
    result["display"] = memory_display(cycle)
    return result


def memory_display(cycle: dict) -> dict:
    """Only a confirmed publisher receipt may render the word 'salva'."""
    read = cycle.get("read_receipt") or {}
    write = cycle.get("write_receipt") or {}
    saved = (
        cycle.get("write_state") == "saved"
        and write.get("state") == "confirmed"
        and write.get("operation_id") == cycle["id"]
        and bool(write.get("commit"))
    )
    return {
        "receipt_id": cycle["id"],
        "start": {
            "state": {"found": "consultada", "empty": "vazia"}.get(
                cycle.get("read_state"), "indisponível"
            ),
            "at": read.get("at"),
        },
        "finish": {
            "state": "salva"
            if saved
            else "falhou"
            if cycle.get("write_state") in {"failed", "blocked", "saved"}
            else "pendente",
            "at": write.get("at")
            if saved
            else cycle.get("last_failure_at", cycle.get("finished_at")),
            "commit": write.get("commit") if saved else None,
        },
    }


def artifact_context(cycle: dict) -> dict:
    return {
        **cycle["context"],
        "bundle": cycle["context"].get(
            "bundle", {"project_id": cycle["project_id"], "documents": []}
        ),
        "context": {},
        "read_state": cycle["read_state"],
        "read_receipt": cycle["read_receipt"],
    }


class MemoryBridge:
    def __init__(self, service):
        self.service = service
        self.cycles = service.memory_cycles
        self.artifacts = MemoryArtifacts(service.config.state_dir)

    def artifact(self, cycle: dict) -> dict:
        # Re-read inside a short ledger transaction: a delayed begin must not
        # replace a snapshot produced by a more recent retry. No vault I/O here.
        with self.service.state.transaction():
            cycle = self.cycles.get(cycle["id"])
            reference = self.artifacts.create(
                {
                    "project_id": cycle["project_id"],
                    "job_id": cycle["id"],
                    "payload": {},
                    "context": artifact_context(cycle),
                }
            )
            reference.update(
                read_via="memory.cycle.read",
                cycle_id=cycle["id"],
                read_receipt_sha256=content_hash(cycle["read_receipt"]),
            )
            self.service.state.set_meta("cycle_artifact:" + cycle["id"], reference)
            return reference

    def describe_with_artifact(self, cycle: dict) -> dict:
        with self.service.state.transaction():
            reference = self.artifact(cycle)
            return dict(public_cycle(self.cycles.get(cycle["id"])), artifact=reference)

    def command(self, envelope) -> dict:
        self.service.project(envelope.project_id)
        p = envelope.payload
        if envelope.kind == "memory.cycle.repair":
            # Local operator command, like objective.cancel; never impersonates a
            # completed executor. The original rejected report hash is retained.
            if envelope.origin.thread_id or envelope.origin.run_id:
                raise StateError("memory_repair_requires_local_operator")
            cycle = self.cycles.get(p.get("cycle_id", ""))
            if cycle["project_id"] != envelope.project_id:
                raise StateError("memory_cycle_project_mismatch")
            if cycle["write_state"] == "blocked" or "evidence" in p:
                if not isinstance(p.get("evidence"), dict):
                    raise StateError("memory_repair_evidence_required")
                cycle = self.cycles.finish(
                    cycle["id"], cycle["work_status"], p["evidence"]
                )
            self.service.state.audit(
                "memory_cycle_operator_retry",
                {"cycle_id": cycle["id"], "project_id": cycle["project_id"]},
            )
            return public_cycle(
                cycle
                if cycle["write_state"] == "blocked"
                else self.cycles.retry_write(cycle["id"])
            )
        thread, run = envelope.origin.thread_id, envelope.origin.run_id
        if not thread or not run:
            raise StateError("memory_cycle_thread_and_run_required")
        action = envelope.kind.removeprefix("memory.cycle.")
        if action == "begin":
            key, query = p.get("key"), p.get("query", "")
            if not isinstance(key, str) or not key or len(key) > 200:
                raise StateError("memory_cycle_key_required")
            if not isinstance(query, str) or len(query) > 8000:
                raise StateError("invalid_memory_cycle_query")
            cycle = self.cycles.begin(
                envelope.project_id,
                f"direct:{thread}:{run}:{key}",
                query,
                {"thread_id": thread, "run_id": run, "entry": "direct"},
            )
            return self.describe_with_artifact(cycle)
        cycle = self.cycles.get(p.get("cycle_id", ""))
        if (
            not cycle
            or cycle["project_id"] != envelope.project_id
            or cycle["identity"].get("thread_id") != thread
            or cycle["identity"].get("run_id") != run
            or cycle["identity"].get("entry") != "direct"
        ):
            raise StateError("memory_cycle_origin_mismatch")
        if action == "read":
            with self.service.state.transaction():
                cycle = self.cycles.get(cycle["id"])
                reference = self.service.state.get_meta("cycle_artifact:" + cycle["id"])
                if not reference:
                    raise StateError("memory_cycle_artifact_missing")
                if reference.get("read_receipt_sha256") != content_hash(
                    cycle["read_receipt"]
                ):
                    reference = self.artifact(cycle)
                if p.get("artifact", reference["artifact"]) != reference["artifact"]:
                    raise StateError("memory_cycle_artifact_version_changed")
                page = self.artifacts.read(
                    reference["artifact"],
                    envelope.project_id,
                    cycle["id"],
                    p.get("offset", 0),
                )
                self.cycles.delivered(cycle["id"], thread, run, reference["artifact"])
                return page
        if action == "finish":
            cycle = self.cycles.finish(
                cycle["id"], p.get("status"), p.get("evidence", {})
            )
            return public_cycle(
                cycle
                if cycle["write_state"] == "blocked"
                else self.cycles.publish_one(cycle["id"])
            )
        if action == "retry":
            if p.get("phase") == "read":
                cycle = self.cycles.retry_read(cycle["id"])
                return self.describe_with_artifact(cycle)
            return public_cycle(self.cycles.retry_write(cycle["id"]))
        if action == "status":
            return public_cycle(cycle)
        raise StateError("unsupported_memory_cycle_command")
