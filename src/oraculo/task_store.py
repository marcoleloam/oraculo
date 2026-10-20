"""Private, durable task intents and return outbox; no model/host side effects."""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

from oraculo.protocol import canonical_json, content_hash
from oraculo.state import DedupeConflict, State, StateError


class TaskStore:
    def __init__(self, path: Path):
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

    def close(self):
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
            task.update(changes)
            task["phase"] = phase
            if phase != "blocked":
                task["blocked_reason"] = None
            event = dict(phase=phase, proof=proof)
            signature = content_hash(event)
            if not any(e["id"] == signature for e in task["events"]):
                task["events"].append(dict(event, id=signature, at=time.time()))
                # Only bounded structural status is sent to the origin, never reports/context.
                notification = dict(
                    marker="ORACULO_CONTROL_V1",
                    task_id=task_id,
                    phase=phase,
                    proof=proof,
                )
                self.db.execute(
                    "INSERT OR IGNORE INTO task_returns VALUES(?,?,?,NULL)",
                    (task_id + ":" + signature, task_id, canonical_json(notification)),
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
                "launch_arguments",
            }
        } | {"pending_returns": len(self.pending_returns(task_id))}
