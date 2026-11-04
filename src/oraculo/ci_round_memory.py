"""Canonical, append-only CI rounds, independent of implementation delivery.

Only authenticated lifecycle observations enter here. A round is a check snapshot,
not an inferred diagnosis, correction, provider approval or merge authorization.
Existing memory cycles own publication/retry; the task outbox owns notifications.
"""

from __future__ import annotations

import copy
import json

from oraculo.protocol import content_hash
from oraculo.review_adjudication import memory_report_digest, read_private
from oraculo.state import StateError


class CIRoundMemory:
    def __init__(self, engine):
        self.engine = engine
        self.store, self.memory = engine.store, engine.memory

    def _update(self, task_id, records, stage):
        task = self.store.get(task_id)
        self.store.update(
            task_id,
            task["phase"],
            {"ci_round_memory": stage, "round_id": records[-1]["id"]},
            ci_rounds=records,
        )

    @staticmethod
    def execution_scope(execution, required):
        """Project the authenticated observation onto this policy's checks.

        Legacy/unsupported observers may expose runs without proving which check
        they executed. Such checks retain snapshot precision, never a guessed ID.
        """
        if execution is None:
            return None
        return dict(
            event={
                k: execution["event"].get(k)
                for k in ("repository", "pr", "head", "base", "run", "attempt")
            },
            runs=sorted(
                [
                    {k: r[k] for k in ("run", "attempt")}
                    for r in execution["evidence"]["runs"]
                ],
                key=lambda r: r["run"],
            ),
            check_bindings=sorted(
                [
                    c
                    for c in execution["evidence"].get("check_bindings", [])
                    if c["name"] in required
                ],
                key=lambda c: c["name"],
            ),
        )

    async def flush(self, task_id):
        """Resume the same durable intent/cycle after uncertain begin or finish."""
        task = self.store.get(task_id)
        records = copy.deepcopy(task.get("ci_rounds", []))

        def require_binding(cycle, expected_id=None):
            if (expected_id is not None and cycle["id"] != expected_id) or (
                cycle["project_id"] != task["project"]["project_id"]
                or any(
                    cycle["identity"].get(k) != task["parent"][k]
                    for k in ("thread_id", "run_id")
                )
            ):
                raise StateError("ci_round_memory_binding_mismatch")

        for item in records:
            stage = "read"
            try:
                saved = item.get("memory")
                if saved:
                    expected_id = saved["id"]
                    saved = await self.memory.status(
                        task["project"]["project_id"], task["parent"], saved["id"]
                    )
                    require_binding(saved, expected_id)
                else:
                    started = await self.memory.begin(
                        task["project"]["project_id"],
                        task["parent"],
                        item["key"],
                        "CI failure diagnosis correction evaluation history",
                    )
                    saved = started["cycle"]
                    require_binding(saved)
                    item["memory"] = saved
                    self._update(task_id, records, "cycle_observed")
                stage = "write"
                if not self.engine._saved(saved):
                    expected_id = saved["id"]
                    saved = await self.memory.finish(
                        task["project"]["project_id"],
                        task["parent"],
                        saved["id"],
                        item["work_status"],
                        item["report"],
                    )
                    require_binding(saved, expected_id)
                    item["memory"] = saved
                    self._update(task_id, records, "write_observed")
                if not self.engine._saved(saved):
                    raise StateError("ci_round_memory_write_unconfirmed")
                stage = "binding"
                if (
                    saved["project_id"] != task["project"]["project_id"]
                    or any(
                        saved["identity"].get(k) != task["parent"][k]
                        for k in ("thread_id", "run_id")
                    )
                    or saved["work_status"] != item["work_status"]
                    or saved["report_sha256"]
                    != memory_report_digest(saved, item["report"])
                ):
                    raise StateError("ci_round_memory_binding_mismatch")
                if item.get("memory") != saved:
                    item["memory"] = saved
                    self._update(task_id, records, "confirmed")
            except Exception as exc:
                # The failed observation/intent survives. Delivery is immutable.
                if isinstance(exc, StateError) and str(exc).startswith("ci_round_"):
                    raise
                raise StateError("ci_round_memory_" + stage + "_unconfirmed") from exc

    async def _predecessor(self, task):
        evidence = (task.get("report") or {}).get("evidence") or {}
        reference = evidence.get("ci_predecessor")
        if reference is None:
            return None
        if not isinstance(reference, dict) or set(reference) != {"task_id", "round_id"}:
            raise StateError("ci_predecessor_reference_invalid")
        prior = self.store.get(reference["task_id"])
        matches = [
            r for r in prior.get("ci_rounds", []) if r["id"] == reference["round_id"]
        ]
        if (
            prior["id"] == task["id"]
            or prior["project"]["project_id"] != task["project"]["project_id"]
            or len(matches) != 1
            or matches[0]["work_status"] != "failed"
            or not self.engine._saved(matches[0].get("memory") or {})
            or prior["publication"]["repository"] != task["publication"]["repository"]
            or prior["report"]["commit"] == task["report"]["commit"]
            or prior["publication"]["head_sha"] == task["publication"]["head_sha"]
        ):
            raise StateError("ci_predecessor_binding_mismatch")
        # Confirm the historical canonical receipt through the same official
        # recovery path, rather than treating a cached task field as memory.
        await self.flush(prior["id"])
        diagnosis = evidence.get("ci_diagnosis")
        if diagnosis is not None:
            if (
                not isinstance(diagnosis, dict)
                or set(diagnosis) != {"summary", "proof"}
                or not isinstance(diagnosis["summary"], str)
                or not diagnosis["summary"].strip()
            ):
                raise StateError("ci_diagnosis_evidence_required")
            read_private(diagnosis["proof"], task, limit=256 * 1024)
        return dict(
            **reference,
            memory_cycle=matches[0]["memory"]["id"],
            observation_sha256=content_hash(matches[0]["facts"]),
            correction_commit=task["report"]["commit"],
            correction_report_sha256=content_hash(task["report"]),
            diagnosis=diagnosis,
        )

    async def observe(self, task_id, receipt, execution=None):
        """Persist terminal required checks before ready/merge; no local clock ID."""
        task = self.store.get(task_id)
        checks = receipt.get("checks")
        required = task["merge_policy"].get("required_checks", [])
        if not isinstance(checks, list):
            raise StateError("ci_round_checks_unobserved")
        selected = sorted(
            [c for c in checks if c.get("name") in required], key=lambda c: c["name"]
        )
        if len(selected) != len(required) or len({c["name"] for c in selected}) != len(
            required
        ):
            return False
        if not selected or any(c.get("completed") is not True for c in selected):
            return not required
        failures = [c for c in selected if c.get("conclusion") not in {"SUCCESS"}]
        # NEUTRAL/SKIPPED are not invented passes; lifecycle retains its policy.
        status = "failed" if failures else "succeeded"
        runs = []
        if execution is not None:
            event, evidence = execution["event"], execution["evidence"]
            if event.get("conclusion") == "pending":
                return False
            if any(
                event.get(k) != v
                for k, v in dict(
                    repository=receipt["repository"],
                    pr=receipt["number"],
                    head=receipt["head_sha"],
                ).items()
            ):
                raise StateError("ci_round_execution_binding_mismatch")
            runs = sorted(
                [dict(run=r["run"], attempt=r["attempt"]) for r in evidence["runs"]],
                key=lambda r: r["run"],
            )
            if (
                not runs
                or any(
                    not isinstance(r["run"], str)
                    or not r["run"]
                    or type(r["attempt"]) is not int
                    or r["attempt"] < 1
                    for r in runs
                )
                or dict(run=event["run"], attempt=event["attempt"]) not in runs
            ):
                raise StateError("ci_round_execution_identity_unproven")
        identity = dict(
            task_id=task_id,
            project_id=task["project"]["project_id"],
            repository=receipt["repository"],
            pr=receipt["number"],
            head=receipt["head_sha"],
            policy_sha256=content_hash(task["merge_policy"]),
            observed_runs=runs,
            precision="per_check_execution_or_snapshot",
        )
        scope = self.execution_scope(execution, required)
        per_check = []
        for check in selected:
            bindings = [
                c
                for c in (scope or {}).get("check_bindings", [])
                if c["name"] == check["name"]
            ]
            if len(bindings) > 1:
                raise StateError("ci_round_check_binding_ambiguous")
            proof = bindings[0] if bindings else None
            if proof is not None:
                if (
                    {k: proof[k] for k in ("run", "attempt")} not in runs
                    or proof["head"] != receipt["head_sha"]
                    or proof["check_status"] != "completed"
                    or proof["check_conclusion"].upper() != check["conclusion"]
                    or proof["job"]["status"] != "completed"
                    or proof["job"]["conclusion"].upper() != check["conclusion"]
                ):
                    raise StateError("ci_round_check_evidence_mismatch")
                check_identity = {
                    k: proof[k] for k in ("name", "head", "run", "attempt")
                }
                precision = "workflow_check_run_attempt"
            else:
                check_identity = dict(
                    name=check["name"], snapshot_sha256=content_hash(check)
                )
                precision = "check_snapshot_without_run_attempt"
            per_check.append(
                dict(
                    identity=check_identity,
                    precision=precision,
                    check=check,
                    proof=proof,
                )
            )
        identity["check_identities"] = [c["identity"] for c in per_check]
        # Unknown check executions are snapshots, even alongside known workflows.
        ident = "ci-round-" + content_hash(identity)
        facts = dict(
            identity=identity,
            checks=selected,
            work_status=status,
            check_bindings=per_check,
        )
        records = copy.deepcopy(task.get("ci_rounds", []))
        for prior in records:
            prior_identity = prior["facts"]["identity"]
            if prior_identity["head"] == identity["head"] and any(
                r["run"] == old["run"] and r["attempt"] < old["attempt"]
                for r in runs
                for old in prior_identity["observed_runs"]
            ):
                raise StateError("ci_round_stale_attempt")
            if prior_identity["head"] == identity["head"] and any(
                current["precision"] == "workflow_check_run_attempt"
                and current["identity"] == old["identity"]
                and current != old
                for current in per_check
                for old in prior["facts"]["check_bindings"]
            ):
                raise StateError("ci_round_terminal_evidence_changed")
        existing = next((r for r in records if r["id"] == ident), None)
        if existing:
            if existing["facts"] != facts:
                raise StateError("ci_round_terminal_evidence_changed")
        else:
            previous = (
                dict(task_id=task_id, round_id=records[-1]["id"])
                if records
                else await self._predecessor(task)
            )
            report = dict(
                title="CI round for delivered implementation",
                # Put recovery anchors ahead of verbose evidence: canonical wiki
                # extracts have a bounded summary, unlike the full source report.
                summary="CI loop anchor: "
                + json.dumps(
                    dict(
                        round_id=ident,
                        task_id=task_id,
                        project_id=identity["project_id"],
                        repository=identity["repository"],
                        pr=identity["pr"],
                        head=identity["head"],
                        work_status=status,
                        previous={
                            k: v
                            for k, v in (previous or {}).items()
                            if k in {"task_id", "round_id", "correction_commit"}
                        },
                    ),
                    sort_keys=True,
                )
                + f"\nCI checks {status}; implementation delivery remains succeeded; merge not asserted. "
                "CI failure diagnosis correction evaluation history: "
                + json.dumps(dict(facts=facts, previous=previous), sort_keys=True),
                topics=["CI", "failure", "diagnosis", "correction", "evaluation"],
                evidence=dict(
                    task_id=task_id,
                    project_id=identity["project_id"],
                    round_id=ident,
                    previous=previous,
                    facts=facts,
                    execution=execution,
                    implementation_report_sha256=content_hash(task["report"]),
                    implementation_memory_cycle=task["memory"]["id"],
                ),
                limitations=[
                    "Authenticated observation; not a provider review or deployment approval.",
                    "Observed workflow IDs cover host-required checks only; extra managed checks retain snapshot precision.",
                    "Without observed run/attempt IDs, different snapshots cannot prove different executions.",
                    "CI failure alone supplies no diagnosis, correction or redispatch authorization.",
                ],
            )
            records.append(
                dict(
                    id=ident,
                    key=task_id + ":" + ident,
                    facts=facts,
                    work_status=status,
                    report=report,
                    memory=None,
                )
            )
            self._update(task_id, records, "intent")
        await self.flush(task_id)
        return True
