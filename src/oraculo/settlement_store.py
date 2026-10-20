"""Durable settlement intent and observations, separate from execution success."""

from __future__ import annotations

import json
import re
import time
import uuid
from typing import Any

from oraculo.protocol import canonical_json
from oraculo.state import DedupeConflict, State, StateError

STATES = {"pending", "blocked", "requested", "uncertain", "confirmed", "reopened"}


class SettlementStore:
    """Append-only decision history with immutable identity and one request claim.

    Creating the store applies an additive migration to the Oráculo database.
    No tables belonging to the T3 application are read or written here.
    """

    def __init__(self, state: State) -> None:
        self.state = state
        with state.transaction() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS settlements("
                "id TEXT PRIMARY KEY,dedupe_key TEXT UNIQUE,"
                "project_id TEXT NOT NULL REFERENCES projects(id),"
                "objective_id TEXT NOT NULL REFERENCES objectives(id),"
                "task_id TEXT NOT NULL,job_id TEXT REFERENCES jobs(id),"
                "thread_id TEXT NOT NULL UNIQUE,run_id TEXT NOT NULL,"
                "binding TEXT NOT NULL,state TEXT NOT NULL,"
                "request_claimed INTEGER NOT NULL DEFAULT 0,"
                "evidence TEXT,last_error TEXT,created_at REAL NOT NULL,"
                "updated_at REAL NOT NULL,UNIQUE(objective_id,task_id))"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS settlement_events("
                "id INTEGER PRIMARY KEY,settlement_id TEXT NOT NULL "
                "REFERENCES settlements(id),state TEXT NOT NULL,"
                "evidence TEXT,last_error TEXT,created_at REAL NOT NULL)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS settlement_events_binding "
                "ON settlement_events(settlement_id,id)"
            )
            connection.execute("INSERT OR IGNORE INTO schema_migrations VALUES(3)")

    @staticmethod
    def _text(value: Any, field: str) -> str:
        if (
            not isinstance(value, str)
            or not value
            or value != value.strip()
            or any(ord(char) < 32 for char in value)
        ):
            raise StateError(f"settlement {field} must be a nonempty string")
        return value

    def _validate(self, project_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        self._text(project_id, "project_id")
        if not isinstance(payload, dict):
            raise StateError("settlement payload must be an object")
        allowed = {
            "id",
            "dedupe_key",
            "objective_id",
            "task_id",
            "job_id",
            "thread_id",
            "run_id",
            "pull_requests",
            "authorization",
            "policy",
        }
        if set(payload) - allowed:
            raise StateError("unknown settlement binding fields")
        for field in ("id", "dedupe_key"):
            if field in payload:
                self._text(payload[field], field)
        binding = {"project_id": project_id}
        for field in ("objective_id", "task_id", "thread_id", "run_id"):
            binding[field] = self._text(payload.get(field), field)
        binding["job_id"] = payload.get("job_id")
        if binding["job_id"] is not None:
            self._text(binding["job_id"], "job_id")
        project = self.state.get_project(project_id)
        objective = self.state.get_objective(binding["objective_id"])
        if not project or not objective or objective["project_id"] != project_id:
            raise StateError("settlement objective must belong to the exact project")
        if self.state._one(
            "SELECT id FROM projects WHERE coordinator_thread_id=?",
            (binding["thread_id"],),
        ):
            raise StateError("permanent coordinator cannot be a settlement target")
        if binding["job_id"] is not None:
            job = self.state.get_job(binding["job_id"])
            if not job or job["objective_id"] != binding["objective_id"]:
                raise StateError("settlement job must belong to the objective")
        resolved_jobs = set()
        direct_job = self.state.get_job(binding["task_id"])
        if direct_job:
            resolved_jobs.add(direct_job["id"])
        if self.state._one(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='objective_plans'"
        ):
            plan = self.state._one(
                "SELECT job_ids FROM objective_plans WHERE objective_id=?",
                (binding["objective_id"],),
            )
            if plan:
                job_ids = json.loads(plan["job_ids"])
                if binding["task_id"] in job_ids:
                    resolved_jobs.add(job_ids[binding["task_id"]])
        for attempt in self.state.connection.execute(
            "SELECT job_id FROM attempts WHERE task_id=?", (binding["task_id"],)
        ):
            resolved_jobs.add(attempt["job_id"])
        if len(resolved_jobs) != 1:
            raise StateError("settlement task must resolve to exactly one existing job")
        resolved_job = self.state.get_job(resolved_jobs.pop())
        if (
            not resolved_job
            or resolved_job["objective_id"] != binding["objective_id"]
            or (
                binding["job_id"] is not None
                and binding["job_id"] != resolved_job["id"]
            )
        ):
            raise StateError(
                "settlement task identity conflicts with the objective/job"
            )
        binding["job_id"] = resolved_job["id"]
        job_attempts = self.state.connection.execute(
            "SELECT thread_id,run_id FROM attempts WHERE job_id=?",
            (binding["job_id"],),
        ).fetchall()
        if job_attempts and not any(
            (attempt["thread_id"], attempt["run_id"])
            == (binding["thread_id"], binding["run_id"])
            for attempt in job_attempts
        ):
            raise StateError("settlement must identify an actual job execution")
        # A known task/run cannot silently be reassigned to a different objective.
        attempts = self.state.connection.execute(
            "SELECT a.*,j.objective_id FROM attempts a JOIN jobs j ON j.id=a.job_id "
            "WHERE a.task_id=? OR (a.thread_id=? AND a.run_id=?)",
            (binding["task_id"], binding["thread_id"], binding["run_id"]),
        ).fetchall()
        for attempt in attempts:
            if (
                attempt["objective_id"] != binding["objective_id"]
                or attempt["thread_id"] != binding["thread_id"]
                or attempt["run_id"] != binding["run_id"]
                or (
                    binding["job_id"] is not None
                    and attempt["job_id"] != binding["job_id"]
                )
            ):
                raise StateError("settlement execution identity conflicts with a job")
        authorization = payload.get("authorization")
        if (
            not isinstance(authorization, dict)
            or set(authorization)
            != {
                "actor",
                "reason",
                "scope",
                "covers_all_objectives",
            }
            or authorization.get("scope") != "dedicated_task"
            or authorization.get("covers_all_objectives") is not True
        ):
            raise StateError(
                "explicit dedicated-task authorization covering all objectives required"
            )
        binding["authorization"] = {
            "actor": self._text(authorization.get("actor"), "authorization.actor"),
            "reason": self._text(authorization.get("reason"), "authorization.reason"),
            "scope": "dedicated_task",
            "covers_all_objectives": True,
        }
        if payload.get("policy") != "all_merged":
            raise StateError("settlement policy must be all_merged")
        binding["policy"] = "all_merged"
        prs = payload.get("pull_requests")
        if not isinstance(prs, list) or not prs:
            raise StateError("settlement requires a nonempty complete pull request set")
        normalized = []
        identities = set()
        for pr in prs:
            if not isinstance(pr, dict) or set(pr) - {
                "host",
                "repository",
                "number",
                "base_branch",
                "head_sha",
                "merge_sha",
            }:
                raise StateError("invalid settlement pull request fields")
            item = {
                field: self._text(pr.get(field), f"pull_requests.{field}")
                for field in ("host", "repository", "base_branch")
            }
            if type(pr.get("number")) is not int or pr["number"] <= 0:
                raise StateError("pull request number must be a positive integer")
            item["number"] = pr["number"]
            for field in ("head_sha", "merge_sha"):
                if field in pr:
                    sha = self._text(pr[field], f"pull_requests.{field}")
                    if not re.fullmatch(r"[0-9a-fA-F]{7,64}", sha):
                        raise StateError(f"pull request {field} must be a commit hash")
                    item[field] = sha.lower()
            identity = (item["host"], item["repository"], item["number"])
            if identity in identities:
                raise StateError("duplicate pull request identity")
            identities.add(identity)
            normalized.append(item)
        binding["pull_requests"] = sorted(
            normalized, key=lambda pr: (pr["host"], pr["repository"], pr["number"])
        )
        return binding

    @staticmethod
    def _decode(row: dict[str, Any] | None) -> dict[str, Any] | None:
        if row is None:
            return None
        row["binding"] = json.loads(row["binding"])
        row.update(row["binding"])
        row["evidence"] = json.loads(row["evidence"]) if row["evidence"] else None
        row["request_claimed"] = bool(row["request_claimed"])
        return row

    def bind(self, project_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self.state.transaction() as connection:
            binding = self._validate(project_id, payload)
            encoded = canonical_json(binding)
            identifier = payload.get("id") or uuid.uuid4().hex
            dedupe_key = payload.get("dedupe_key")
            matches = connection.execute(
                "SELECT * FROM settlements WHERE id=? OR dedupe_key=? "
                "OR thread_id=? OR (objective_id=? AND task_id=?)",
                (
                    identifier,
                    dedupe_key,
                    binding["thread_id"],
                    binding["objective_id"],
                    binding["task_id"],
                ),
            ).fetchall()
            if matches:
                previous = dict(matches[0])
                if (
                    len(matches) != 1
                    or previous["binding"] != encoded
                    or ("id" in payload and previous["id"] != identifier)
                    or (dedupe_key is not None and previous["dedupe_key"] != dedupe_key)
                ):
                    raise DedupeConflict(
                        "settlement identity reused with different binding"
                    )
                return self._decode(previous)
            now = time.time()
            connection.execute(
                "INSERT INTO settlements(id,dedupe_key,project_id,objective_id,"
                "task_id,job_id,thread_id,run_id,binding,state,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,'pending',?,?)",
                (
                    identifier,
                    dedupe_key,
                    project_id,
                    binding["objective_id"],
                    binding["task_id"],
                    binding["job_id"],
                    binding["thread_id"],
                    binding["run_id"],
                    encoded,
                    now,
                    now,
                ),
            )
            self._event(
                identifier, "pending", {"intent": "settle_after_all_merged"}, None, now
            )
            return self.get(identifier)

    def get(self, identifier: str) -> dict[str, Any] | None:
        return self._decode(
            self.state._one("SELECT * FROM settlements WHERE id=?", (identifier,))
        )

    def list(self, project_id: str = "*") -> list[dict[str, Any]]:
        where = "" if project_id == "*" else " WHERE project_id=?"
        return [
            self._decode(dict(row))
            for row in self.state.connection.execute(
                f"SELECT * FROM settlements{where} ORDER BY created_at,id",
                () if project_id == "*" else (project_id,),
            )
        ]

    def _event(
        self, identifier: str, status: str, evidence: Any, error: str | None, now: float
    ) -> None:
        self.state.connection.execute(
            "INSERT INTO settlement_events(settlement_id,state,evidence,last_error,"
            "created_at) VALUES(?,?,?,?,?)",
            (identifier, status, canonical_json(evidence), error, now),
        )

    def record(
        self,
        identifier: str,
        state: str,
        evidence: Any = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        if state not in STATES:
            raise StateError("invalid settlement state")
        if error is not None and not isinstance(error, str):
            raise StateError("settlement error must be a string")
        encoded = canonical_json(evidence)
        with self.state.transaction() as connection:
            previous = self.get(identifier)
            if previous is None:
                raise StateError("settlement not found")
            if state == "requested" and not previous["request_claimed"]:
                raise StateError("settlement request must be claimed before recording")
            if (
                previous["state"] == state
                and canonical_json(previous["evidence"]) == encoded
                and previous["last_error"] == error
            ):
                return previous
            now = time.time()
            connection.execute(
                "UPDATE settlements SET state=?,evidence=?,last_error=?,updated_at=? "
                "WHERE id=?",
                (state, encoded, error, now, identifier),
            )
            self._event(identifier, state, evidence, error, now)
            return self.get(identifier)

    def claim_request(self, identifier: str, evidence: Any) -> bool:
        """Persist intent before transport; never clear the claim after uncertainty."""
        encoded = canonical_json(evidence)
        with self.state.transaction() as connection:
            if self.get(identifier) is None:
                raise StateError("settlement not found")
            now = time.time()
            changed = connection.execute(
                "UPDATE settlements SET request_claimed=1,state='requested',"
                "evidence=?,last_error=NULL,updated_at=? WHERE id=? "
                "AND request_claimed=0 AND state IN ('pending','blocked','uncertain')",
                (encoded, now, identifier),
            ).rowcount
            if changed:
                self._event(identifier, "requested", evidence, None, now)
            return bool(changed)

    def events(self, identifier: str) -> list[dict[str, Any]]:
        result = []
        for row in self.state.connection.execute(
            "SELECT * FROM settlement_events WHERE settlement_id=? ORDER BY id",
            (identifier,),
        ):
            event = dict(row)
            event["evidence"] = (
                json.loads(event["evidence"]) if event["evidence"] else None
            )
            result.append(event)
        return result
