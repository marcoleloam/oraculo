from __future__ import annotations

import sqlite3
import time
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from oraculo.protocol import canonical_json, content_hash


class StateError(ValueError):
    pass


class StaleGrant(StateError):
    pass


class DedupeConflict(StateError):
    pass


SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY);
INSERT OR IGNORE INTO schema_migrations VALUES(1);
CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY,path TEXT NOT NULL,status TEXT NOT NULL,coordinator_thread_id TEXT);
CREATE TABLE IF NOT EXISTS objectives(id TEXT PRIMARY KEY,project_id TEXT NOT NULL REFERENCES projects(id),text TEXT NOT NULL,origin TEXT NOT NULL,dedupe_key TEXT UNIQUE,evidence TEXT NOT NULL,priority INTEGER NOT NULL,status TEXT NOT NULL DEFAULT 'queued',stop_generation INTEGER NOT NULL DEFAULT 0,created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,objective_id TEXT NOT NULL REFERENCES objectives(id),kind TEXT NOT NULL,payload TEXT NOT NULL,payload_hash TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'queued',next_attempt_at REAL NOT NULL DEFAULT 0,created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS dependencies(job_id TEXT REFERENCES jobs(id),depends_on TEXT REFERENCES jobs(id),PRIMARY KEY(job_id,depends_on));
CREATE TABLE IF NOT EXISTS attempts(id TEXT PRIMARY KEY,job_id TEXT NOT NULL REFERENCES jobs(id),number INTEGER NOT NULL,generation INTEGER NOT NULL,provider TEXT NOT NULL,model TEXT NOT NULL,owner_thread_id TEXT NOT NULL,owner_run_id TEXT NOT NULL,thread_id TEXT,run_id TEXT,task_id TEXT,status TEXT NOT NULL,raw_status TEXT,created_at REAL NOT NULL,UNIQUE(job_id,number));
CREATE TABLE IF NOT EXISTS executor_lease(singleton INTEGER PRIMARY KEY CHECK(singleton=1),generation INTEGER NOT NULL,attempt_id TEXT NOT NULL REFERENCES attempts(id),state TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS operations(id TEXT PRIMARY KEY,attempt_id TEXT REFERENCES attempts(id),kind TEXT NOT NULL,idempotency_key TEXT,expected_state TEXT NOT NULL,observed_state TEXT,state TEXT NOT NULL,payload_hash TEXT NOT NULL,created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS inbox(id TEXT PRIMARY KEY,origin_key TEXT UNIQUE,payload_hash TEXT NOT NULL,payload TEXT NOT NULL,response TEXT);
CREATE TABLE IF NOT EXISTS outbox(id TEXT PRIMARY KEY,objective_id TEXT REFERENCES objectives(id),payload_hash TEXT NOT NULL,payload TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'pending');
CREATE TABLE IF NOT EXISTS thread_cursors(thread_id TEXT PRIMARY KEY,cursor TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS provider_eligibility(provider TEXT PRIMARY KEY,blocked_until REAL NOT NULL DEFAULT 0,reason TEXT);
CREATE TABLE IF NOT EXISTS memory_batches(id TEXT PRIMARY KEY,state TEXT NOT NULL,payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS review_sessions(id TEXT PRIMARY KEY,state TEXT NOT NULL,payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audit_events(id INTEGER PRIMARY KEY,kind TEXT NOT NULL,payload TEXT NOT NULL,created_at REAL NOT NULL);
"""


class State:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.connection = sqlite3.connect(self.path, isolation_level=None, timeout=5)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA busy_timeout=5000")
        self.connection.executescript(SCHEMA)
        self.path.chmod(0o600)

    def close(self) -> None:
        self.connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        if self.connection.in_transaction:
            yield self.connection
            return
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield self.connection
        except BaseException:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def _one(self, sql: str, args: Sequence[Any] = ()) -> dict[str, Any] | None:
        row = self.connection.execute(sql, args).fetchone()
        return dict(row) if row else None

    def get_meta(self, key: str, default: Any = None) -> Any:
        import json

        row = self._one("SELECT value FROM metadata WHERE key=?", (key,))
        return json.loads(row["value"]) if row else default

    def set_meta(self, key: str, value: Any) -> None:
        self.connection.execute(
            "INSERT INTO metadata VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, canonical_json(value)),
        )

    def register_project(
        self, project_id: str, path: str | Path, status: str = "registered"
    ) -> dict[str, Any]:
        self.connection.execute(
            "INSERT INTO projects(id,path,status) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET path=excluded.path,status=excluded.status",
            (project_id, str(path), status),
        )
        return self.get_project(project_id)

    def get_project(self, project_id: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM projects WHERE id=?", (project_id,))

    def list_projects(self) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.connection.execute("SELECT * FROM projects ORDER BY id")
        ]

    def set_coordinator(self, project_id: str, thread_id: str) -> None:
        self.connection.execute(
            "UPDATE projects SET coordinator_thread_id=? WHERE id=?",
            (thread_id, project_id),
        )

    def submit_objective(
        self,
        project_id: str,
        text: str,
        origin: str = "human",
        dedupe_key: str | None = None,
        evidence: Any = None,
        priority: int = 100,
    ) -> dict[str, Any]:
        if origin not in {"human", "autonomous"}:
            raise StateError("invalid objective origin")
        with self.transaction():
            if dedupe_key:
                previous = self._one(
                    "SELECT * FROM objectives WHERE dedupe_key=?", (dedupe_key,)
                )
                if previous:
                    if previous["project_id"] != project_id or previous["text"] != text:
                        raise DedupeConflict(
                            "objective key reused with different payload"
                        )
                    return previous
            identifier = uuid.uuid4().hex
            self.connection.execute(
                "INSERT INTO objectives(id,project_id,text,origin,dedupe_key,evidence,priority,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    identifier,
                    project_id,
                    text,
                    origin,
                    dedupe_key,
                    canonical_json(evidence),
                    priority,
                    time.time(),
                ),
            )
            return self.get_objective(identifier)

    def get_objective(self, objective_id: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM objectives WHERE id=?", (objective_id,))

    def list_objectives(self) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.connection.execute(
                "SELECT * FROM objectives ORDER BY created_at"
            )
        ]

    def enqueue_job(
        self,
        objective_id: str,
        kind: str,
        payload: dict[str, Any],
        dependencies: Sequence[str] = (),
    ) -> dict[str, Any]:
        with self.transaction():
            objective = self.get_objective(objective_id)
            if not objective or objective["status"] in {"cancel_requested", "stopped"}:
                raise StateError("objective cannot accept new jobs")
            for dependency in dependencies:
                job = self.get_job(dependency)
                if not job or job["objective_id"] != objective_id:
                    raise StateError(
                        "dependency must be an existing job of this objective"
                    )
            identifier = uuid.uuid4().hex
            self.connection.execute(
                "INSERT INTO jobs(id,objective_id,kind,payload,payload_hash,created_at) VALUES(?,?,?,?,?,?)",
                (
                    identifier,
                    objective_id,
                    kind,
                    canonical_json(payload),
                    content_hash(payload),
                    time.time(),
                ),
            )
            self.connection.executemany(
                "INSERT INTO dependencies VALUES(?,?)",
                [(identifier, d) for d in dependencies],
            )
            return self.get_job(identifier)

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM jobs WHERE id=?", (job_id,))

    def list_jobs(self, objective_id: str | None = None) -> list[dict[str, Any]]:
        sql = (
            "SELECT * FROM jobs"
            + (" WHERE objective_id=?" if objective_id else "")
            + " ORDER BY created_at"
        )
        return [
            dict(r)
            for r in self.connection.execute(
                sql, (objective_id,) if objective_id else ()
            )
        ]

    def lease(self) -> dict[str, Any] | None:
        return self._one("SELECT * FROM executor_lease WHERE singleton=1")

    def get_attempt(self, attempt_id: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM attempts WHERE id=?", (attempt_id,))

    def acquire_grant(
        self,
        job_id: str,
        provider: str,
        model: str,
        owner_thread_id: str,
        owner_run_id: str,
    ) -> dict[str, Any] | None:
        with self.transaction():
            if self.lease():
                return None
            job = self.get_job(job_id)
            if (
                not job
                or job["state"] not in {"queued", "quota_wait", "auth_required"}
                or job["next_attempt_at"] > time.time()
            ):
                return None
            objective = self.get_objective(job["objective_id"])
            if objective["status"] in {"cancel_requested", "stopped"}:
                return None
            pending = self._one(
                "SELECT d.job_id FROM dependencies d JOIN jobs j ON d.depends_on=j.id WHERE d.job_id=? AND j.state!='succeeded' LIMIT 1",
                (job_id,),
            )
            if pending:
                return None
            generation = self.get_meta("grant_generation", 0) + 1
            self.set_meta("grant_generation", generation)
            attempt_id = uuid.uuid4().hex
            number = self.connection.execute(
                "SELECT count(*)+1 FROM attempts WHERE job_id=?", (job_id,)
            ).fetchone()[0]
            self.connection.execute(
                "INSERT INTO attempts(id,job_id,number,generation,provider,model,owner_thread_id,owner_run_id,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    attempt_id,
                    job_id,
                    number,
                    generation,
                    provider,
                    model,
                    owner_thread_id,
                    owner_run_id,
                    "reserved",
                    time.time(),
                ),
            )
            self.connection.execute(
                "INSERT INTO executor_lease VALUES(1,?,?,?)",
                (generation, attempt_id, "reserved"),
            )
            self.connection.execute(
                "UPDATE jobs SET state='admitted' WHERE id=?", (job_id,)
            )
            self.connection.execute(
                "UPDATE objectives SET status='running' WHERE id=?", (objective["id"],)
            )
            return self.get_attempt(attempt_id)

    def validate_grant(self, attempt_id: str, generation: int) -> dict[str, Any]:
        lease = self.lease()
        attempt = self.get_attempt(attempt_id)
        if (
            not lease
            or lease["attempt_id"] != attempt_id
            or lease["generation"] != generation
            or not attempt
        ):
            raise StaleGrant("grant is stale or unavailable")
        job = self.get_job(attempt["job_id"])
        objective = self.get_objective(job["objective_id"])
        if objective["status"] in {"cancel_requested", "stopped"}:
            raise StaleGrant("objective cancellation invalidated grant")
        return attempt

    def claim_grant(
        self,
        attempt_id: str,
        generation: int,
        thread_id: str,
        run_id: str,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        with self.transaction():
            attempt = self.validate_grant(attempt_id, generation)
            if attempt["thread_id"] and (attempt["thread_id"], attempt["run_id"]) != (
                thread_id,
                run_id,
            ):
                raise StaleGrant("grant already claimed by another run")
            self.connection.execute(
                "UPDATE attempts SET thread_id=?,run_id=?,task_id=COALESCE(?,task_id),status='running' WHERE id=?",
                (thread_id, run_id, task_id, attempt_id),
            )
            self.connection.execute(
                "UPDATE executor_lease SET state='claimed' WHERE singleton=1"
            )
            self.connection.execute(
                "UPDATE jobs SET state='running' WHERE id=?", (attempt["job_id"],)
            )
            return self.get_attempt(attempt_id)

    def bind_cancelled_execution(
        self,
        attempt_id: str,
        generation: int,
        thread_id: str,
        run_id: str,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        if not thread_id or not run_id:
            raise StateError("execution identities required")
        with self.transaction():
            lease = self.lease()
            attempt = self.get_attempt(attempt_id)
            if (
                not lease
                or not attempt
                or lease["attempt_id"] != attempt_id
                or lease["generation"] != generation
                or attempt["generation"] != generation
            ):
                raise StaleGrant(
                    "only the current grant may bind cancellation identity"
                )
            job = self.get_job(attempt["job_id"])
            objective = self.get_objective(job["objective_id"])
            if objective["status"] != "cancel_requested":
                raise StateError("objective has no pending cancellation")
            if (
                (attempt["thread_id"] is not None and attempt["thread_id"] != thread_id)
                or (attempt["run_id"] is not None and attempt["run_id"] != run_id)
                or (
                    attempt["task_id"] is not None
                    and task_id is not None
                    and attempt["task_id"] != task_id
                )
            ):
                raise StaleGrant("cancelled execution identity cannot be rebound")
            self.connection.execute(
                "UPDATE attempts SET thread_id=?,run_id=?,task_id=COALESCE(?,task_id),status='reconciling' WHERE id=?",
                (thread_id, run_id, task_id, attempt_id),
            )
            self.connection.execute(
                "UPDATE executor_lease SET state='reconciling' WHERE attempt_id=?",
                (attempt_id,),
            )
            self.audit(
                "cancelled_execution_bound",
                {
                    "attempt_id": attempt_id,
                    "generation": generation,
                    "thread_id": thread_id,
                    "run_id": run_id,
                    "task_id": task_id,
                },
            )
            return self.get_attempt(attempt_id)

    def mark_uncertain(self, attempt_id: str, reason: str) -> None:
        with self.transaction():
            attempt = self.get_attempt(attempt_id)
            if attempt:
                self.connection.execute(
                    "UPDATE attempts SET status='uncertain',raw_status=? WHERE id=?",
                    (reason, attempt_id),
                )
                self.connection.execute(
                    "UPDATE jobs SET state='uncertain' WHERE id=?", (attempt["job_id"],)
                )
                self.connection.execute(
                    "UPDATE executor_lease SET state='uncertain' WHERE attempt_id=?",
                    (attempt_id,),
                )

    def fail_unstarted(
        self, attempt_id: str, generation: int, evidence: dict[str, Any]
    ) -> dict[str, Any]:
        import re

        if (
            not isinstance(evidence, dict)
            or evidence.get("definitive_no_child") is not True
            or evidence.get("error_kind") not in {"quota", "auth", "validation"}
            or not isinstance(evidence.get("client_request_id"), str)
            or not evidence["client_request_id"].strip()
            or not isinstance(evidence.get("tool_error"), (str, dict))
            or not evidence["tool_error"]
        ):
            raise StateError("definitive no-child tool rejection required")
        tool_error = canonical_json(evidence["tool_error"])
        if re.search(
            r"timeout|timed?\s*out|connection\s*(?:lost|reset|closed)|"
            r"(?:lost|missing)\s*response|response\s*(?:lost|missing)|"
            r"transport\s*(?:error|failure)|network\s*(?:error|failure)",
            tool_error,
            re.IGNORECASE,
        ):
            raise StateError("transport ambiguity cannot prove no child")
        with self.transaction():
            lease = self.lease()
            attempt = self.get_attempt(attempt_id)
            if (
                not lease
                or not attempt
                or lease["attempt_id"] != attempt_id
                or lease["generation"] != generation
                or attempt["generation"] != generation
                or lease["state"] not in {"reserved", "uncertain"}
                or attempt["thread_id"] is not None
                or attempt["run_id"] is not None
            ):
                raise StaleGrant("only the current unclaimed grant can fail unstarted")
            if self._one(
                "SELECT id FROM operations WHERE attempt_id=? "
                "AND state IN ('prepared','dispatched','uncertain')",
                (attempt_id,),
            ):
                raise StateError("unresolved operation must be reconciled first")
            job = self.get_job(attempt["job_id"])
            objective = self.get_objective(job["objective_id"])
            self.audit(
                "execution_definitively_not_started",
                {
                    "attempt_id": attempt_id,
                    "generation": generation,
                    "evidence": evidence,
                },
            )
            final = "stopped" if objective["status"] == "cancel_requested" else "failed"
            self.connection.execute(
                "UPDATE attempts SET status=?,raw_status='not_started' WHERE id=?",
                (final, attempt_id),
            )
            self.connection.execute(
                "UPDATE jobs SET state=? WHERE id=?", (final, attempt["job_id"])
            )
            self.connection.execute(
                "DELETE FROM executor_lease WHERE attempt_id=? AND generation=?",
                (attempt_id, generation),
            )
            remaining = self._one(
                "SELECT id FROM jobs WHERE objective_id=? "
                "AND state NOT IN ('succeeded','failed','stopped')",
                (objective["id"],),
            )
            if not remaining:
                status = (
                    "stopped" if objective["status"] == "cancel_requested" else "failed"
                )
                self.connection.execute(
                    "UPDATE objectives SET status=? WHERE id=?",
                    (status, objective["id"]),
                )
            return self.get_attempt(attempt_id)

    def record_execution(
        self,
        attempt_id: str,
        generation: int,
        run_id: str,
        status: str,
        *,
        pending_children: bool = True,
        tree_confirmed: bool = False,
        evidence: Any = None,
    ) -> bool:
        with self.transaction():
            attempt = self.get_attempt(attempt_id)
            lease = self.lease()
            if (
                not attempt
                or not lease
                or lease["attempt_id"] != attempt_id
                or attempt["generation"] != generation
                or attempt["run_id"] != run_id
            ):
                return False
            self.audit(
                "execution_observed",
                {"attempt_id": attempt_id, "status": status, "evidence": evidence},
            )
            unresolved = self._one(
                "SELECT id FROM operations WHERE attempt_id=? AND state IN ('prepared','dispatched','uncertain')",
                (attempt_id,),
            )
            if (
                pending_children
                or not tree_confirmed
                or unresolved
                or status not in {"succeeded", "failed", "stopped"}
            ):
                return False
            job = self.get_job(attempt["job_id"])
            objective = self.get_objective(job["objective_id"])
            failed_operation = self._one(
                "SELECT id FROM operations WHERE attempt_id=? AND state='failed'",
                (attempt_id,),
            )
            final = (
                "stopped"
                if objective["status"] == "cancel_requested"
                else "failed"
                if failed_operation
                else status
            )
            self.connection.execute(
                "UPDATE attempts SET status=?,raw_status=? WHERE id=?",
                (final, status, attempt_id),
            )
            self.connection.execute(
                "UPDATE jobs SET state=? WHERE id=?", (final, attempt["job_id"])
            )
            self.connection.execute(
                "DELETE FROM executor_lease WHERE attempt_id=?", (attempt_id,)
            )
            remaining = self._one(
                "SELECT id FROM jobs WHERE objective_id=? AND state NOT IN ('succeeded','failed','stopped')",
                (objective["id"],),
            )
            if not remaining:
                failed = self._one(
                    "SELECT id FROM jobs WHERE objective_id=? AND state='failed'",
                    (objective["id"],),
                )
                self.connection.execute(
                    "UPDATE objectives SET status=? WHERE id=?",
                    ("failed" if failed else final, objective["id"]),
                )
            return True

    def cancel_objective(self, objective_id: str) -> dict[str, Any]:
        with self.transaction():
            objective = self.get_objective(objective_id)
            if not objective:
                raise StateError("objective not found")
            if objective["status"] in {
                "succeeded",
                "failed",
                "stopped",
                "cancel_requested",
            }:
                return objective
            self.connection.execute(
                "UPDATE objectives SET status='cancel_requested',stop_generation=stop_generation+1 WHERE id=?",
                (objective_id,),
            )
            self.connection.execute(
                "UPDATE jobs SET state=CASE WHEN state IN ('queued','quota_wait','auth_required') THEN 'stopped' ELSE 'cancel_requested' END WHERE objective_id=? AND state NOT IN ('succeeded','failed','stopped')",
                (objective_id,),
            )
            self.connection.execute(
                "UPDATE outbox SET state='suspended' WHERE objective_id=? AND state='pending'",
                (objective_id,),
            )
            if not self._one(
                "SELECT a.id FROM attempts a JOIN jobs j ON j.id=a.job_id JOIN executor_lease l ON l.attempt_id=a.id WHERE j.objective_id=?",
                (objective_id,),
            ):
                self.connection.execute(
                    "UPDATE objectives SET status='stopped' WHERE id=?", (objective_id,)
                )
            return self.get_objective(objective_id)

    def audit(self, kind: str, payload: Any) -> None:
        self.connection.execute(
            "INSERT INTO audit_events(kind,payload,created_at) VALUES(?,?,?)",
            (kind, canonical_json(payload), time.time()),
        )

    def request_begin(
        self, request_id: str, payload: Any, origin_key: str | None = None
    ) -> dict[str, Any] | None:
        import json

        digest = content_hash(payload)
        row = self._one(
            "SELECT * FROM inbox WHERE id=? OR (origin_key IS NOT NULL AND origin_key=?)",
            (request_id, origin_key),
        )
        if row:
            if row["payload_hash"] != digest:
                raise DedupeConflict("request identity reused with different payload")
            return json.loads(row["response"]) if row["response"] else {"pending": True}
        self.connection.execute(
            "INSERT INTO inbox(id,origin_key,payload_hash,payload) VALUES(?,?,?,?)",
            (request_id, origin_key, digest, canonical_json(payload)),
        )
        return None

    def request_finish(self, request_id: str, response: Any) -> None:
        self.connection.execute(
            "UPDATE inbox SET response=? WHERE id=?",
            (canonical_json(response), request_id),
        )

    def ingest_items(
        self, thread_id: str, items: Sequence[dict[str, Any]], cursor: Any
    ) -> None:
        with self.transaction():
            for item in items:
                if item.get("textTruncated"):
                    raise StateError("incomplete message cannot advance cursor")
                identifier = str(item.get("itemId", item.get("id")))
                if identifier == "None":
                    raise StateError("message identity missing")
                self.request_begin(
                    f"t3:{thread_id}:{identifier}", item, f"{thread_id}:{identifier}"
                )
            self.connection.execute(
                "INSERT INTO thread_cursors VALUES(?,?) ON CONFLICT(thread_id) DO UPDATE SET cursor=excluded.cursor",
                (thread_id, canonical_json(cursor)),
            )

    def enqueue_outbox(
        self, identifier: str, payload: Any, objective_id: str | None = None
    ) -> None:
        digest = content_hash(payload)
        row = self._one("SELECT * FROM outbox WHERE id=?", (identifier,))
        if row and row["payload_hash"] != digest:
            raise DedupeConflict("outbox identity conflict")
        self.connection.execute(
            "INSERT OR IGNORE INTO outbox(id,objective_id,payload_hash,payload) VALUES(?,?,?,?)",
            (identifier, objective_id, digest, canonical_json(payload)),
        )

    def backup(self, destination: Path) -> None:
        with sqlite3.connect(destination) as target:
            self.connection.backup(target)
