"""Implementation delivery is distinct from quality review and PR integration.

Pre-execution roles and explicit post-execution legacy policy are distinct
provenance. A terminal status or free-form explanation cannot classify a failed
functional test as review. No provider operation is performed here.
"""

import hashlib
import json
from pathlib import Path

from oraculo.protocol import content_hash
from oraculo.review_adjudication import (
    descendant_binding,
    memory_report_digest,
    read_private,
)
from oraculo.run_continuity import report_link
from oraculo.state import StateError

ROLES = frozenset({"implementation", "quality_review", "functional_tests"})


def target(task):
    report = task.get("report") or task.get("submitted_report")
    if not report:
        raise StateError("review_target_report_required")
    return dict(
        task_id=task["id"],
        request_sha256=content_hash(task["request"]),
        report_sha256=content_hash(report),
        commit=report["commit"],
    )


def check_log(task, check):
    if not isinstance(check.get("result"), str) or not check["result"].strip():
        raise StateError("implementation_test_proof_required")
    try:
        path = Path(check["proof"])
        if not path.is_file() or path.stat().st_size > 16777216:
            raise StateError("implementation_test_proof_required")
        with path.open("rb") as stream:
            raw = stream.read(16777217)
        digest = hashlib.sha256(raw).hexdigest()
    except (OSError, KeyError, TypeError):
        raise StateError("implementation_test_proof_required") from None
    read_private(dict(path=str(path), sha256=digest), task, limit=16777216)
    return dict(
        command=check["command"], status=check["status"], proof=str(path), sha256=digest
    )


class DeliveryReview:
    def __init__(self, engine):
        self.engine, self.store = engine, engine.store

    def start_role(self, request, actor):
        role = request.get("task_role", "implementation")
        if role not in ROLES:
            raise StateError("unsupported_task_role")
        if role in {"quality_review", "functional_tests"}:
            if request.get("deliverable_kind") != "validation":
                raise StateError("validation_role_requires_validation_deliverable")
        if role in {"quality_review", "functional_tests"}:
            supplied = request.get("review_target")
            if not isinstance(supplied, dict) or not isinstance(
                supplied.get("task_id"), str
            ):
                raise StateError("quality_review_target_required")
            implementation = self.store.get(supplied["task_id"])
            if supplied != target(implementation) or actor["thread_id"] not in {
                implementation["origin"]["thread_id"],
                implementation["child"]["childThreadId"],
            }:
                raise StateError("quality_review_target_mismatch")

    @staticmethod
    def scoped_snapshot(snapshot, thread_id):
        """Subset observed rows; never rewrite host statuses or run history.

        Native task_status is parent-scoped. Root callers validate a delivered
        descendant using its confirmed receipt and observed subtree, without
        impersonating its parent to query its delegated task.
        """
        rows = {d["thread"]["threadId"]: d for d in snapshot["descendants"]}
        owner = rows[thread_id]
        selected, pending = [], [thread_id]
        while pending:
            parent = pending.pop()
            children = [
                d for d in rows.values() if d["thread"]["parentThreadId"] == parent
            ]
            selected.extend(children)
            pending.extend(d["thread"]["threadId"] for d in children)
        return dict(
            thread=owner["thread"],
            recentRuns=owner["recentRuns"],
            descendants=selected,
            childrenObserved=True,
            observedChildCount=len(selected),
            childrenTerminal=all(d["terminal"] is True for d in selected),
            childrenSuccessful=all(d["successful"] is True for d in selected),
            hasPendingChildRuns=any(d["terminal"] is not True for d in selected),
            _deliveryScopeProofs=snapshot["_deliveryScopeProofs"],
        )

    async def owner(self, task, snapshot, child, descendant, tree_tasks, scopes):
        supplied = child["request"].get("review_target")
        owner = (
            tree_tasks.get(supplied.get("task_id"))
            if isinstance(supplied, dict)
            else None
        )
        if owner is None:
            raise StateError("review_owner_outside_observed_tree")
        if (
            child.get("transport_mode") != "native_child"
            or child["origin"]["thread_id"] != descendant["parent_thread_id"]
            or descendant["parent_thread_id"]
            not in {owner["origin"]["thread_id"], owner["child"]["childThreadId"]}
            or owner["project"]["project_id"] != task["project"]["project_id"]
            or owner["project"]["repository_common_dir"]
            != task["project"]["repository_common_dir"]
        ):
            raise StateError("review_owner_relation_unproven")
        if supplied != target(owner) or child.get("base_sha") != supplied["commit"]:
            raise StateError("review_owner_target_mismatch")
        if owner["id"] != task["id"] and owner["id"] not in scopes:
            cache = snapshot["_deliveryScopeProofs"]
            if owner["id"] in cache:
                if cache[owner["id"]]["target"] != supplied:
                    raise StateError("review_owner_target_mismatch")
                scopes[owner["id"]] = cache[owner["id"]]
                return owner
            report = owner.get("report")
            if (
                not report
                or self.engine._report_failed(report)
                or owner.get("work_status") != "succeeded"
                or not owner.get("completion_evidence")
                or not self.engine._saved(owner.get("memory") or {})
            ):
                raise StateError("review_owner_delivery_unproven")
            view = self.scoped_snapshot(snapshot, owner["child"]["childThreadId"])
            await self.engine._execution_gate(owner, snapshot=view)
            ws = self.engine._report_workspace(owner, view["thread"])
            if ws["dirty"] or ws["head"] != report["commit"]:
                raise StateError("review_owner_delivery_unproven")
            cycle = await self.engine.memory.status(
                task["project"]["project_id"], owner["parent"], owner["memory"]["id"]
            )
            if (
                not self.engine._saved(cycle)
                or cycle.get("id") != owner["memory"]["id"]
                or cycle.get("work_status") != "succeeded"
                or cycle.get("project_id") != task["project"]["project_id"]
                or any(
                    cycle.get("identity", {}).get(k) != owner["parent"][k]
                    for k in ("thread_id", "run_id")
                )
                or memory_report_digest(cycle, report) != cycle.get("report_sha256")
            ):
                raise StateError("review_owner_memory_unproven")
            scopes[owner["id"]] = dict(
                task_id=owner["id"],
                target=supplied,
                execution=report_link(owner, view, report),
                memory_cycle_id=cycle["id"],
                memory_report_sha256=cycle["report_sha256"],
                memory_receipt_sha256=content_hash(cycle["write_receipt"]),
                checks=[check_log(owner, c) for c in report["checks"]],
            )
            cache[owner["id"]] = scopes[owner["id"]]
        return owner

    async def observe(self, task, snapshot, *, enforce=True):
        bound = descendant_binding(snapshot)  # liveness/history never optional
        from oraculo.legacy_delivery import LegacyDeliveryRecovery

        legacy = await LegacyDeliveryRecovery(self.engine).gate(task, snapshot)
        # Per-observation only. Nested gates share this cache to avoid repeated
        # subtree validation; a fresh post-memory snapshot starts a new cache.
        snapshot.setdefault("_deliveryScopeProofs", {})
        tasks = {}
        for row in self.store.db.execute("SELECT id FROM managed_tasks"):
            child = self.store.get(row[0])
            thread_id = (child.get("child") or {}).get("childThreadId")
            if thread_id:
                tasks[thread_id] = child
        reviews, mandatory, unknown = [], [], []
        raw_threads = {
            d["thread"]["threadId"]: d["thread"] for d in snapshot["descendants"]
        }
        raw_descendants = {d["thread"]["threadId"]: d for d in snapshot["descendants"]}
        tree_tasks = {
            child["id"]: child
            for thread_id, child in tasks.items()
            if thread_id in raw_descendants
        }
        tree_tasks[task["id"]] = task
        scopes = {}
        for descendant in bound:
            child = tasks.get(descendant["thread_id"])
            request = (child or {}).get("request", {})
            role = request.get("task_role")
            report = (child or {}).get("report") or (child or {}).get(
                "submitted_report"
            )
            negative = bool(report and self.engine._report_failed(report))
            owner = task
            if role in {"quality_review", "functional_tests"}:
                if (role == "functional_tests" and negative) or any(
                    c["status"] == "failed"
                    and c.get("layer") not in {"host", "quality"}
                    for c in (report or {}).get("checks", [])
                ):
                    raise StateError("mandatory_functional_validation_failed")
                owner = await self.owner(
                    task, snapshot, child, descendant, tree_tasks, scopes
                )
            if role == "quality_review":
                if request.get("deliverable_kind") != "validation" or request.get(
                    "review_target"
                ) != target(owner):
                    raise StateError("quality_review_target_mismatch")
                failed = [
                    c
                    for c in (report or {}).get("checks", [])
                    if c["status"] == "failed"
                ]
                # Mandatory test failures block delivery even in a review task.
                if any(c.get("layer") not in {"host", "quality"} for c in failed):
                    raise StateError("mandatory_functional_validation_failed")
                for check in failed:
                    if check.get("layer") == "host":
                        proof = check_log(child, check)
                        try:
                            receipt = json.loads(
                                read_private(
                                    dict(path=proof["proof"], sha256=proof["sha256"]),
                                    task,
                                    limit=16777216,
                                )
                            )
                        except (ValueError, UnicodeError):
                            raise StateError(
                                "quality_review_interruption_receipt_unproven"
                            ) from None
                        if (
                            not isinstance(receipt, dict)
                            or any(
                                receipt.get(k) != child["child"].get(k)
                                for k in ("taskId", "childThreadId", "childRunId")
                            )
                            or receipt.get("status") != descendant["status"]
                            or receipt.get("workState") != "result_available"
                            or receipt.get("hasPendingChildRuns") is not False
                        ):
                            raise StateError(
                                "quality_review_interruption_receipt_unproven"
                            )
                if (
                    negative
                    and (
                        report.get("verdict") == "FAIL"
                        or report.get("summary", "").lstrip().upper().startswith("FAIL")
                    )
                    and not any(c.get("layer") == "quality" for c in failed)
                ):
                    raise StateError("negative_review_scope_unproven")
                state = (
                    "changes_requested"
                    if any(c.get("layer") == "quality" for c in failed)
                    or (report or {}).get("findings")
                    else "not_approved"
                )
                latest = descendant["runs"][-1] if descendant["runs"] else {}
                reviews.append(
                    dict(
                        task_id=child["id"],
                        owner_task_id=owner["id"],
                        thread_id=descendant["thread_id"],
                        original_child_run_id=child["child"]["childRunId"],
                        runs=descendant["runs"],
                        host_status=descendant["status"],
                        host_thread=raw_threads[descendant["thread_id"]],
                        quality_state=state,
                        request_sha256=content_hash(request),
                        report_sha256=content_hash(report) if report else None,
                        memory=dict(
                            cycle_id=(child.get("memory") or {}).get("id"),
                            work_status=(child.get("memory") or {}).get("work_status"),
                            write_state=(child.get("memory") or {}).get("write_state"),
                            write_receipt_sha256=content_hash(
                                child["memory"]["write_receipt"]
                            )
                            if (child.get("memory") or {}).get("write_receipt")
                            else None,
                        ),
                        provider_instance_id=latest.get("providerInstanceId"),
                        model=latest.get("model"),
                        approval=False,
                        independence="not_certified_by_delivery",
                        human_certification=False,
                        provider_restriction="unresolved_or_unproven"
                        if descendant["status"] != "completed"
                        else "not_assessed_by_delivery",
                        redispatch_permitted=False,
                    )
                )
            elif role == "functional_tests":
                if request.get("review_target") != target(owner):
                    raise StateError("functional_validation_target_mismatch")
                if negative:
                    raise StateError("mandatory_functional_validation_failed")
                if (
                    descendant["status"] != "completed"
                    or not report
                    or child.get("work_status") != "succeeded"
                    or not self.engine._saved(child.get("memory") or {})
                    or not child.get("completion_evidence")
                ):
                    raise StateError("mandatory_functional_validation_unproven")
                ws = self.engine._report_workspace(
                    child, raw_threads[descendant["thread_id"]]
                )
                if (
                    ws["dirty"]
                    or ws["head"] != report.get("commit")
                    or report.get("commit") != target(owner)["commit"]
                ):
                    raise StateError("mandatory_functional_validation_unproven")
                cycle = await self.engine.memory.status(
                    task["project"]["project_id"],
                    child["parent"],
                    child["memory"]["id"],
                )
                if (
                    not self.engine._saved(cycle)
                    or cycle.get("id") != child["memory"]["id"]
                    or cycle.get("work_status") != "succeeded"
                    or cycle.get("project_id") != task["project"]["project_id"]
                    or any(
                        cycle.get("identity", {}).get(k) != child["parent"][k]
                        for k in ("thread_id", "run_id")
                    )
                    or memory_report_digest(cycle, report) != cycle.get("report_sha256")
                ):
                    raise StateError("mandatory_functional_memory_unproven")
                mandatory.append(
                    dict(
                        task_id=child["id"],
                        thread_id=descendant["thread_id"],
                        original_child_run_id=child["child"]["childRunId"],
                        request_sha256=content_hash(request),
                        target=request["review_target"],
                        report_sha256=content_hash(report),
                        memory_cycle_id=cycle["id"],
                        memory_report_sha256=cycle["report_sha256"],
                        memory_receipt_sha256=content_hash(cycle["write_receipt"]),
                        execution=report_link(
                            child, raw_descendants[descendant["thread_id"]], report
                        ),
                        checks=[check_log(child, c) for c in report["checks"]],
                    )
                )
            elif descendant["thread_id"] in legacy:
                reviews.append(legacy[descendant["thread_id"]])
            elif descendant["status"] != "completed":
                unknown.append(descendant["thread_id"])
            elif negative:
                raise StateError("descendant_negative_scope_unproven")
        if unknown and enforce:
            raise StateError("terminal_descendant_review_required")
        report = task.get("report") or task.get("submitted_report")
        checks = (
            [check_log(task, c) for c in report["checks"]]
            if reviews or mandatory
            else []
        )
        return dict(
            state="implementation_delivery_only",
            target=target(task),
            quality_reviews=reviews,
            mandatory_validations=mandatory,
            scope_validations=[scopes[k] for k in sorted(scopes)],
            unknown_descendant_roles=unknown,
            implementation_checks=checks,
            review_approved=False,
            integration_authorized=False,
            activation_observed=False,
            provider_redispatch_permitted=False,
        )

    async def gate(self, task, snapshot):
        if task.get("review_adjudication"):
            return  # Explicit legacy human alternative has its own full validator.
        evidence = await self.observe(task, snapshot)
        saved = task.get("delivery_review")
        if (
            saved
            and saved.get("implementation_checks") != evidence["implementation_checks"]
        ):
            raise StateError("implementation_test_evidence_changed")
        if (
            saved
            and saved.get("mandatory_validations") != evidence["mandatory_validations"]
        ):
            raise StateError("mandatory_functional_evidence_changed")
        if (
            saved
            and saved.get("scope_validations", []) != evidence["scope_validations"]
        ):
            raise StateError("nested_delivery_evidence_changed")
        snapshot["deliveryReview"] = evidence
