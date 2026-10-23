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
                if version and version > 5:
                    raise StateError("state_schema_newer_than_runtime")
                self.db.execute("SELECT id,payload FROM managed_tasks LIMIT 0")
                self.db.execute("SELECT id,payload,receipt FROM task_returns LIMIT 0")
            except (sqlite3.Error, StateError) as exc:
                if hasattr(self, "db"):
                    self.db.close()
                raise StateError("managed_ledger_read_unavailable") from exc
            return
        # Schema 5 belongs only to the managed ledger. Old writers must refuse
        # it: NULL receipts on covered events are deliberately not host ACKs.
        self.state = State(path, schema_max=5)
        self.db = self.state.connection
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS managed_tasks(
            id TEXT PRIMARY KEY, origin TEXT NOT NULL, dedupe TEXT NOT NULL,
            request_hash TEXT NOT NULL, payload TEXT NOT NULL,
            UNIQUE(origin,dedupe));
          CREATE TABLE IF NOT EXISTS task_returns(
            id TEXT PRIMARY KEY, task_id TEXT NOT NULL, payload TEXT NOT NULL,
            receipt TEXT);
          CREATE TABLE IF NOT EXISTS task_notifications(
            return_id TEXT PRIMARY KEY REFERENCES task_returns(id),
            task_id TEXT NOT NULL, origin TEXT NOT NULL,
            signature TEXT NOT NULL, representative_id TEXT NOT NULL,
            disposition TEXT NOT NULL CHECK(disposition IN
              ('intent','coalesced','suppressed')), decided_at REAL NOT NULL);
          CREATE INDEX IF NOT EXISTS task_notification_scope
            ON task_notifications(task_id,origin);
        """)
        columns = {r[1] for r in self.db.execute("PRAGMA table_info(task_returns)")}
        if "delivery" not in columns:
            self.db.execute("ALTER TABLE task_returns ADD COLUMN delivery TEXT")
        self.db.execute("INSERT OR IGNORE INTO schema_migrations VALUES(5)")

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
        if request.get("return_format", "readable") not in {"readable", "json"}:
            raise StateError("invalid_return_format")
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
                return_format=request.get("return_format", "readable"),
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
        # Old read-only ledgers have no notification migration yet.
        migrated = self._notifications_available()
        join = "LEFT JOIN task_notifications n ON n.return_id=r.id" if migrated else ""
        visible = (
            "AND (n.return_id IS NULL OR n.disposition='intent')" if migrated else ""
        )
        return [
            dict(id=r[0], payload=json.loads(r[1]))
            for r in self.db.execute(
                f"SELECT r.id,r.payload FROM task_returns r {join} "
                f"WHERE r.task_id=? AND r.receipt IS NULL {visible} ORDER BY r.rowid",
                (task_id,),
            )
        ]

    def _notifications_available(self):
        return bool(
            self.db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='task_notifications'"
            ).fetchone()
        )

    def prepare_returns(self, task_id: str) -> list[dict]:
        """Atomically select readable intentions before any external effect.

        Frozen deliveries are barriers: always replay their original bytes/key.
        Only unprepared readable events can be covered by another intention.
        No receipt is written here; coverage is local provenance, not a host ACK.
        The caller sends this bounded batch, so sends cannot feed their own loop.
        """
        from oraculo.presentation import notification_signature, task_view

        with self.state.transaction():
            task = self.get(task_id)
            origin = canonical_json(task["origin"])
            last = self.db.execute(
                "SELECT n.signature,n.return_id FROM task_notifications n "
                "JOIN task_returns r ON r.id=n.return_id "
                "WHERE n.task_id=? AND n.origin=? AND n.disposition='intent' "
                "AND r.receipt IS NOT NULL ORDER BY r.rowid DESC LIMIT 1",
                (task_id, origin),
            ).fetchone()
            target = tuple(last) if last else None
            confirmed = target is not None
            current_signature = notification_signature(task_view(task), task)
            pending = self.pending_returns(task_id)
            fresh = []

            def prepare_fresh():
                nonlocal target, confirmed
                if not fresh:
                    return
                if not target or target[0] != current_signature:
                    # Prefer the newest event: the one summary of a completed
                    # batch should not masquerade as an obsolete transition.
                    representative = fresh[-1]
                    self.delivery(representative)
                    self._notification(
                        representative,
                        origin,
                        current_signature,
                        representative["id"],
                        "intent",
                    )
                    target, confirmed = (current_signature, representative["id"]), False
                for item in fresh:
                    if item["id"] != target[1]:
                        self._notification(
                            item,
                            origin,
                            current_signature,
                            target[1],
                            "suppressed" if confirmed else "coalesced",
                        )
                fresh.clear()

            for event in pending:
                row = self.db.execute(
                    "SELECT delivery FROM task_returns WHERE id=?", (event["id"],)
                ).fetchone()
                frozen = json.loads(row[0]) if row[0] else None
                if frozen is not None:
                    prepare_fresh()
                    # Even a pre-upgrade frozen readable duplicate must replay.
                    if frozen.get("return_format") == "readable":
                        signature = notification_signature(
                            frozen["presentation"], frozen["snapshot"]
                        )
                        target, confirmed = (signature, event["id"]), False
                        self._notification(
                            event, origin, signature, event["id"], "intent"
                        )
                    else:
                        target = None
                    continue
                if task.get("return_format", "json") != "readable":
                    self.delivery(event)
                    target = None
                    continue
                fresh.append(event)
            prepare_fresh()
            return self.pending_returns(task_id)

    def _notification(self, event, origin, signature, representative, disposition):
        self.db.execute(
            "INSERT OR IGNORE INTO task_notifications VALUES(?,?,?,?,?,?,?)",
            (
                event["id"],
                event["payload"]["task_id"],
                origin,
                signature,
                representative,
                disposition,
                time.time(),
            ),
        )

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
        from oraculo.presentation import task_view

        payload["presentation"] = task_view(task)
        # Old tasks and already cached deliveries keep their original wire format.
        payload["return_format"] = task.get("return_format", "json")
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
            disposition = self.db.execute(
                "SELECT disposition FROM task_notifications WHERE return_id=?",
                (event_id,),
            ).fetchone()
            if disposition and disposition[0] != "intent":
                raise StateError("return_was_not_sent")
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
            returns=self.return_history(task_id, events),
        )

    def return_history(self, task_id: str, events: list[dict]) -> list[dict]:
        """CLI audit page: local coverage never impersonates host confirmation."""
        migrated = self._notifications_available()
        result = []
        for event in events:
            ident = task_id + ":" + event["id"]
            row = self.db.execute(
                "SELECT receipt FROM task_returns WHERE id=?", (ident,)
            ).fetchone()
            notification = (
                self.db.execute(
                    "SELECT * FROM task_notifications WHERE return_id=?", (ident,)
                ).fetchone()
                if migrated
                else None
            )
            record = (
                dict(notification)
                if notification
                else {
                    "return_id": ident,
                    "disposition": "unprepared",
                }
            )
            receipt = json.loads(row[0]) if row and row[0] else None
            record["receipt"] = receipt
            record["delivery_status"] = "host_confirmed" if receipt else "pending"
            if notification and notification["disposition"] != "intent":
                representative = self.db.execute(
                    "SELECT receipt FROM task_returns WHERE id=?",
                    (notification["representative_id"],),
                ).fetchone()
                record["delivery_status"] = (
                    "covered_by_confirmed"
                    if representative and representative[0]
                    else "covered_by_pending"
                )
            result.append(record)
        return result

    def describe(self, task_id: str) -> dict:
        from oraculo.presentation import task_view

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
            "presentation": task_view(task),
            "title": (task.get("request") or {}).get("title"),
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
