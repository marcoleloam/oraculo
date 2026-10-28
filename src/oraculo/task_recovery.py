"""Explicit origin decisions; host failures and original reports stay immutable."""

import time

from oraculo.managed_tasks import ReportValidationError
from oraculo.protocol import content_hash
from oraculo.run_continuity import report_link, terminal_chain
from oraculo.state import StateError


class TaskRecovery:
    def __init__(self, engine):
        self.engine = engine
        self.store = engine.store

    @staticmethod
    def origin(task, actor, payload):
        if actor["thread_id"] != task["origin"]["thread_id"] or actor[
            "project_id"
        ] != task["origin"].get("project_id"):
            raise ReportValidationError("recovery_requires_origin")
        reason = payload.get("reason")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
            raise ReportValidationError("recovery_reason_required")
        if not task.get("child") or not task.get("memory"):
            raise ReportValidationError("recovery_bound_child_required")

    async def complete(self, task, payload, actor):
        self.origin(task, actor, payload)
        if set(payload) != {
            "task_id",
            "report",
            "reason",
            "expected_child_run_id",
            "expected_latest_run_id",
        }:
            raise ReportValidationError("origin_completion_payload_required")
        report = payload["report"]
        audit = task.get("origin_completion")
        if audit:
            if audit["intent"] != payload:
                raise ReportValidationError("origin_completion_intent_reused")
            if self.engine._certified(task) and not self.engine._reservation_held(
                task["id"]
            ):
                return
        kind, failed = await self.engine._prevalidate_report(task, report)
        if (
            kind != "implementation"
            or failed
            or any(
                not isinstance(c.get("proof"), str)
                or not c["proof"].strip()
                or not isinstance(c.get("result"), str)
                or not c["result"].strip()
                for c in report["checks"]
            )
        ):
            raise ReportValidationError("independent_validation_proof_required")
        if not audit:
            if (
                task.get("submitted_report") is not None
                or task.get("report") is not None
                or task.get("completion_evidence")
                or task.get("report_recovery")
                or task.get("supersession")
                or task.get("work_status") != "pending"
                or task.get("publication")
                or task.get("merged")
                or task.get("settled")
            ):
                raise ReportValidationError(
                    "origin_completion_existing_evidence_immutable"
                )
            cycle = await self.engine.memory.status(
                task["project"]["project_id"], task["parent"], task["memory"]["id"]
            )
            if (
                cycle.get("id") != task["memory"]["id"]
                or cycle.get("project_id") != task["project"]["project_id"]
                or any(
                    cycle.get("identity", {}).get(k) != task["parent"][k]
                    for k in ("thread_id", "run_id")
                )
                or cycle.get("work_status") != "running"
                or cycle.get("write_state") != "not_requested"
                or cycle.get("write_receipt") is not None
                or cycle.get("report_sha256") is not None
            ):
                raise ReportValidationError("origin_completion_memory_not_pristine")
            snapshot = await self.engine._terminal_gate(task)
            chain = terminal_chain(snapshot)
            if (
                len(chain) != 1
                or chain[0]["status"] != "failed"
                or payload["expected_child_run_id"] != task["child"]["childRunId"]
                or payload["expected_latest_run_id"] != chain[-1]["runId"]
            ):
                raise ReportValidationError(
                    "origin_completion_single_failed_run_required"
                )
            ws = self.engine._report_workspace(task, snapshot["thread"])
            if ws["dirty"] or ws["head"] != report["commit"]:
                raise ReportValidationError(
                    "validated_commit_or_clean_workspace_required"
                )
            # Preserve the failed host observation and caller's justification
            # before saving any successful work result. Never rewrite childRunId.
            audit = dict(
                intent=payload,
                actor=actor,
                at=time.time(),
                child=task["child"],
                runs=chain,
                terminal_snapshot=snapshot,
                report_sha256=content_hash(report),
                basis="independent_origin_validation_after_host_failure",
            )
            self.store.update(
                task["id"],
                "origin_validation_accepted",
                {
                    "reason": payload["reason"],
                    "actor": actor,
                    "report_sha256": content_hash(report),
                    "host_status": "failed",
                    "run_id": chain[0]["runId"],
                },
                origin_completion=audit,
            )
        await self.engine._finish(task["id"], report, actor)

    async def supersede(self, task, payload, actor):
        self.origin(task, actor, payload)
        if set(payload) - {"task_id", "replacement_task_id", "reason", "report"}:
            raise ReportValidationError("supersession_payload_required")
        replacement_id = payload.get("replacement_task_id")
        if not isinstance(replacement_id, str) or replacement_id == task["id"]:
            raise ReportValidationError("supersession_replacement_required")
        replacement = self.store.get(replacement_id)
        if (
            any(
                replacement["origin"].get(k) != task["origin"].get(k)
                for k in ("thread_id", "project_id")
            )
            or any(
                (replacement.get("project") or {}).get(k) != task["project"].get(k)
                for k in ("project_id", "project_path", "repository_common_dir")
            )
            or not replacement.get("child")
        ):
            raise ReportValidationError("supersession_replacement_binding_mismatch")
        seen = {task["id"]}
        for _ in range(100):
            if replacement["id"] in seen:
                raise ReportValidationError("cyclic_supersession")
            seen.add(replacement["id"])
            if not replacement.get("supersession"):
                break
            replacement = self.store.get(
                replacement["supersession"]["replacement_task_id"]
            )
        else:
            raise ReportValidationError("supersession_chain_limit")
        audit = task.get("supersession")
        if audit and audit["intent"] != payload:
            raise ReportValidationError("supersession_intent_reused")
        if audit and task["phase"] == "superseded":
            return
        report = (
            task.get("report") or task.get("submitted_report") or payload.get("report")
        )
        if payload.get("report") is not None and report != payload["report"]:
            raise ReportValidationError("terminal_report_reused")
        _, failed = self.engine._validate_report(task, report)
        if not self.engine._certified(task) and not failed:
            raise ReportValidationError("supersession_negative_report_required")
        # Supersession records abandonment of this execution. Its checkout may
        # already have been removed; it certifies no source commit or success.
        # The original thread/project/run binding and terminal descendants still
        # require fresh proof both before and after confirmed memory.
        snapshot = await self.engine._terminal_gate(task, workspace_required=False)
        evidence = report_link(task, snapshot, report, actor=actor)
        if not audit:
            audit = dict(
                intent=payload,
                replacement_task_id=replacement_id,
                actor=actor,
                at=time.time(),
                report_sha256=content_hash(report),
                terminal_snapshot=snapshot,
                completion_evidence=evidence,
            )
            self.store.update(
                task["id"],
                "supersession_pending",
                {
                    "replacement_task_id": replacement_id,
                    "reason": payload["reason"],
                    "actor": actor,
                },
                supersession=audit,
            )
        try:
            await self.engine._finish(
                task["id"], report, actor, workspace_required=False
            )
        except StateError as exc:
            if str(exc) != "work_not_successful":
                raise
        current = self.store.get(task["id"])
        if not self.engine._certified(current) or self.engine._reservation_held(
            task["id"]
        ):
            raise StateError("supersession_certification_required")
        self.store.update(
            task["id"],
            "superseded",
            {
                "replacement_task_id": replacement_id,
                "reason": payload["reason"],
                "report_sha256": content_hash(report),
                "work_status": current["work_status"],
            },
        )
