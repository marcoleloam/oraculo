from __future__ import annotations

import json
import sqlite3
import time
import unicodedata
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
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
CREATE TABLE IF NOT EXISTS executor_lease(slot INTEGER PRIMARY KEY CHECK(slot>=1),generation INTEGER NOT NULL,attempt_id TEXT NOT NULL UNIQUE REFERENCES attempts(id),state TEXT NOT NULL);
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
    def __init__(
        self,
        path: str | Path,
        max_executors: int = 1,
        provider_limits: dict[str, int] | None = None,
    ) -> None:
        if type(max_executors) is not int or not 1 <= max_executors <= 4:
            raise StateError("max_executors must be an integer between 1 and 4")
        self.max_executors = max_executors
        self.provider_limits = provider_limits or {}
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.connection = sqlite3.connect(self.path, isolation_level=None, timeout=5)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA busy_timeout=5000")
        self.connection.executescript(SCHEMA)
        # Rebuild the old singleton table atomically without losing a live grant.
        with self.transaction():
            columns = {
                row["name"]
                for row in self.connection.execute("PRAGMA table_info(executor_lease)")
            }
            if "singleton" in columns:
                self.connection.execute(
                    "ALTER TABLE executor_lease RENAME TO executor_lease_v1"
                )
                self.connection.execute(
                    "CREATE TABLE executor_lease(slot INTEGER PRIMARY KEY CHECK(slot>=1),"
                    "generation INTEGER NOT NULL,attempt_id TEXT NOT NULL UNIQUE "
                    "REFERENCES attempts(id),state TEXT NOT NULL)"
                )
                self.connection.execute(
                    "INSERT INTO executor_lease SELECT singleton,generation,attempt_id,state "
                    "FROM executor_lease_v1"
                )
                self.connection.execute("DROP TABLE executor_lease_v1")
            self.connection.execute("INSERT OR IGNORE INTO schema_migrations VALUES(2)")
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
        self._write_paths(payload)
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

    def leases(self) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM executor_lease ORDER BY slot"
            )
        ]

    def lease(self, attempt_id: str | None = None) -> dict[str, Any] | None:
        if attempt_id is None:
            return self._one("SELECT * FROM executor_lease ORDER BY slot LIMIT 1")
        return self._one(
            "SELECT * FROM executor_lease WHERE attempt_id=?", (attempt_id,)
        )

    @staticmethod
    def _write_paths(payload: dict[str, Any]) -> list[str]:
        """Accept only canonical project-relative paths; legacy jobs lock all."""
        spec = payload.get("task_spec")
        if spec is None:
            return ["*"]
        if not isinstance(spec, dict):
            raise StateError("task_spec must be an object")
        paths = spec.get("write_paths", ["*"])
        if not isinstance(paths, list):
            raise StateError("write_paths must be a list")
        for path in paths:
            if not isinstance(path, str) or not path or "\\" in path:
                raise StateError("write_paths must contain canonical relative paths")
            if path == "*":
                continue
            parsed = PurePosixPath(path)
            if (
                parsed.is_absolute()
                or str(parsed) != path
                or path == "."
                or ".." in parsed.parts
                or any(c in path for c in "*?[]\x00")
                or ":" in path
            ):
                raise StateError("write_paths must contain canonical relative paths")
        return paths

    def _scope_conflicts(
        self,
        job: dict[str, Any],
        project_id: str,
        *,
        active: list | None = None,
    ) -> bool:
        if active is None:
            active = self.connection.execute(
                "SELECT j.*,o.project_id FROM executor_lease l "
                "JOIN attempts a ON a.id=l.attempt_id JOIN jobs j ON j.id=a.job_id "
                "JOIN objectives o ON o.id=j.objective_id"
            ).fetchall()
        try:
            paths = self._write_paths(json.loads(job["payload"]))
        except (StateError, ValueError, TypeError, AttributeError):
            # An old or externally written malformed payload cannot narrow a lock.
            paths = ["*"]

        def locations(
            paths: list[str], project: str
        ) -> tuple[set[PurePosixPath], set[tuple[int, int]]]:
            root = Path(self.get_project(project)["path"]).absolute()
            result: set[PurePosixPath] = set()
            identities: set[tuple[int, int]] = set()
            for path in paths:
                location = root if path == "*" else root / path
                # Retain both the directory entry and its physical target. Two
                # registered project IDs may address the same checkout, and a
                # symlink must not bypass writer exclusion.
                for candidate in (location, location.resolve()):
                    # APFS may ignore case and Unicode normalization, including
                    # paths which do not exist yet. Use the same conservative
                    # key on all hosts instead of inferring volume behavior.
                    result.add(
                        PurePosixPath(
                            unicodedata.normalize("NFD", str(candidate)).casefold()
                        )
                    )
                try:
                    info = location.stat()
                except FileNotFoundError:
                    pass
                else:
                    # resolve() does not detect two names for the same inode.
                    identities.add((info.st_dev, info.st_ino))
            return result, identities

        try:
            targets, identities = locations(paths, project_id)
        except (OSError, RuntimeError):
            return bool(active)
        for other in active:
            # Memory can mutate the shared vault/root regardless of task scopes.
            if job["kind"] == "memory" or other["kind"] == "memory":
                return True
            try:
                other_paths = self._write_paths(json.loads(other["payload"]))
            except (StateError, ValueError, TypeError, AttributeError):
                other_paths = ["*"]
            try:
                other_targets, other_identities = locations(
                    other_paths, other["project_id"]
                )
            except (OSError, RuntimeError):
                return True
            if identities & other_identities or any(
                a == b or a in b.parents or b in a.parents
                for a in targets
                for b in other_targets
            ):
                return True
        return False

    def get_attempt(self, attempt_id: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM attempts WHERE id=?", (attempt_id,))

    def capacity(self) -> dict[str, Any]:
        requested = self.get_meta("executor_capacity")
        effective = min(
            self.max_executors,
            requested if requested is not None else self.max_executors,
        )
        occupied = len(self.leases())
        return {
            "configured_ceiling": self.max_executors,
            "requested": requested,
            "effective": effective,
            "occupied": occupied,
            "available": max(0, effective - occupied),
            "draining": occupied > effective,
            "source": "persisted" if requested is not None else "configuration",
        }

    def set_capacity(self, maximum: int) -> dict[str, Any]:
        if type(maximum) is not int or not 1 <= maximum <= self.max_executors:
            raise StateError("capacity_outside_configured_ceiling")
        with self.transaction():
            self.set_meta("executor_capacity", maximum)
            self.audit("capacity.set", {"max_executors": maximum})
            return self.capacity()

    def plan_capacity(self, objective_id: str) -> dict[str, Any]:
        requested = self.get_meta(f"plan_parallel:{objective_id}")
        effective = min(
            self.capacity()["effective"],
            requested if requested is not None else self.max_executors,
        )
        occupied = self.connection.execute(
            "SELECT count(*) FROM executor_lease l JOIN attempts a ON a.id=l.attempt_id "
            "JOIN jobs j ON j.id=a.job_id WHERE j.objective_id=?",
            (objective_id,),
        ).fetchone()[0]
        return {
            "requested": requested,
            "effective": effective,
            "occupied": occupied,
            "available": min(
                self.capacity()["available"], max(0, effective - occupied)
            ),
            "limited": requested is not None and requested > effective,
            "draining": occupied > effective,
        }

    def provider_capacity(self, provider: str) -> dict[str, int]:
        maximum = self.provider_limits.get(provider, self.max_executors)
        occupied = self.connection.execute(
            "SELECT count(*) FROM executor_lease l JOIN attempts a ON a.id=l.attempt_id "
            "WHERE a.provider=?",
            (provider,),
        ).fetchone()[0]
        return {
            "limit": maximum,
            "occupied": occupied,
            "available": max(0, maximum - occupied),
        }

    def admission_blockers(self, job: dict, provider: str | None = None) -> list[dict]:
        """The same durable gates are used by status, dispatch and atomic admission."""
        objective = self.get_objective(job["objective_id"])
        blockers = []

        def block(reason: str, action: str, **details: Any) -> None:
            blockers.append({"reason": reason, "action": action, **details})

        if objective["status"] in {"cancel_requested", "stopped"}:
            block("objective_stopped", "Reconcile cancellation; do not dispatch")
        if job["state"] not in {"queued", "quota_wait", "auth_required"}:
            block(
                "job_not_queued", "Reconcile the original attempt", state=job["state"]
            )
        missing = [
            r[0]
            for r in self.connection.execute(
                "SELECT d.depends_on FROM dependencies d JOIN jobs j ON j.id=d.depends_on "
                "WHERE d.job_id=? AND j.state!='succeeded'",
                (job["id"],),
            )
        ]
        if missing:
            block(
                "dependencies_pending",
                "Reconcile successful dependency criteria",
                job_ids=missing,
            )
        if job["next_attempt_at"] > time.time():
            block(
                "retry_backoff", "Wait for retry time", retry_at=job["next_attempt_at"]
            )
        if self._scope_conflicts(job, objective["project_id"]):
            block(
                "scope_conflict",
                "Wait for conflicting writers and effects to reconcile",
            )
        if not self.capacity()["available"]:
            block(
                "service_capacity",
                "Wait for a slot or set capacity within the configured ceiling",
            )
        plan = self.plan_capacity(job["objective_id"])
        if plan["occupied"] >= plan["effective"]:
            block(
                "plan_capacity",
                "Wait for this plan's active attempts or configure max_parallel",
            )
        if provider and not self.provider_capacity(provider)["available"]:
            block(
                "provider_capacity",
                "Wait for authorized provider capacity",
                provider=provider,
            )
        return blockers

    def acquire_grant(
        self,
        job_id: str,
        provider: str,
        model: str,
        owner_thread_id: str,
        owner_run_id: str,
    ) -> dict[str, Any] | None:
        with self.transaction():
            leases = self.leases()
            job = self.get_job(job_id)
            if not job or self.admission_blockers(job, provider):
                return None
            objective = self.get_objective(job["objective_id"])
            occupied = {lease["slot"] for lease in leases}
            slot = next(
                i
                for i in range(1, self.capacity()["effective"] + 1)
                if i not in occupied
            )
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
                "INSERT INTO executor_lease VALUES(?,?,?,?)",
                (slot, generation, attempt_id, "reserved"),
            )
            self.connection.execute(
                "UPDATE jobs SET state='admitted' WHERE id=?", (job_id,)
            )
            self.connection.execute(
                "UPDATE objectives SET status='running' WHERE id=?", (objective["id"],)
            )
            return self.get_attempt(attempt_id)

    def validate_grant(self, attempt_id: str, generation: int) -> dict[str, Any]:
        lease = self.lease(attempt_id)
        attempt = self.get_attempt(attempt_id)
        if (
            not lease
            or lease["attempt_id"] != attempt_id
            or lease["generation"] != generation
            or not attempt
            or attempt["generation"] != generation
        ):
            raise StaleGrant("grant is stale or unavailable")
        job = self.get_job(attempt["job_id"])
        objective = self.get_objective(job["objective_id"])
        if objective["status"] in {"cancel_requested", "stopped"}:
            raise StaleGrant("objective cancellation invalidated grant")
        return attempt

    @staticmethod
    def _validate_execution_identity(
        thread_id: str,
        run_id: str,
        task_id: str | None = None,
    ) -> None:
        if (
            not isinstance(thread_id, str)
            or not thread_id.strip()
            or not isinstance(run_id, str)
            or not run_id.strip()
            or (
                task_id is not None
                and (not isinstance(task_id, str) or not task_id.strip())
            )
        ):
            raise StateError("execution identities required")

    def _check_execution_binding(
        self, attempt_id: str, thread_id: str, run_id: str, task_id: str | None
    ) -> None:
        if self._one(
            "SELECT a.id FROM attempts a JOIN executor_lease l ON l.attempt_id=a.id "
            "WHERE a.id!=? AND ((a.thread_id=? AND a.run_id=?) "
            "OR (? IS NOT NULL AND a.task_id=?)) LIMIT 1",
            (attempt_id, thread_id, run_id, task_id, task_id),
        ):
            raise StaleGrant("execution already bound to another live grant")

    def claim_grant(
        self,
        attempt_id: str,
        generation: int,
        thread_id: str,
        run_id: str,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        self._validate_execution_identity(thread_id, run_id, task_id)
        with self.transaction():
            attempt = self.validate_grant(attempt_id, generation)
            self._check_execution_binding(attempt_id, thread_id, run_id, task_id)
            if attempt["thread_id"] and (attempt["thread_id"], attempt["run_id"]) != (
                thread_id,
                run_id,
            ):
                raise StaleGrant("grant already claimed by another run")
            if (
                attempt["task_id"] is not None
                and task_id is not None
                and attempt["task_id"] != task_id
            ):
                raise StaleGrant("grant already claimed by another task")
            self.connection.execute(
                "UPDATE attempts SET thread_id=?,run_id=?,task_id=COALESCE(?,task_id),status='running' WHERE id=?",
                (thread_id, run_id, task_id, attempt_id),
            )
            self.connection.execute(
                "UPDATE executor_lease SET state='claimed' WHERE attempt_id=?",
                (attempt_id,),
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
        self._validate_execution_identity(thread_id, run_id, task_id)
        with self.transaction():
            lease = self.lease(attempt_id)
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
            self._check_execution_binding(attempt_id, thread_id, run_id, task_id)
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
            if attempt and self.lease(attempt_id):
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
            lease = self.lease(attempt_id)
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
            lease = self.lease(attempt_id)
            if (
                not attempt
                or not lease
                or lease["attempt_id"] != attempt_id
                or attempt["generation"] != generation
                or lease["generation"] != generation
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
                    (
                        "stopped"
                        if objective["status"] == "cancel_requested"
                        else "failed"
                        if failed
                        else final,
                        objective["id"],
                    ),
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
                "UPDATE jobs SET state=CASE WHEN EXISTS("
                "SELECT 1 FROM attempts a JOIN executor_lease l ON l.attempt_id=a.id "
                "WHERE a.job_id=jobs.id) THEN 'cancel_requested' ELSE 'stopped' END "
                "WHERE objective_id=? AND state NOT IN ('succeeded','failed','stopped')",
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
