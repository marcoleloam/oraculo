"""One-way reconciliation of an already saved negative legacy task result.

This is not an offline adapter. The legacy service remains the sole writer of
memory/state; this command owns only its existing schema-2 task ledger lock.
No State constructor, DDL, report file, or caller-selected terminal status.
"""

from __future__ import annotations

import copy
import fcntl
import json
import os
import sqlite3
import time
from contextlib import contextmanager

from oraculo.cli import make_envelope, request
from oraculo.managed_tasks import ManagedTasks
from oraculo.memory.cycles import REPORT_FIELDS
from oraculo.protocol import canonical_json, content_hash
from oraculo.state import StateError


@contextmanager
def legacy_database(path, *, version, writable=False):
    if path.is_symlink() or not path.is_file():
        raise StateError("legacy_schema_required")
    db = sqlite3.connect(
        path.absolute().as_uri() + ("?mode=rw" if writable else "?mode=ro"),
        uri=True,
        isolation_level=None,
        timeout=0,
    )
    try:
        if not writable:
            db.execute("PRAGMA query_only=ON")
        if db.execute("SELECT max(version) FROM schema_migrations").fetchone() != (
            version,
        ):
            raise StateError("legacy_schema_required")
        yield db
    finally:
        db.close()


def daemon_running(config):
    fd = os.open(config.state_dir / "daemon.lock", os.O_RDWR | os.O_NOFOLLOW)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pass
        else:
            raise StateError("live_legacy_service_required")
    finally:
        os.close(fd)


async def legacy_service(config):
    # The known legacy parser rejects this exact unsupported kind. A timeout,
    # generic error, missing field, or successful/new protocol is not evidence.
    with legacy_database(config.db_path, version=3):
        pass
    daemon_running(config)
    result = await request(config, make_envelope("managed.capabilities", "*", {}))
    error = result.get("error") or {}
    message = error.get("message", "")
    if not (
        result.get("ok") is False
        and result.get("request_id") == "unknown"
        and result.get("state") == "error"
        and error.get("code") == "ValidationError"
        and error.get("retryable") is False
        and isinstance(message, str)
        and "type=literal_error" in message
        and "input_value='managed.capabilities'" in message
    ):
        raise StateError("live_legacy_service_required")


class Observation:
    """Reuse host gates without persisting their intermediate observations."""

    def __init__(self, task):
        self.task = copy.deepcopy(task)

    def get(self, task_id):
        if task_id != self.task["id"]:
            raise StateError("managed_task_origin_mismatch")
        return copy.deepcopy(self.task)

    def update(self, task_id, phase, proof, **changes):
        self.get(task_id)
        self.task.update(changes)
        return self.get(task_id)


async def confirmed_cycle(config, task, report):
    parent = task["parent"]
    expected_id = "cycle-" + content_hash(
        [
            task["project"]["project_id"],
            f"direct:{parent['thread_id']}:{parent['run_id']}:{task['id']}",
        ]
    )
    if task["memory"]["id"] != expected_id:
        raise StateError("legacy_memory_binding_mismatch")
    envelope = make_envelope(
        "memory.cycle.status",
        task["project"]["project_id"],
        {"cycle_id": task["memory"]["id"]},
    )
    envelope["origin"] = {k: parent[k] for k in ("thread_id", "run_id")}
    response = await request(config, envelope)
    if (
        response.get("ok") is not True
        or response.get("request_id") != envelope["request_id"]
    ):
        raise StateError("legacy_memory_unconfirmed")
    cycle = response.get("data") or {}
    identity = {**envelope["origin"], "entry": "direct"}
    if (
        cycle.get("id") != task["memory"]["id"]
        or cycle.get("project_id") != task["project"]["project_id"]
        or cycle.get("identity") != identity
        or cycle.get("work_status") != "failed"
        or not ManagedTasks._saved(cycle)
    ):
        raise StateError("legacy_memory_binding_mismatch")
    # Legacy status deliberately hides report contents. Reconstruct its exact
    # canonical report from the service's fields and immutable submitted evidence,
    # then compare the digest committed by that service. Never trust a file receipt.
    expected = {
        "schema_version": 1,
        "cycle_id": cycle["id"],
        "project_id": cycle["project_id"],
        "identity": identity,
        "work_status": "failed",
        "started_at": cycle["created_at"],
        "finished_at": cycle["finished_at"],
        "read_state": cycle["read_state"],
        "read_receipt": cycle["read_receipt"],
        "deliveries": cycle["deliveries"],
        "evidence": {
            k: v for k, v in report.items() if k in REPORT_FIELDS - {"verdict"}
        },
        "scope": "selected_structured_report; not a conversation archive",
    }
    if content_hash(expected) != cycle.get("report_sha256"):
        raise StateError("legacy_memory_report_mismatch")
    return cycle


async def reconcile(config, payload, host):
    if (
        not isinstance(payload, dict)
        or set(payload) != {"task_id"}
        or not isinstance(payload["task_id"], str)
        or not payload["task_id"]
    ):
        raise StateError("legacy_task_id_only")
    path = config.state_dir / "managed-tasks.sqlite3"
    fd = os.open(path.with_suffix(".lock"), os.O_RDWR | os.O_NOFOLLOW)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise StateError("managed_adapter_busy_retry_same_key") from None
        await legacy_service(config)
        with legacy_database(path, version=2) as db:
            return await _reconcile(config, payload["task_id"], host, db)
    except (sqlite3.Error, KeyError, TypeError, AttributeError):
        raise StateError("legacy_evidence_invalid") from None
    finally:
        os.close(fd)


async def _reconcile(config, task_id, host, db):
    row = db.execute(
        "SELECT payload FROM managed_tasks WHERE id=?", (task_id,)
    ).fetchone()
    if row is None:
        raise StateError("managed_task_not_found")
    original = row[0]
    task = json.loads(original)
    observation = Observation(task)
    engine = ManagedTasks(config, observation, host, None)
    actor, _ = await engine._identity()
    if (
        actor["thread_id"] != task["origin"]["thread_id"]
        or actor["project_id"] != task["origin"]["project_id"]
        or task["parent"] != task["origin"]
        or task["project"]["project_id"] != task["origin"]["project_id"]
    ):
        raise StateError("legacy_reconciliation_requires_origin")
    if (
        not task.get("child", {}).get("childRunId")
        or task.get("work_status") not in {"pending", "failed"}
        or task.get("publication")
        or task.get("publication_attempted")
        or task.get("merged")
        or task.get("settled")
    ):
        raise StateError("legacy_negative_task_required")
    report = task.get("submitted_report")
    engine._report_kind(task, report)
    if not engine._report_failed(report):
        raise StateError("legacy_negative_report_required")
    if task.get("report") is not None and task["report"] != report:
        raise StateError("terminal_report_reused")
    # Legacy memory omitted verdict and review metadata. Its retained evidence
    # must independently establish the negative result; a dropped verdict alone
    # cannot authorize reconciliation. Keep the full immutable report unchanged.
    retained_report = {
        k: v for k, v in report.items() if k in REPORT_FIELDS - {"verdict"}
    }
    if not engine._report_failed(retained_report):
        raise StateError("legacy_report_format_required")
    await engine._scope(task)
    cycle = await confirmed_cycle(config, task, report)
    await legacy_service(config)
    # No awaited memory or service operation between fresh host checks and commit.
    observed = await engine._observe(task)
    if (
        observed.get("status") not in {"completed", "failed"}
        or observed.get("workState") != "result_available"
        or observed.get("hasPendingChildRuns") is not False
    ):
        raise StateError("terminal_execution_or_descendants_unproven")
    await engine._terminal_gate(observation.get(task_id))
    proof = {
        "work_status": "failed",
        "reason": "work_not_successful",
        "report_sha256": content_hash(report),
        "memory_cycle_id": cycle["id"],
        "memory_report_sha256": cycle["report_sha256"],
        "memory_commit": cycle["write_receipt"]["commit"],
        "child_thread_id": task["child"]["childThreadId"],
        "child_run_id": task["child"]["childRunId"],
    }
    event = {"phase": "legacy_failure_reconciled", "proof": proof}
    signature = content_hash(event)
    event_id = task_id + ":legacy-failure:" + signature
    prior = task.get("legacy_failure_reconciliation")
    if prior is not None and prior != proof:
        raise StateError("legacy_reconciliation_reused")
    if prior is not None and (
        task["work_status"] != "failed" or task.get("report") != report
    ):
        raise StateError("legacy_reconciliation_inconsistent")
    # Open the writer only after all evidence passes. Read-only validation cannot
    # recover a hot journal or change bytes on a rejected attempt.
    daemon_running(config)
    with legacy_database(config.db_path, version=3):
        pass
    with legacy_database(
        config.state_dir / "managed-tasks.sqlite3", version=2, writable=True
    ) as db:
        if db.execute(
            "SELECT payload FROM managed_tasks WHERE id=?", (task_id,)
        ).fetchone() != (original,):
            raise StateError("legacy_task_changed_retry")
        if prior is None:
            task.update(
                work_status="failed",
                phase="blocked",
                blocked_reason="work_not_successful",
                report=report,
                memory=cycle,
                observed=observed,
                legacy_failure_reconciliation=proof,
            )
            task["events"].append(dict(event, id=signature, at=time.time()))
            notification = dict(
                marker="ORACULO_CONTROL_V1",
                task_id=task_id,
                phase="legacy_failure_reconciled",
                proof=proof,
                refresh_required=True,
            )
            db.execute("BEGIN IMMEDIATE")
            try:
                daemon_running(config)
                with legacy_database(config.db_path, version=3):
                    pass
                if db.execute(
                    "SELECT max(version) FROM schema_migrations"
                ).fetchone() != (2,):
                    raise StateError("legacy_schema_required")
                changed = db.execute(
                    "UPDATE managed_tasks SET payload=? WHERE id=? AND payload=?",
                    (canonical_json(task), task_id, original),
                )
                if changed.rowcount != 1:
                    raise StateError("legacy_task_changed_retry")
                db.execute(
                    "INSERT INTO task_returns(id,task_id,payload,receipt) VALUES(?,?,?,NULL)",
                    (event_id, task_id, canonical_json(notification)),
                )
                db.commit()
            except BaseException:
                db.rollback()
                raise
        # A lost ACK leaves this exact durable message pending; replay never appends
        # another event or rewrites the report/cycle. Existing unrelated returns stay intact.
        row = db.execute(
            "SELECT payload,receipt FROM task_returns WHERE id=?", (event_id,)
        ).fetchone()
        if row is None:
            raise StateError("legacy_reconciliation_receipt_missing")
        if row[1] is None:
            try:
                receipt = await host.call(
                    "t3_thread_send",
                    dict(
                        threadId=task["origin"]["thread_id"],
                        message=row[0],
                        mode="auto",
                        clientRequestId=event_id,
                    ),
                )
                if receipt.get("threadId") != task["origin"][
                    "thread_id"
                ] or not receipt.get("messageId"):
                    raise StateError("return_origin_mismatch")
                db.execute(
                    "UPDATE task_returns SET receipt=? WHERE id=? AND receipt IS NULL",
                    (canonical_json(receipt), event_id),
                )
            except Exception:
                pass
        pending = db.execute(
            "SELECT receipt IS NULL FROM task_returns WHERE id=?", (event_id,)
        ).fetchone()[0]
        return dict(
            task_id=task_id,
            phase="blocked",
            blocked_reason="work_not_successful",
            work_status="failed",
            reconciliation="confirmed",
            replayed=prior is not None,
            memory=cycle,
            pending_reconciliation_returns=int(pending),
            publication=None,
        )
