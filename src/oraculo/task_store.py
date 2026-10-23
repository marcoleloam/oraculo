"""Private, durable task intents and return outbox; no model/host side effects."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path

from oraculo.protocol import canonical_json, content_hash
from oraculo.state import DedupeConflict, State, StateError


class TaskStore:
    def __init__(self, path: Path, *, read_only=False):
        if read_only:
            # Offline inspection must not run State's schema migrations or
            # contend with the daemon's writer authority. URI mode also forbids
            # creating a missing ledger; a read transaction gives one snapshot.
            self.state = None
            try:
                self.db = sqlite3.connect(
                    path.absolute().as_uri() + "?mode=ro", uri=True
                )
                self.db.row_factory = sqlite3.Row
                self.db.execute("PRAGMA query_only=ON")
                self.db.execute("BEGIN")
                version = self.db.execute(
                    "SELECT max(version) FROM schema_migrations"
                ).fetchone()[0]
                if version and version > 4:
                    raise StateError("state_schema_newer_than_runtime")
                self.db.execute("SELECT id,payload FROM managed_tasks LIMIT 0")
                self.db.execute("SELECT id,payload,receipt FROM task_returns LIMIT 0")
            except (sqlite3.Error, StateError) as exc:
                if hasattr(self, "db"):
                    self.db.close()
                raise StateError("managed_ledger_read_unavailable") from exc
            return
        self.state = State(path)
        self.db = self.state.connection
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS managed_tasks(
            id TEXT PRIMARY KEY, origin TEXT NOT NULL, dedupe TEXT NOT NULL,
            request_hash TEXT NOT NULL, payload TEXT NOT NULL,
            UNIQUE(origin,dedupe));
          CREATE TABLE IF NOT EXISTS task_returns(
            id TEXT PRIMARY KEY, task_id TEXT NOT NULL, payload TEXT NOT NULL,
            receipt TEXT);
        """)
        columns = {r[1] for r in self.db.execute("PRAGMA table_info(task_returns)")}
        if "delivery" not in columns:
            self.db.execute("ALTER TABLE task_returns ADD COLUMN delivery TEXT")

    def close(self):
        if self.state is None:
            self.db.close()
        else:
            self.state.close()

    def get(self, task_id: str) -> dict:
        row = self.db.execute(
            "SELECT payload FROM managed_tasks WHERE id=?", (task_id,)
        ).fetchone()
        if row is None:
            raise StateError("managed_task_not_found")
        return json.loads(row[0])

    def create(self, origin: dict, key: str, request: dict) -> dict:
        if not key or len(key) > 200:
            raise StateError("task_dedupe_key_required")
        task_id = "task-" + content_hash([origin["thread_id"], key])
        digest = content_hash(request)
        with self.state.transaction():
            row = self.db.execute(
                "SELECT request_hash FROM managed_tasks WHERE id=?", (task_id,)
            ).fetchone()
            if row:
                if row[0] != digest:
                    raise DedupeConflict("task_key_reused")
                return self.get(task_id)
            task = dict(
                id=task_id,
                origin=origin,
                parent=origin.copy(),
                request=request,
                phase="received",
                events=[],
                work_status="pending",
                memory=None,
                publication=None,
                merged=False,
                settled=False,
            )
            self.db.execute(
                "INSERT INTO managed_tasks VALUES(?,?,?,?,?)",
                (task_id, origin["thread_id"], key, digest, canonical_json(task)),
            )
            return self.update(task_id, "received", {"request_sha256": digest})

    def update(self, task_id: str, phase: str, proof: dict, **changes) -> dict:
        if not proof:
            raise StateError("task_transition_requires_proof")
        with self.state.transaction():
            task = self.get(task_id)
            before = canonical_json(task)
            task.update(changes)
            task["phase"] = phase
            if phase != "blocked":
                task["blocked_reason"] = None
            event = dict(phase=phase, proof=proof)
            signature = content_hash(event)
            if before != canonical_json(task) or not any(
                e.get("signature", e["id"]) == signature for e in task["events"]
            ):
                revision = task.get("snapshot_revision", 0) + 1
                task["snapshot_revision"] = revision
                task["observed_at"] = time.time()
                event_id = content_hash([task_id, revision, signature])
                task["events"].append(
                    dict(
                        event,
                        id=event_id,
                        signature=signature,
                        sequence=revision,
                        at=time.time(),
                    )
                )
                # Only bounded structural status is sent to the origin, never reports/context.
                notification = dict(
                    marker="ORACULO_CONTROL_V1",
                    task_id=task_id,
                    schema_version=2,
                    event_id=event_id,
                    sequence=revision,
                    occurred_at=task["events"][-1]["at"],
                    phase=phase,
                    proof=proof,
                )
                self.db.execute(
                    "INSERT OR IGNORE INTO task_returns(id,task_id,payload,receipt) VALUES(?,?,?,NULL)",
                    (task_id + ":" + event_id, task_id, canonical_json(notification)),
                )
            self.db.execute(
                "UPDATE managed_tasks SET payload=? WHERE id=?",
                (canonical_json(task), task_id),
            )
            return task

    def pending_returns(self, task_id: str) -> list[dict]:
        return [
            dict(id=r[0], payload=json.loads(r[1]))
            for r in self.db.execute(
                "SELECT id,payload FROM task_returns WHERE task_id=? AND receipt IS NULL ORDER BY rowid",
                (task_id,),
            )
        ]

    def recover_report(self, expected: dict, replacement: dict, audit: dict):
        """CAS, original evidence, replacement, event and outbox in one transaction."""
        with self.state.transaction():
            if self.get(expected["id"]) != expected:
                raise StateError("report_recovery_task_changed_retry")
            if expected.get("report_recovery") is not None:
                raise StateError("report_recovery_reused")
            proof = {
                key: audit[key]
                for key in ("original_sha256", "replacement_sha256", "invalid_reason")
            }
            return self.update(
                expected["id"],
                "validation_recovered",
                proof,
                submitted_report=replacement,
                report_recovery=audit,
            )

    def delivery(self, event: dict) -> dict:
        cached = self.db.execute(
            "SELECT delivery FROM task_returns WHERE id=?", (event["id"],)
        ).fetchone()
        if cached and cached[0]:
            return json.loads(cached[0])
        task = self.get(event["payload"]["task_id"])
        payload = dict(event["payload"])
        revision = task.get("snapshot_revision", 0)
        payload.update(
            delivered_at=time.time(),
            snapshot_revision=revision,
            historical=payload.get("sequence", -1) < revision,
            # A frozen retry can arrive after later revisions. The snapshot is
            # an observation at emission, never proof of freshness at receipt.
            refresh_required=True,
            replayed=False,
            replay_semantics="immutable_delivery_may_repeat",
            snapshot={
                k: task.get(k)
                for k in (
                    "id",
                    "phase",
                    "work_status",
                    "blocked_reason",
                    "snapshot_revision",
                    "observed_at",
                    "deliverable_kind",
                    "approval_state",
                )
            },
        )
        self.db.execute(
            "UPDATE task_returns SET delivery=? WHERE id=?",
            (canonical_json(payload), event["id"]),
        )
        return payload

    def acknowledge_return(self, event_id: str, receipt: dict):
        if not receipt.get("messageId") or not receipt.get("threadId"):
            raise StateError("return_receipt_missing")
        with self.state.transaction():
            row = self.db.execute(
                "SELECT task_id FROM task_returns WHERE id=?", (event_id,)
            ).fetchone()
            if (
                row is None
                or receipt["threadId"] != self.get(row[0])["origin"]["thread_id"]
            ):
                raise StateError("return_origin_mismatch")
            self.db.execute(
                "UPDATE task_returns SET receipt=? WHERE id=?",
                (canonical_json(receipt), event_id),
            )

    def context_artifact(self, context: dict) -> dict:
        raw = canonical_json(context).encode()
        digest = hashlib.sha256(raw).hexdigest()
        root = self.state.path.parent / "managed-contexts"
        root.mkdir(mode=0o700, exist_ok=True)
        if root.is_symlink():
            raise StateError("managed_context_directory_invalid")
        path = root / (digest + ".json")
        try:
            fd = os.open(
                path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600
            )
        except FileExistsError:
            if path.is_symlink() or path.read_bytes() != raw:
                raise StateError("managed_context_integrity_error")
        else:
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
        return {
            "path": str(path),
            "sha256": digest,
            "bytes": len(raw),
            "scope": "selected_project_and_sanitized_shared",
        }

    def history(self, task_id: str, offset: int = 0) -> dict:
        if type(offset) is not int or offset < 0:
            raise StateError("invalid_history_offset")
        task = self.get(task_id)
        events = task["events"][offset : offset + 50]
        return dict(
            task_id=task_id,
            events=events,
            offset=offset,
            next_offset=offset + len(events),
            total=len(task["events"]),
            snapshot_revision=task.get("snapshot_revision", 0),
        )

    def describe(self, task_id: str) -> dict:
        task = self.get(task_id)
        return {
            k: v
            for k, v in task.items()
            if k
            not in {
                "request",
                "context",
                "report",
                "submitted_report",
                "report_recovery",
                "report_bootstrap",
                "continuation_bootstrap",
                "launch_arguments",
                "events",
            }
        } | {
            "continuation_bootstrap": (
                {
                    k: task["continuation_bootstrap"][k]
                    for k in ("version", "original_sha256", "work_status")
                }
                if task.get("continuation_bootstrap")
                else None
            ),
            "report_bootstrap": (
                {
                    k: task["report_bootstrap"][k]
                    for k in ("version", "original_sha256", "replacement_sha256")
                }
                if task.get("report_bootstrap")
                else None
            ),
            "report_recovery": (
                {
                    k: task["report_recovery"][k]
                    for k in ("original_sha256", "replacement_sha256", "invalid_reason")
                }
                if task.get("report_recovery")
                else None
            ),
            "pending_returns": len(self.pending_returns(task_id)),
            "events": task["events"][-100:],
            "event_count": len(task["events"]),
            "history_truncated": len(task["events"]) > 100,
        }
