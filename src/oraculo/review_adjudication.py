"""Human evidence for an interrupted descendant; never rewrite host success.

The local operator/host relay is the trust boundary. A hash proves integrity,
not that somebody reviewed code. Actual human host messages must attest the
technical artifact and approve its exact binding. No alternate model is called.
"""

import hashlib
import json
import re
import time
from pathlib import Path

from oraculo.managed_tasks import ReportValidationError, git
from oraculo.memory.cycles import report_projection
from oraculo.protocol import canonical_json, content_hash
from oraculo.run_continuity import report_link, terminal_chain
from oraculo.state import StateError
from oraculo.t3.client import T3Client
from oraculo.t3.contracts import ContractError


def descendant_binding(snapshot):
    """All descendants, including successful ones, must retain their run chains."""
    if (
        snapshot.get("childrenObserved") is not True
        or snapshot.get("childrenTerminal") is not True
        or not isinstance(snapshot.get("descendants"), list)
        or len(snapshot["descendants"]) != snapshot.get("observedChildCount")
    ):
        raise StateError("thread_children_pending_or_unknown")
    result = []
    for child in snapshot["descendants"]:
        thread = child["thread"]
        runs = terminal_chain(child) if thread["latestRunId"] else []
        result.append(
            dict(
                thread_id=thread["threadId"],
                parent_thread_id=thread["parentThreadId"],
                status=thread["status"],
                runs=runs,
            )
        )
    return sorted(result, key=lambda c: c["thread_id"])


def _child_root(task):
    root = (task.get("child_workspace") or {}).get("path")
    if not root:
        raise StateError("child_workspace_not_ready")
    return root


def read_private(reference, task, *, limit):
    if not isinstance(reference, dict) or set(reference) != {"path", "sha256"}:
        raise StateError("review_artifact_reference_required")
    child_root = _child_root(task)
    try:
        path = Path(reference["path"])
        if not path.is_absolute():
            raise ValueError
        path = path.resolve(strict=True)
        # Technical evidence is private, never tracked in the delivery checkout.
        roots = [
            root
            for root in (
                (task.get("project") or {}).get("project_path"),
                child_root,
                (task.get("parent_workspace") or {}).get("path"),
            )
            if root
        ]
        if any(path.is_relative_to(Path(root).resolve()) for root in roots):
            raise StateError("review_evidence_must_be_private")
        if not path.is_file():
            raise ValueError
        with path.open("rb") as stream:
            raw = stream.read(limit + 1)
        if not raw or len(raw) > limit:
            raise ValueError
        if hashlib.sha256(raw).hexdigest() != reference["sha256"]:
            raise StateError("review_artifact_digest_mismatch")
        return raw
    except StateError:
        raise
    except (OSError, TypeError, ValueError, KeyError):
        raise StateError("review_artifact_unavailable") from None


def coverage(task, commit):
    cwd = _child_root(task)
    paths = git(
        cwd,
        "diff",
        "--name-only",
        "--no-renames",
        "-z",
        task["base_sha"],
        commit,
        "--",
        strip=False,
    ).split("\0")
    result = []
    for path in sorted(p for p in paths if p):
        # Deleted files have no final blob. Renames cover both old and new paths.
        rows = git(
            cwd, "--literal-pathspecs", "ls-tree", "-z", commit, "--", path, strip=False
        ).split("\0")
        row = next((r for r in rows if r and r.split("\t", 1)[-1] == path), None)
        result.append(dict(path=path, blob_oid=row.split()[2] if row else None))
    return result


def memory_report_digest(cycle, original):
    # Public memory status intentionally excludes the private report. Rebuild
    # its canonical envelope from the immutable original and fresh cycle fields,
    # then compare to the service's stored digest; never query the vault/SQLite.
    expected = dict(
        schema_version=1,
        cycle_id=cycle["id"],
        project_id=cycle["project_id"],
        identity=cycle["identity"],
        work_status=cycle["work_status"],
        started_at=cycle["created_at"],
        finished_at=cycle["finished_at"],
        read_state=cycle["read_state"],
        read_receipt=cycle["read_receipt"],
        deliveries=cycle["deliveries"],
        evidence=report_projection(original)[0],
        scope="selected_structured_report; not a conversation archive",
    )
    if cycle.get("initial_finish_rejection"):
        expected["initial_finish_rejection"] = cycle["initial_finish_rejection"]
        expected["finish_rejection"] = cycle["finish_rejection"]
    return content_hash(expected)


class ReviewAdjudication:
    def __init__(self, engine):
        self.engine, self.store = engine, engine.store

    async def diagnose(self, task):
        if not task.get("child"):
            raise ReportValidationError("review_bound_child_required")
        client = T3Client("http://127.0.0.1/mcp")
        client.call = self.engine.host.call
        try:
            snapshot = await client.settlement_snapshot(
                task["child"]["childThreadId"],
                terminal_only=True,
                complete_runs=True,
            )
        except (StateError, ContractError, OSError) as exc:
            code = (
                str(exc)
                if isinstance(exc, (StateError, ContractError))
                else type(exc).__name__
            )
            return dict(
                reason=code,
                certification="not_granted_by_diagnosis",
                missing_evidence=[code],
            )
        reason = (
            "thread_children_pending_or_unknown"
            if snapshot["childrenTerminal"] is not True
            else "terminal_descendant_review_required"
            if snapshot["childrenSuccessful"] is not True
            else "descendants_successful"
        )
        report = task.get("report") or task.get("submitted_report")
        delivery = None
        if report and snapshot.get("childrenTerminal") is True:
            from oraculo.delivery_review import DeliveryReview

            try:
                delivery = await DeliveryReview(self.engine).observe(
                    task, snapshot, enforce=False
                )
                if (
                    delivery["quality_reviews"]
                    and not delivery["unknown_descendant_roles"]
                ):
                    reason = "implementation_delivery_separate_from_quality_review"
            except StateError as exc:
                delivery = dict(state="delivery_evidence_incomplete", reason=str(exc))
        return dict(
            reason=reason,
            snapshot=snapshot,
            binding=dict(
                task_id=task["id"],
                project_id=task["project"]["project_id"],
                origin_thread_id=task["origin"]["thread_id"],
                child_run_id=task["child"]["childRunId"],
                request_sha256=content_hash(task["request"]),
                report_sha256=content_hash(report) if report else None,
                commit=report.get("commit") if report else None,
            ),
            missing_evidence=[
                "Immutable pre-execution task role and exact target binding for optional quality review",
                "Complete terminal execution and passing mandatory functional test evidence",
                "CI/PR review remains separate; no human message is a generic delivery prerequisite",
            ]
            if reason == "terminal_descendant_review_required"
            else [],
            certification="not_granted_by_diagnosis",
            delivery_review=delivery,
        )

    async def document(self, task, snapshot, reference):
        raw = read_private(reference, task, limit=131072)
        try:
            doc = json.loads(raw)
        except (ValueError, UnicodeError):
            raise StateError("technical_human_review_required") from None
        report = task.get("report") or task.get("submitted_report")
        kind, failed = self.engine._validate_report(task, report)
        if kind != "implementation" or failed:
            raise StateError("negative_validation_not_adjudicable")
        expected = dict(
            task_id=task["id"],
            project_id=task["project"]["project_id"],
            origin_thread_id=task["origin"]["thread_id"],
            child_run_id=task["child"]["childRunId"],
            request_sha256=content_hash(task["request"]),
            report_sha256=content_hash(report),
            commit=report["commit"],
        )
        if not isinstance(doc, dict) or any(
            doc.get(k) != v for k, v in expected.items()
        ):
            raise StateError("review_scope_or_head_mismatch")
        if (
            set(doc)
            != set(expected)
            | {
                "kind",
                "verdict",
                "descendants",
                "coverage",
                "scope_assessment",
                "findings",
                "checks",
                "reviewer",
                "operational_dispositions",
            }
            or doc["kind"] != "human_review_v1"
            or doc["verdict"] != "PASS"
        ):
            raise StateError("technical_human_review_required")
        reviewer = doc["reviewer"]
        if (
            not isinstance(reviewer, dict)
            or set(reviewer) != {"name", "independence_attestation"}
            or any(not isinstance(v, str) or not v.strip() for v in reviewer.values())
        ):
            raise StateError("human_reviewer_attestation_required")
        if doc["descendants"] != descendant_binding(snapshot):
            raise StateError("review_descendants_changed")
        if not any(c["status"] != "completed" for c in doc["descendants"]):
            raise StateError("unsuccessful_descendant_required")
        await self.dispositions(task, doc)
        if (
            not isinstance(doc["scope_assessment"], str)
            or not doc["scope_assessment"].strip()
        ):
            raise StateError("technical_human_review_required")
        expected_coverage = coverage(task, report["commit"])
        rows = doc["coverage"]
        if (
            not isinstance(rows, list)
            or any(
                not isinstance(r, dict)
                or set(r) != {"path", "blob_oid", "assessment"}
                or not isinstance(r["assessment"], str)
                or not r["assessment"].strip()
                for r in rows
            )
            or [{k: r[k] for k in ("path", "blob_oid")} for r in rows]
            != expected_coverage
        ):
            raise StateError("review_diff_coverage_required")
        # Findings must be closed in the human review, with technical resolution.
        if not isinstance(doc["findings"], list) or any(
            not isinstance(f, dict)
            or set(f) != {"finding", "resolution", "status"}
            or f["status"] != "resolved"
            or any(
                not isinstance(f[k], str) or not f[k].strip()
                for k in ("finding", "resolution")
            )
            for f in doc["findings"]
        ):
            raise StateError("unresolved_review_findings")
        checks = doc["checks"]
        if not isinstance(checks, list) or len(checks) != len(report["checks"]):
            raise StateError("review_test_evidence_required")
        for check, original in zip(checks, report["checks"], strict=True):
            if (
                not isinstance(check, dict)
                or set(check) != {"command", "status", "result", "proof", "sha256"}
                or check["status"] != "passed"
                or any(
                    check.get(k) != original.get(k)
                    for k in ("command", "status", "result", "proof")
                )
                or not isinstance(check["result"], str)
                or not check["result"].strip()
            ):
                raise StateError("review_test_evidence_required")
            read_private(
                dict(path=check["proof"], sha256=check["sha256"]), task, limit=16777216
            )
        return doc, expected

    async def dispositions(self, task, doc):
        """A human may distinguish host interruption, never waive technical FAIL.

        Layer=host is only an eligibility filter. Each original failed check,
        run, scope, report, log and confirmed failed-memory receipt must be
        individually disposed in the human-attested technical document.
        """
        supplied = doc["operational_dispositions"]
        if not isinstance(supplied, list):
            raise StateError("human_operational_disposition_required")
        descendants = {c["thread_id"]: c for c in doc["descendants"]}
        expected_ids = set()
        for row in self.store.db.execute("SELECT id FROM managed_tasks"):
            child = self.store.get(row[0])
            binding = child.get("child") or {}
            if binding.get("childThreadId") not in descendants:
                continue
            original = child.get("report") or child.get("submitted_report")
            if not original or not self.engine._report_failed(original):
                continue
            if (
                original.get("verdict") == "FAIL"
                or re.match(r"^\s*FAIL\b", original.get("summary", ""), re.I)
                or any(
                    c["status"] == "failed" and c.get("layer") != "host"
                    for c in original["checks"]
                )
                or (
                    child.get("submitted_report")
                    and child["submitted_report"] != original
                )
            ):
                raise StateError("negative_descendant_report_not_adjudicable")
            expected_ids.add(child["id"])
            matches = [
                d
                for d in supplied
                if isinstance(d, dict) and d.get("task_id") == child["id"]
            ]
            if len(matches) != 1:
                raise StateError("human_operational_disposition_required")
            disposition = matches[0]
            chain = descendants[binding["childThreadId"]]["runs"]
            if (
                len(chain) != 1
                or chain[0]["runId"] != binding.get("childRunId")
                or chain[0]["status"] not in {"failed", "interrupted", "cancelled"}
                or child.get("work_status") != "failed"
                or not child.get("memory")
            ):
                raise StateError("operational_failure_binding_unproven")
            cycle = await self.engine.memory.status(
                task["project"]["project_id"], child["parent"], child["memory"]["id"]
            )
            if (
                not self.engine._saved(cycle)
                or cycle.get("work_status") != "failed"
                or cycle.get("id") != child["memory"]["id"]
                or cycle.get("project_id") != task["project"]["project_id"]
                or any(
                    cycle.get("identity", {}).get(k) != child["parent"][k]
                    for k in ("thread_id", "run_id")
                )
                or memory_report_digest(cycle, original) != cycle.get("report_sha256")
            ):
                raise StateError("operational_failure_memory_unproven")
            expected = dict(
                task_id=child["id"],
                thread_id=binding["childThreadId"],
                child_run_id=binding["childRunId"],
                request_sha256=content_hash(child["request"]),
                report_sha256=content_hash(original),
                memory_cycle_id=cycle["id"],
                memory_report_sha256=cycle["report_sha256"],
                memory_receipt_sha256=content_hash(cycle["write_receipt"]),
                classification="operational_review_interruption_without_verdict",
            )
            if (
                set(disposition) != set(expected) | {"rationale", "failed_checks"}
                or any(disposition.get(k) != v for k, v in expected.items())
                or not isinstance(disposition["rationale"], str)
                or not disposition["rationale"].strip()
            ):
                raise StateError("operational_disposition_binding_mismatch")
            failed = [
                (i, c)
                for i, c in enumerate(original["checks"])
                if c["status"] == "failed"
            ]
            checks = disposition["failed_checks"]
            if not isinstance(checks, list) or len(checks) != len(failed):
                raise StateError("human_operational_disposition_required")
            for check, (index, original_check) in zip(checks, failed, strict=True):
                if (
                    not isinstance(check, dict)
                    or set(check) != {"index", "check_sha256", "proof", "assessment"}
                    or type(check["index"]) is not int
                    or check["index"] != index
                    or check["check_sha256"] != content_hash(original_check)
                    or not isinstance(check["assessment"], str)
                    or not check["assessment"].strip()
                    or not isinstance(check["proof"], dict)
                    or check["proof"].get("path") != original_check.get("proof")
                ):
                    raise StateError("operational_disposition_check_mismatch")
                read_private(check["proof"], task, limit=16777216)
        if (
            len(supplied) != len(expected_ids)
            or {d.get("task_id") for d in supplied if isinstance(d, dict)}
            != expected_ids
        ):
            raise StateError("unexpected_operational_disposition")

    async def human_message(self, task, source, text, *, review=False):
        if not isinstance(source, dict) or set(source) != {
            "thread_id",
            "item_id",
            "message_id",
        }:
            raise StateError("observable_human_message_required")
        if review:
            if source["thread_id"] in {
                task["origin"]["thread_id"],
                task["child"]["childThreadId"],
            }:
                raise StateError("separate_human_review_thread_required")
        elif source["thread_id"] != task["origin"]["thread_id"]:
            raise StateError("review_approval_requires_origin")
        observed = await self.engine.host.call(
            "t3_thread_read",
            dict(
                threadId=source["thread_id"],
                itemId=source["item_id"],
                view="messages",
                limit=1,
                runLimit=1,
                maxCharsPerItem=32000,
            ),
        )
        items = observed.get("items", [])
        if (
            observed.get("thread", {}).get("threadId") != source["thread_id"]
            or observed["thread"].get("projectId") != task["project"]["project_id"]
            or len(items) != 1
        ):
            raise StateError("observable_human_message_required")
        item = items[0]
        if (
            item.get("itemId") != source["item_id"]
            or item.get("messageId") != source["message_id"]
            or item.get("sourceThreadId") != source["thread_id"]
            or item.get("visibility") != "local"
            or item.get("type") != "user_message"
            or item.get("createdBy") != "user"
            or item.get("creationSource") not in {"web", "mobile"}
            or item.get("textTruncated") is not False
            or item.get("text") != text
        ):
            raise StateError("observable_human_message_required")
        return item

    async def validate(self, task, snapshot, intent):
        doc, binding = await self.document(task, snapshot, intent["review"])
        binding = dict(binding, review_sha256=intent["review"]["sha256"])
        reviewer = await self.human_message(
            task,
            intent["review_source"],
            "ORACULO_HUMAN_REVIEW_V1\n" + canonical_json(binding),
            review=True,
        )
        approval = await self.human_message(
            task,
            intent["approval"],
            "ORACULO_REVIEW_APPROVAL_V1\n"
            + canonical_json(
                dict(
                    binding,
                    review_message_id=reviewer["messageId"],
                    review_thread_id=intent["review_source"]["thread_id"],
                )
            ),
        )
        return doc, dict(review=reviewer, approval=approval)

    async def accept(self, task, payload, actor):
        try:
            await self._accept(task, payload, actor)
        except StateError as exc:
            # Input rejection before acceptance has no ledger/outbox effects.
            # After acceptance, preserve the audit and any confirmed memory;
            # failure of a fresh gate must persist as a blocked release.
            if not self.store.get(task["id"]).get("review_adjudication"):
                raise ReportValidationError(str(exc)) from None
            raise

    async def _accept(self, task, payload, actor):
        from oraculo.task_recovery import TaskRecovery

        TaskRecovery.origin(task, actor, payload)
        if set(payload) != {"task_id", "reason", "review", "review_source", "approval"}:
            raise ReportValidationError("review_adjudication_payload_required")
        audit = task.get("review_adjudication")
        if audit and audit["intent"] != payload:
            raise ReportValidationError("review_adjudication_intent_reused")
        report = task.get("report") or task.get("submitted_report")
        kind, failed = await self.engine._prevalidate_report(task, report)
        if kind != "implementation" or failed:
            raise ReportValidationError("negative_validation_not_adjudicable")
        snapshot = await self.engine._terminal_gate(task)
        report_link(task, snapshot, report, actor=actor)
        doc, messages = await self.validate(task, snapshot, payload)
        if not audit:
            if (
                task.get("work_status") != "pending"
                or task.get("completion_evidence")
                or task.get("publication")
            ):
                raise ReportValidationError(
                    "review_adjudication_existing_evidence_immutable"
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
                raise ReportValidationError("review_adjudication_memory_not_pristine")
            self.store.update(
                task["id"],
                "review_adjudication_recorded",
                {
                    "review_sha256": payload["review"]["sha256"],
                    "actor": actor,
                    "host_children_successful": snapshot["childrenSuccessful"],
                },
                review_adjudication=dict(
                    intent=payload,
                    actor=actor,
                    at=time.time(),
                    document=doc,
                    messages=messages,
                    terminal_snapshot=snapshot,
                    report_sha256=content_hash(report),
                    basis="human_review_after_terminal_descendant_failure",
                ),
            )
        await self.engine._finish(task["id"], report, actor)

    async def gate(self, task, snapshot):
        audit = task.get("review_adjudication")
        if audit:
            doc, messages = await self.validate(task, snapshot, audit["intent"])
            if doc != audit["document"] or messages != audit["messages"]:
                raise StateError("review_evidence_changed")
