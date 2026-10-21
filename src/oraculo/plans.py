"""Bounded executable plans built on the existing durable job/grant protocol."""

from __future__ import annotations

import json
import time
from pathlib import PurePosixPath
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from oraculo.config import StrictModel
from oraculo.protocol import canonical_json, content_hash, validate_memory_submission
from oraculo.state import State, StateError


class Criterion(StrictModel):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    description: str = Field(min_length=1, max_length=2000)


class TaskSpec(StrictModel):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    kind: str = Field(default="implementation", min_length=1, max_length=100)
    stage: Literal[
        "implementation",
        "validation",
        "review",
        "research",
        "memory",
        "publication",
        "deployment",
        "acceptance",
    ] = "implementation"
    owner: str = Field(min_length=1, max_length=200)
    brief: str = Field(min_length=1, max_length=16000)
    sources: list[str] = Field(default_factory=list, max_length=32)
    write_paths: list[str] = Field(default_factory=lambda: ["*"], max_length=64)
    depends_on: list[str] = Field(default_factory=list, max_length=32)
    criteria: list[Criterion] = Field(min_length=1, max_length=32)
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("write_paths")
    @classmethod
    def safe_paths(cls, paths: list[str]) -> list[str]:
        for path in paths:
            p = PurePosixPath(path)
            if path == "*":
                continue
            if (
                not path
                or p.is_absolute()
                or ".." in p.parts
                or str(p) != path
                or path == "."
                or any(c in path for c in "*?[]\\\x00:")
            ):
                raise ValueError(
                    "write_paths must be canonical relative prefixes or '*'"
                )
        return sorted(set(paths))

    @model_validator(mode="after")
    def valid_task(self) -> TaskSpec:
        if len({c.id for c in self.criteria}) != len(self.criteria):
            raise ValueError("duplicate criterion id")
        if len(set(self.depends_on)) != len(self.depends_on):
            raise ValueError("duplicate dependency id")
        if not self.owner.strip() or not self.brief.strip():
            raise ValueError("owner and brief must not be blank")
        if (self.kind == "memory") != (self.stage == "memory"):
            raise ValueError("memory kind and stage must agree")
        validate_memory_submission(dict(self.payload, kind=self.kind))
        if {
            "task_spec",
            "kind",
            "text",
            "project_id",
            "objective_id",
        } & self.payload.keys():
            raise ValueError("reserved task payload key")
        return self


class PlanSpec(StrictModel):
    text: str = Field(min_length=1, max_length=16000)
    tasks: list[TaskSpec] = Field(min_length=1, max_length=32)
    max_followups: int = Field(default=4, ge=0, le=4)
    max_parallel: int | None = Field(default=None, ge=1, strict=True)

    @model_validator(mode="after")
    def dag(self) -> PlanSpec:
        if not self.text.strip():
            raise ValueError("objective text must not be blank")
        tasks = {t.id: t for t in self.tasks}
        if len(tasks) != len(self.tasks):
            raise ValueError("duplicate task id")
        done: set[str] = set()
        while len(done) < len(tasks):
            ready = {
                key
                for key, task in tasks.items()
                if key not in done and set(task.depends_on) <= done
            }
            if not ready:
                raise ValueError("cyclic or unknown dependencies")
            done |= ready
        return self


class Plans:
    def __init__(self, state: State):
        self.state = state
        state.connection.execute(
            "CREATE TABLE IF NOT EXISTS objective_plans(objective_id TEXT PRIMARY KEY REFERENCES objectives(id),spec TEXT NOT NULL,submission_hash TEXT NOT NULL,job_ids TEXT NOT NULL,followups INTEGER NOT NULL DEFAULT 0)"
        )

    def get(self, objective_id: str) -> dict | None:
        row = self.state._one(
            "SELECT * FROM objective_plans WHERE objective_id=?", (objective_id,)
        )
        if row:
            row["spec"] = json.loads(row["spec"])
            row["job_ids"] = json.loads(row["job_ids"])
        return row

    def submit(
        self, project_id: str, raw: dict, dedupe_key: str, profiles: dict
    ) -> dict:
        spec = PlanSpec.model_validate(raw)
        if not isinstance(dedupe_key, str) or not dedupe_key.strip():
            raise StateError("plan_dedupe_key_required")
        if any(t.kind not in profiles for t in spec.tasks):
            raise StateError("unknown_job_kind")
        data = spec.model_dump(mode="json")
        if spec.max_parallel is None:
            # Preserve ORA-CONT-003 submission hashes for replays after upgrade.
            data.pop("max_parallel")
        digest = content_hash(data)
        with self.state.transaction():
            objective = self.state.submit_objective(
                project_id, spec.text, dedupe_key=dedupe_key
            )
            previous = self.get(objective["id"])
            if previous:
                if previous["submission_hash"] != digest:
                    raise StateError("plan_dedupe_conflict")
                return self.describe(objective["id"])
            if self.state.list_jobs(objective["id"]):
                raise StateError("objective_already_has_unplanned_jobs")
            jobs: dict[str, str] = {}
            self._enqueue(spec.tasks, objective["id"], jobs)
            self.state.connection.execute(
                "INSERT INTO objective_plans VALUES(?,?,?,?,0)",
                (objective["id"], canonical_json(data), digest, canonical_json(jobs)),
            )
            if spec.max_parallel is not None:
                self.state.set_meta(
                    f"plan_parallel:{objective['id']}", spec.max_parallel
                )
            return self.describe(objective["id"])

    def configure(self, objective_id: str, maximum: int) -> dict:
        if type(maximum) is not int or maximum < 1:
            raise StateError("max_parallel_must_be_positive_integer")
        with self.state.transaction():
            if not self.get(objective_id):
                raise StateError("planned_objective_required")
            self.state.set_meta(f"plan_parallel:{objective_id}", maximum)
            self.state.audit(
                "plan.configure",
                {
                    "objective_id": objective_id,
                    "max_parallel": maximum,
                },
            )
            return self.describe(objective_id)

    def _enqueue(
        self, tasks: list[TaskSpec], objective_id: str, jobs: dict[str, str]
    ) -> None:
        pending = list(tasks)
        while pending:
            ready = [t for t in pending if set(t.depends_on) <= jobs.keys()]
            if not ready:
                raise StateError("invalid_plan_dependencies")
            for task in ready:
                payload = dict(
                    task.payload,
                    text=task.brief,
                    task_spec=task.model_dump(exclude={"payload"}, mode="json"),
                )
                jobs[task.id] = self.state.enqueue_job(
                    objective_id, task.kind, payload, [jobs[d] for d in task.depends_on]
                )["id"]
                pending.remove(task)

    def extend(self, attempt: dict, raw: dict, profiles: dict) -> dict:
        """An active task may add bounded followups only within its declared scope."""
        with self.state.transaction():
            current = self.state.validate_grant(attempt["id"], attempt["generation"])
            if current["status"] != "running" or current["job_id"] != attempt["job_id"]:
                raise StateError("active_source_task_required")
            job = self.state.get_job(current["job_id"])
            plan = self.get(job["objective_id"])
            if not plan:
                raise StateError("planned_objective_required")
            task = TaskSpec.model_validate(raw)
            original = json.loads(job["payload"])["task_spec"]
            existing = next(
                (t for t in plan["spec"]["tasks"] if t["id"] == task.id), None
            )
            if existing:
                if existing != task.model_dump(mode="json"):
                    raise StateError("followup_id_conflict")
                source = self.state.get_meta(
                    f"followup_source:{job['objective_id']}:{task.id}"
                )
                if source != job["id"]:
                    raise StateError("followup_source_mismatch")
                return self.describe(job["objective_id"])
            if plan["followups"] >= plan["spec"]["max_followups"]:
                raise StateError("followup_budget_exhausted")
            if task.kind not in profiles or task.stage in {
                "publication",
                "deployment",
                "acceptance",
                "memory",
            }:
                raise StateError("followup_scope_requires_new_authorization")
            if (
                original["id"] not in task.depends_on
                or not set(task.depends_on) <= plan["job_ids"].keys()
            ):
                raise StateError("followup_requires_source_dependency")
            if any(
                not any(
                    base == "*" or path == base or path.startswith(base + "/")
                    for base in original["write_paths"]
                )
                for path in task.write_paths
            ):
                raise StateError("followup_exceeds_write_scope")
            self._enqueue([task], job["objective_id"], plan["job_ids"])
            plan["spec"]["tasks"].append(task.model_dump(mode="json"))
            self.state.connection.execute(
                "UPDATE objective_plans SET spec=?,job_ids=?,followups=followups+1 WHERE objective_id=?",
                (
                    canonical_json(plan["spec"]),
                    canonical_json(plan["job_ids"]),
                    job["objective_id"],
                ),
            )
            self.state.set_meta(
                f"followup_source:{job['objective_id']}:{task.id}", job["id"]
            )
            return self.describe(job["objective_id"])

    def validate_report(self, attempt: dict, report: dict) -> None:
        job = self.state.get_job(attempt["job_id"])
        spec = json.loads(job["payload"]).get("task_spec")
        if not spec or report.get("status") != "succeeded":
            return
        evidence = report.get("evidence")
        receipts = evidence.get("criteria", []) if isinstance(evidence, dict) else []
        if not isinstance(receipts, list):
            raise StateError("criterion_receipts_required")
        expected_ids = {c["id"] for c in spec["criteria"]}
        if any(
            not isinstance(receipt, dict)
            or not isinstance(receipt.get("id"), str)
            or receipt["id"] not in expected_ids
            for receipt in receipts
        ):
            raise StateError("unknown_criterion_receipt")
        for criterion in spec["criteria"]:
            matched = [
                r
                for r in receipts
                if isinstance(r, dict) and r.get("id") == criterion["id"]
            ]
            if (
                len(matched) != 1
                or matched[0].get("status") != "passed"
                or not isinstance(matched[0].get("reference"), str)
                or not matched[0]["reference"].strip()
            ):
                raise StateError("criterion_not_satisfied:" + criterion["id"])
        if spec["stage"] in {"publication", "deployment"}:
            kinds = (
                {"deploy"} if spec["stage"] == "deployment" else {"push", "merge", "pr"}
            )
            operations = self.state.connection.execute(
                "SELECT kind,state FROM operations WHERE attempt_id=?", (attempt["id"],)
            ).fetchall()
            if not any(
                o["kind"] in kinds and o["state"] == "confirmed" for o in operations
            ):
                raise StateError("delivery_operation_not_confirmed")

    def retry(self, objective_id: str, task_id: str, max_attempts: int) -> dict:
        if type(max_attempts) is not int or max_attempts < 1:
            raise StateError("invalid_attempt_budget")
        with self.state.transaction():
            plan = self.get(objective_id)
            if not plan or task_id not in plan["job_ids"]:
                raise StateError("unknown_plan_task")
            obj = self.state.get_objective(objective_id)
            if obj["status"] in {"cancel_requested", "stopped", "succeeded"}:
                raise StateError("objective_not_retryable")
            job_id = plan["job_ids"][task_id]
            job = self.state.get_job(job_id)
            attempts = self.state.connection.execute(
                "SELECT * FROM attempts WHERE job_id=? ORDER BY number", (job_id,)
            ).fetchall()
            if (
                job["state"] not in {"failed", "blocked"}
                or len(attempts) >= max_attempts
                or any(self.state.lease(a["id"]) for a in attempts)
            ):
                raise StateError("task_not_retryable_or_budget_exhausted")
            unresolved = self.state._one(
                "SELECT o.id FROM operations o JOIN attempts a ON a.id=o.attempt_id "
                "WHERE a.job_id=? AND o.state IN ('prepared','dispatched','uncertain')",
                (job_id,),
            )
            if unresolved or any(
                a["status"] not in {"failed", "stopped", "succeeded"} for a in attempts
            ):
                raise StateError("task_requires_reconciliation")
            self.state.connection.execute(
                "UPDATE jobs SET state='queued' WHERE id=?", (job_id,)
            )
            self.state.set_meta(f"blocker:{job_id}", None)
            return self.describe(objective_id)

    def describe(self, objective_id: str) -> dict:
        # Keep task states, cancellation, and the aggregate update in one snapshot.
        with self.state.transaction():
            return self._describe(objective_id)

    def _describe(self, objective_id: str) -> dict:
        plan = self.get(objective_id)
        if not plan:
            return {}
        obj = self.state.get_objective(objective_id)
        tasks = []
        for spec in plan["spec"]["tasks"]:
            job = self.state.get_job(plan["job_ids"][spec["id"]])
            attempts = [
                dict(r)
                for r in self.state.connection.execute(
                    "SELECT * FROM attempts WHERE job_id=? ORDER BY number",
                    (job["id"],),
                )
            ]
            last = attempts[-1] if attempts else None
            report = (
                self.state.get_meta(f"execution_report:{last['id']}", {})
                if last
                else {}
            )
            missing = [
                d
                for d in spec["depends_on"]
                if self.state.get_job(plan["job_ids"][d])["state"] != "succeeded"
            ]
            state = job["state"]
            blocker = self.state.get_meta(f"blocker:{job['id']}")
            if state == "queued" and missing:
                state = "waiting_dependencies"
                blocker = {
                    "reason": "dependencies_pending",
                    "owner": spec["owner"],
                    "action": "Complete or resolve: " + ", ".join(missing),
                }
            elif state == "queued" and job["next_attempt_at"] > time.time():
                state = "waiting_retry"
                blocker = {
                    "reason": "retry_backoff",
                    "owner": spec["owner"],
                    "action": "Wait until the recorded retry time",
                    "retry_at": job["next_attempt_at"],
                }
            elif (
                state
                in {
                    "uncertain",
                    "failed",
                    "blocked",
                    "auth_required",
                    "quota_wait",
                }
                and not blocker
            ):
                blocker = {
                    "reason": state,
                    "owner": spec["owner"],
                    "action": "Reconcile original T3 execution"
                    if state == "uncertain"
                    else "Restore an authorized provider credential"
                    if state == "auth_required"
                    else "Wait for authorized provider capacity"
                    if state == "quota_wait"
                    else "Inspect evidence; retry within budget or cancel",
                }
            tasks.append(
                {
                    "id": spec["id"],
                    "job_id": job["id"],
                    "kind": spec["kind"],
                    "stage": spec["stage"],
                    "owner": spec["owner"],
                    "state": state,
                    "depends_on": spec["depends_on"],
                    "write_paths": spec["write_paths"],
                    "criteria": spec["criteria"],
                    "blocker": blocker,
                    "evidence": report.get("evidence"),
                    "attempts": [
                        {
                            "attempt_id": a["id"],
                            "task_id": a["task_id"],
                            "thread_id": a["thread_id"],
                            "run_id": a["run_id"],
                            "state": a["status"],
                            "link": "t3-thread://v1/" + a["thread_id"]
                            if a["thread_id"]
                            else None,
                        }
                        for a in attempts
                    ],
                }
            )
        states = {t["state"] for t in tasks}
        if obj["status"] in {"cancel_requested", "stopped"}:
            status = obj["status"]
        elif states == {"succeeded"}:
            status = "succeeded"
        elif states & {"admitted", "running", "uncertain"}:
            status = "running"
        elif states & {"failed", "blocked", "stopped", "auth_required", "quota_wait"}:
            status = "blocked"
        else:
            status = "queued"
        self.state.connection.execute(
            "UPDATE objectives SET status=? WHERE id=?", (status, objective_id)
        )
        stages = {}
        for stage in (
            "implementation",
            "validation",
            "review",
            "research",
            "memory",
            "publication",
            "deployment",
            "acceptance",
        ):
            entries = [t for t in tasks if t["stage"] == stage]
            stages[stage] = (
                "not_requested"
                if not entries
                else "succeeded"
                if all(t["state"] == "succeeded" for t in entries)
                else "pending"
            )
        ready = [
            t["id"]
            for t in tasks
            if t["state"] == "queued"
            and obj["status"] not in {"cancel_requested", "stopped"}
        ]
        active = [
            t["id"] for t in tasks if t["state"] in {"running", "admitted", "uncertain"}
        ]
        return {
            "objective_id": objective_id,
            "text": obj["text"],
            "status": status,
            "delivery_contract_satisfied": status == "succeeded",
            "stages": stages,
            "tasks": tasks,
            "ready": ready,
            "active": active,
            "next_action": "Reconcile active tasks"
            if active
            else "Cancellation requested; no successors will be dispatched"
            if status == "cancel_requested"
            else "Objective stopped; no successors will be dispatched"
            if status == "stopped"
            else "Dispatch: " + ", ".join(ready)
            if ready
            else "Delivery contract satisfied"
            if status == "succeeded"
            else "Wait for the recorded retry time"
            if "waiting_retry" in states
            else "Resolve blockers or cancel",
            "followups": plan["followups"],
            "max_followups": plan["spec"]["max_followups"],
            "concurrency": self.state.plan_capacity(objective_id),
        }
