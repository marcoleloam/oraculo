"""A current origin policy decision, never retrospective review provenance.

The local host relay and original report/log producers are trusted. Digests bind
their bytes, not the truth of a semantic claim. Only an original host receipt
correlated with canonical failed execution and confirmed memory is eligible.
No provider operation, technical approval or integration authority is issued.
"""

import json
import time

from oraculo.delivery_review import check_log, target
from oraculo.legacy_host_artifact import host_artifact
from oraculo.managed_tasks import ReportValidationError, git
from oraculo.protocol import canonical_json, content_hash
from oraculo.review_adjudication import (
    descendant_binding,
    memory_report_digest,
    read_private,
)
from oraculo.run_continuity import report_link
from oraculo.state import StateError
from oraculo.task_recovery import TaskRecovery


class LegacyDeliveryRecovery:
    def __init__(self, engine):
        self.engine, self.store = engine, engine.store

    @staticmethod
    def history(task):
        events = task.get("events", [])
        if (
            not events
            or events[0].get("phase") != "received"
            or events[0].get("proof", {}).get("request_sha256")
            != content_hash(task["request"])
            or [e.get("sequence") for e in events] != list(range(1, len(events) + 1))
            or task.get("snapshot_revision") != len(events)
            or any(
                e.get("signature")
                != content_hash(dict(phase=e["phase"], proof=e["proof"]))
                for e in events
            )
        ):
            raise StateError("legacy_delivery_canonical_history_incomplete")

    async def facts(self, task, snapshot):
        self.history(task)
        report = task.get("report") or task.get("submitted_report")
        kind, failed = self.engine._validate_report(task, report)
        if kind != "implementation" or failed:
            raise StateError("legacy_delivery_positive_implementation_required")
        if task.get("review_adjudication") or any(
            k in task["request"] for k in ("task_role", "review_target")
        ):
            raise StateError("legacy_delivery_unclassified_request_required")
        execution = report_link(task, snapshot, report)
        if snapshot["thread"]["status"] != "completed":
            raise StateError("legacy_delivery_completed_execution_required")
        ws = self.engine._report_workspace(task, snapshot["thread"])
        if ws["dirty"] or ws["head"] != report["commit"]:
            raise StateError("validated_commit_or_clean_workspace_required")
        descendants = descendant_binding(snapshot)
        if any(not descendant["runs"] for descendant in descendants):
            raise StateError("legacy_delivery_descendant_history_required")
        tasks = {}
        for row in self.store.db.execute("SELECT id FROM managed_tasks"):
            child = self.store.get(row[0])
            thread_id = (child.get("child") or {}).get("childThreadId")
            if thread_id:
                if thread_id in tasks:
                    raise StateError("legacy_delivery_ambiguous_child_binding")
                tasks[thread_id] = child
        raw = {r["thread"]["threadId"]: r for r in snapshot["descendants"]}
        interruptions = []
        for descendant in descendants:
            if descendant["status"] == "completed":
                continue
            child = tasks.get(descendant["thread_id"])
            if child is None:
                raise StateError("legacy_delivery_original_child_record_required")
            self.history(child)
            binding = child["child"]
            # Only direct native children of this implementation. Nested scope
            # or explicit functional roles need their own evidence/lifecycle.
            if (
                child.get("transport_mode") != "native_child"
                or descendant["parent_thread_id"] != task["child"]["childThreadId"]
                or child["origin"]["thread_id"] != descendant["parent_thread_id"]
                or child["project"]["project_id"] != task["project"]["project_id"]
                or child["project"]["repository_common_dir"]
                != task["project"]["repository_common_dir"]
                or "task_role" in child["request"]
                or "review_target" in child["request"]
            ):
                raise StateError("legacy_delivery_original_relation_unproven")
            original = child.get("report")
            if not original or child.get("submitted_report", original) != original:
                raise StateError("legacy_delivery_original_report_required")
            self.engine._validate_report(child, original)
            # A host label alone is insufficient. A final verdict, findings,
            # functional/quality check, or undocumented negative is ineligible.
            if (
                not self.engine._report_failed(original)
                or original.get("verdict") is not None
                or original.get("summary", "").lstrip().upper().startswith("FAIL")
                or original.get("findings")
                or original.get("prior_findings")
                or any(c.get("layer") != "host" for c in original["checks"])
            ):
                raise StateError("legacy_delivery_negative_scope_unproven")
            view = raw[descendant["thread_id"]]
            history = report_link(child, view, original)
            if (
                not child.get("completion_evidence")
                or history != child["completion_evidence"]
                or history["runs"][0]["runId"] != binding["childRunId"]
                or child.get("work_status") != "failed"
                or descendant["status"] not in {"failed", "interrupted", "cancelled"}
                # A continuation can contain technical work not described by
                # the old host-only report. Do not infer its operational scope.
                or len(history["runs"]) != 1
            ):
                raise StateError("legacy_delivery_original_execution_unproven")
            historical_base = child["base_sha"]
            if original.get("commit") not in {None, historical_base}:
                raise StateError("legacy_delivery_original_target_unproven")
            git(
                ws["path"],
                "merge-base",
                "--is-ancestor",
                historical_base,
                report["commit"],
                error="legacy_delivery_historical_base_not_ancestor",
            )
            child_ws = self.engine._report_workspace(child, view["thread"])
            if child_ws["dirty"] or child_ws["head"] != historical_base:
                raise StateError("legacy_delivery_interruption_effect_uncertain")
            logs = []
            for index, check in enumerate(original["checks"]):
                proof = check_log(child, check)
                reference = dict(path=proof["proof"], sha256=proof["sha256"])
                artifact = host_artifact(
                    read_private(reference, task, limit=16777216),
                    child,
                    set(self.store.describe(child["id"]))
                    | {
                        "source",
                        "protocol_version",
                        "admission_at_launch",
                        "admission",
                    },
                    descendant["status"],
                )
                logs.append(
                    dict(
                        index=index,
                        check_sha256=content_hash(check),
                        **proof,
                        historical_artifact=artifact,
                    )
                )
            if not any(c["status"] == "failed" for c in original["checks"]):
                raise StateError("legacy_delivery_original_failed_check_required")
            if not child.get("memory"):
                raise StateError("legacy_delivery_failed_memory_required")
            cycle = await self.engine.memory.status(
                task["project"]["project_id"], child["parent"], child["memory"]["id"]
            )
            if (
                not self.engine._saved(cycle)
                or cycle.get("id") != child["memory"]["id"]
                or cycle.get("project_id") != task["project"]["project_id"]
                or cycle.get("work_status") != "failed"
                or any(
                    cycle.get("identity", {}).get(k) != child["parent"][k]
                    for k in ("thread_id", "run_id")
                )
                or memory_report_digest(cycle, original) != cycle.get("report_sha256")
                or not self.engine._saved(child["memory"])
                or cycle["write_receipt"] != child["memory"]["write_receipt"]
            ):
                raise StateError("legacy_delivery_failed_memory_required")
            interruptions.append(
                dict(
                    task_id=child["id"],
                    thread_id=binding["childThreadId"],
                    original_child_run_id=binding["childRunId"],
                    parent_thread_id=descendant["parent_thread_id"],
                    historical_base=historical_base,
                    final_head_reviewed=False,
                    request_sha256=content_hash(child["request"]),
                    report_sha256=content_hash(original),
                    execution=history,
                    host_status=descendant["status"],
                    terminal_corroboration=dict(
                        basis="current_caller_bound_host_snapshot",
                        temporal_scope="post_execution",
                        project_id=view["thread"]["projectId"],
                        thread_id=descendant["thread_id"],
                        parent_thread_id=descendant["parent_thread_id"],
                        original_run_id=binding["childRunId"],
                        runs=descendant["runs"],
                        host_status=descendant["status"],
                        review_approval=False,
                    ),
                    checks=logs,
                    memory_cycle_id=cycle["id"],
                    memory_report_sha256=cycle["report_sha256"],
                    memory_receipt_sha256=content_hash(cycle["write_receipt"]),
                )
            )
        if not interruptions:
            raise StateError("legacy_delivery_interrupted_descendant_required")
        facts = dict(
            target=target(task),
            implementation_base=task["base_sha"],
            workspace=ws,
            project_id=task["project"]["project_id"],
            origin=task["origin"],
            child=task["child"],
            execution=execution,
            descendants=descendants,
            interruptions=interruptions,
            implementation_checks=[check_log(task, c) for c in report["checks"]],
        )
        if len(canonical_json(facts).encode()) > 393216:
            raise StateError("legacy_delivery_evidence_limit")
        return facts

    async def diagnose(self, task, actor):
        TaskRecovery.origin(task, actor, dict(reason="Read legacy delivery evidence"))
        snapshot = await self.engine._terminal_gate(task)
        try:
            facts = await self.facts(task, snapshot)
        except StateError as exc:
            return dict(eligible=False, missing_evidence=str(exc), certification=False)
        return dict(
            eligible=True,
            evidence=facts,
            certification=False,
            authority="current_automated_origin_policy_only",
        )

    async def validate(self, task, snapshot, intent):
        try:
            doc = json.loads(read_private(intent["decision"], task, limit=524288))
        except (ValueError, UnicodeError):
            raise StateError("legacy_delivery_policy_document_required") from None
        facts = await self.facts(task, snapshot)
        if (
            not isinstance(doc, dict)
            or set(doc) != {"kind", "author", "scope", "evidence", "dispositions"}
            or doc.get("kind") != "post_execution_delivery_policy_v1"
            or doc.get("author") != "automated_origin"
            or doc.get("scope") != "implementation_delivery_only"
            or doc.get("evidence") != facts
        ):
            raise StateError("legacy_delivery_evidence_changed_or_scope_mismatch")
        expected = [
            {
                "task_id": r["task_id"],
                "failed_checks": [
                    dict(
                        index=c["index"],
                        check_sha256=c["check_sha256"],
                        log_sha256=c["sha256"],
                    )
                    for c in r["checks"]
                    if c["status"] == "failed"
                ],
            }
            for r in facts["interruptions"]
        ]
        dispositions = doc["dispositions"]
        if not isinstance(dispositions, list) or len(dispositions) != len(expected):
            raise StateError("legacy_delivery_explicit_dispositions_required")
        for row, binding in zip(dispositions, expected, strict=True):
            if (
                not isinstance(row, dict)
                or set(row) != set(binding) | {"scope", "rationale"}
                or any(row.get(k) != v for k, v in binding.items())
                or row.get("scope") != "host_interruption_only_no_technical_verdict"
                or not isinstance(row.get("rationale"), str)
                or not 0 < len(row["rationale"].strip()) <= 2000
            ):
                raise StateError("legacy_delivery_explicit_dispositions_required")
        return doc

    async def accept(self, task, payload, actor):
        try:
            TaskRecovery.origin(task, actor, payload)
            if set(payload) != {"task_id", "reason", "decision"}:
                raise StateError("legacy_delivery_payload_required")
            audit = task.get("legacy_delivery_decision")
            if audit and audit["intent"] != payload:
                raise StateError("legacy_delivery_intent_reused")
            report = task.get("report") or task.get("submitted_report")
            await self.engine._prevalidate_report(task, report)
            snapshot = await self.engine._terminal_gate(task)
            doc = await self.validate(task, snapshot, payload)
            if not audit:
                if (
                    task["work_status"] != "pending"
                    or task.get("completion_evidence")
                    or task.get("publication")
                    or task.get("origin_completion")
                ):
                    raise StateError("legacy_delivery_existing_evidence_immutable")
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
                    raise StateError("legacy_delivery_memory_not_pristine")
                self.store.update(
                    task["id"],
                    "legacy_delivery_policy_recorded",
                    dict(decision_sha256=payload["decision"]["sha256"], actor=actor),
                    legacy_delivery_decision=dict(
                        intent=payload,
                        document=doc,
                        actor=actor,
                        at=time.time(),
                        basis="post_execution_automated_origin_policy",
                        authorship="automated_origin",
                        historical_role_asserted=False,
                        final_head_reviewed=False,
                        report_sha256=content_hash(report),
                        decision_sha256=payload["decision"]["sha256"],
                        terminal_snapshot=snapshot,
                        review_approved=False,
                        integration_authorized=False,
                        provider_redispatch_permitted=False,
                        activation_authorized=False,
                    ),
                )
        except StateError as exc:
            if not self.store.get(task["id"]).get("legacy_delivery_decision"):
                raise ReportValidationError(str(exc)) from None
            raise
        await self.engine._finish(task["id"], report, actor)

    async def gate(self, task, snapshot):
        audit = task.get("legacy_delivery_decision")
        if not audit:
            return {}
        doc = await self.validate(task, snapshot, audit["intent"])
        if doc != audit["document"]:
            raise StateError("legacy_delivery_decision_changed")
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
            or (
                self.engine._saved(task["memory"])
                and (
                    not self.engine._saved(cycle)
                    or cycle.get("work_status") != "succeeded"
                    or cycle.get("write_receipt") != task["memory"].get("write_receipt")
                    or memory_report_digest(cycle, task["report"])
                    != cycle.get("report_sha256")
                )
            )
        ):
            raise StateError("legacy_delivery_current_memory_unproven")
        # Memory reads yield too. Observe host/history again after those reads,
        # then check local bytes synchronously before returning this proof.
        fresh = await self.engine._terminal_gate(task)
        facts = doc["evidence"]
        report = task.get("report") or task.get("submitted_report")
        current = self.store.get(task["id"])
        self.history(current)
        if (
            target(current) != facts["target"]
            or current["origin"] != facts["origin"]
            or current["child"] != facts["child"]
            or current["base_sha"] != facts["implementation_base"]
            or descendant_binding(fresh) != facts["descendants"]
            or report_link(task, fresh, report) != facts["execution"]
            or self.engine._report_workspace(task, fresh["thread"])
            != facts["workspace"]
            or [check_log(task, c) for c in report["checks"]]
            != facts["implementation_checks"]
        ):
            raise StateError("legacy_delivery_evidence_changed_during_memory_read")
        rows = {r["thread"]["threadId"]: r for r in fresh["descendants"]}
        for interruption in facts["interruptions"]:
            child = self.store.get(interruption["task_id"])
            self.history(child)
            child_ws = self.engine._report_workspace(
                child, rows[interruption["thread_id"]]["thread"]
            )
            if (
                child_ws["dirty"]
                or child_ws["head"] != interruption["historical_base"]
                or content_hash(child["request"]) != interruption["request_sha256"]
                or content_hash(child.get("report")) != interruption["report_sha256"]
            ):
                raise StateError("legacy_delivery_evidence_changed_during_memory_read")
            for check, fixed in zip(
                child["report"]["checks"], interruption["checks"], strict=True
            ):
                if check_log(child, check) != {
                    k: fixed[k] for k in ("command", "status", "proof", "sha256")
                }:
                    raise StateError(
                        "legacy_delivery_evidence_changed_during_memory_read"
                    )
        return {
            r["thread_id"]: dict(
                task_id=r["task_id"],
                owner_task_id=task["id"],
                thread_id=r["thread_id"],
                host_status=r["host_status"],
                quality_state="not_approved",
                approval=False,
                independence="not_certified_by_delivery",
                human_certification=False,
                provider_restriction="unresolved_or_unproven",
                redispatch_permitted=False,
                provenance="post_execution_origin_policy",
                historical_role_asserted=False,
                historical_base=r["historical_base"],
                final_head_reviewed=False,
            )
            for r in doc["evidence"]["interruptions"]
        }
