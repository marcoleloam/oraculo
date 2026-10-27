"""Audited carry of finished executors' reservations across offline maintenance.

`maintenance.quiescent` stays the rule for every path. This narrow exception
lets one explicitly opened maintenance operation proceed when the only
occupancy is the reservations of explicitly listed managed tasks whose executor
already submitted its own report and whose ledger registers no live run.

Nothing here releases a reservation or edits a task. The activated daemon
keeps applying its own fresh host gates before any release; the carry proof is
ledger evidence, not a host attestation.
"""

from __future__ import annotations

import getpass
import json
import os
import re
import socket

from oraculo.protocol import content_hash
from oraculo.state import StateError

MAX_CARRY = 16
TASK_ID = re.compile(r"task-[0-9a-f]{64}")
# Phases reached only after the executor's own submission (or a later origin
# observation of it); every other phase may still have a run in flight.
CARRY_PHASES = frozenset({"validation_submitted", "validating", "blocked"})
# Ledger events that register a launch or a running native run.
LIVE_PHASES = frozenset({"launch_pending", "launch_acknowledged", "executing"})
# Blocked reasons that leave a launch, continuation or host effect uncertain.
UNCERTAIN_REASONS = frozenset(
    {
        "children_still_running",
        "child_not_launched",
        "expected_run_not_completed",
        "host_response_uncertain",
        "managed_adapter_busy_retry_same_key",
        "native_retry_parent_binding_changed",
        "task_launch_ack_incomplete",
    }
)
TERMINAL_WORK = frozenset({"succeeded", "failed", "stopped"})


def normalize(task_ids):
    """Explicit, duplicate-free, bounded list of full managed task IDs."""
    if task_ids is None:
        return None
    if (
        not isinstance(task_ids, (list, tuple))
        or not task_ids
        or len(task_ids) > MAX_CARRY
        or any(not isinstance(i, str) or not TASK_ID.fullmatch(i) for i in task_ids)
        or len(set(task_ids)) != len(task_ids)
    ):
        raise StateError("carry_reservations_invalid")
    return sorted(task_ids)


def actor(authorization=None):
    """Who requested the carry; recorded verbatim in every audit entry."""
    if authorization is not None and (
        not isinstance(authorization, str)
        or not authorization.strip()
        or len(authorization) > 500
    ):
        raise StateError("carry_authorization_invalid")
    try:
        user = getpass.getuser()
    except (KeyError, OSError):
        user = None
    return dict(
        user=user,
        uid=os.getuid(),
        pid=os.getpid(),
        host=socket.gethostname(),
        authorization=authorization,
    )


def _ledger(config):
    from oraculo.task_store import TaskStore

    path = config.state_dir / "managed-tasks.sqlite3"
    if not path.is_file():
        raise StateError("carry_ledger_unavailable")
    return TaskStore(path, read_only=True)


def _submission(task, child):
    """Last executor submission event, bound to the submitted report bytes."""
    events = task.get("events") or []
    submitted = [
        e
        for e in events
        if e.get("phase") == "validation_submitted"
        and isinstance(e.get("proof"), dict)
        and (e["proof"].get("submission") or {}).get("thread_id")
        == child["childThreadId"]
    ]
    if not submitted or task.get("submitted_report") is None:
        raise StateError("carry_report_unproven")
    event = submitted[-1]
    if event["proof"].get("report_sha256") != content_hash(task["submitted_report"]):
        raise StateError("carry_report_unproven")
    live = [
        e
        for e in events
        if e.get("phase") in LIVE_PHASES and e.get("sequence", 0) > event["sequence"]
    ]
    if live:
        raise StateError("carry_run_live")
    return event


def _task_proof(store, row):
    task_id = row["id"]
    try:
        task = store.get(task_id)
    except StateError:
        raise StateError("carry_task_unknown") from None
    if row["state"] != "executing":
        # A reservation never bound to a child is a pending launch.
        raise StateError("carry_launch_unbound")
    child = task.get("child") or {}
    if not child.get("childThreadId") or not child.get("childRunId"):
        raise StateError("carry_launch_unbound")
    try:
        receipt = json.loads(row["receipt"] or "null") or {}
    except ValueError:
        receipt = {}
    if (
        receipt.get("childThreadId") != child["childThreadId"]
        or receipt.get("childRunId") != child["childRunId"]
    ):
        raise StateError("carry_child_unproven")
    if task.get("work_status") != "pending":
        raise StateError("carry_task_not_pending")
    reason = task.get("blocked_reason")
    if task.get("phase") not in CARRY_PHASES:
        raise StateError("carry_run_live")
    if task["phase"] == "blocked" and (
        not reason or reason in UNCERTAIN_REASONS or reason.startswith("launch_")
    ):
        raise StateError("carry_host_effect_uncertain")
    observed = task.get("observed") or {}
    if observed.get("hasPendingChildRuns") is True or observed.get(
        "hasPendingQuestion"
    ):
        raise StateError("carry_descendants_pending")
    if task["phase"] == "validating" and observed.get("workState") != (
        "result_available"
    ):
        raise StateError("carry_run_live")
    for key, value in store.db.execute(
        "SELECT key,value FROM metadata WHERE key LIKE ?",
        (task_id + ":continue:%",),
    ):
        if not (json.loads(value) or {}).get("receipt"):
            raise StateError("carry_continuation_uncertain")
    event = _submission(task, child)
    return dict(
        task_id=task_id,
        phase=task["phase"],
        blocked_reason=reason,
        work_status=task["work_status"],
        transport_mode=task.get("transport_mode"),
        child_thread_id=child["childThreadId"],
        child_run_id=child["childRunId"],
        reservation=dict(
            provider=row["provider"],
            slots=row["slots"],
            state=row["state"],
            receipt_sha256=content_hash(row["receipt"]),
        ),
        submission=dict(
            sequence=event["sequence"],
            at=event.get("at"),
            run_id=event["proof"]["submission"].get("run_id"),
            report_sha256=event["proof"]["report_sha256"],
        ),
        last_event=dict(
            sequence=task["events"][-1].get("sequence"),
            phase=task["events"][-1].get("phase"),
            at=task["events"][-1].get("at"),
        ),
        observed={
            k: observed.get(k) for k in ("status", "workState", "hasPendingChildRuns")
        },
        snapshot_revision=task.get("snapshot_revision"),
    )


def prove(state, config, task_ids, *, exact=True):
    """Prove that the listed reservations are the only occupancy.

    ``exact`` requires every listed task to still hold its reservation (first
    acquisition). Re-entry and end accept reservations the daemon released in
    between, but never a new or unlisted one.
    """
    task_ids = normalize(task_ids)
    connection = state.connection
    if connection.execute(
        "SELECT 1 FROM sqlite_master WHERE name='memory_cycles'"
    ).fetchone():
        for (payload,) in connection.execute("SELECT payload FROM memory_cycles"):
            if json.loads(payload).get("write_state") in {"pending", "failed"}:
                raise StateError("carry_memory_cycle_pending")
    if state.get_meta("managed_legacy_untracked"):
        raise StateError("carry_legacy_untracked")
    if connection.execute(
        "SELECT 1 FROM operations WHERE state IN ('dispatched','uncertain') LIMIT 1"
    ).fetchone():
        raise StateError("carry_operation_uncertain")
    if state.leases():
        raise StateError("carry_other_occupancy")
    rows = [
        dict(row)
        for row in connection.execute(
            "SELECT id,provider,slots,state,receipt FROM managed_reservations ORDER BY id"
        )
    ]
    held = {row["id"] for row in rows}
    if held - set(task_ids):
        raise StateError("carry_other_occupancy")
    if exact and set(task_ids) - held:
        raise StateError("carry_reservation_missing")
    store = _ledger(config)
    try:
        tasks = [_task_proof(store, row) for row in rows]
        for (other_id,) in store.db.execute("SELECT id FROM managed_tasks"):
            if other_id in held:
                continue
            other = store.get(other_id)
            # Same rule as maintenance.check_legacy_tasks, minus the carried set.
            if (other.get("launch_attempted") or other.get("child")) and other.get(
                "work_status"
            ) not in TERMINAL_WORK:
                if other_id in task_ids:
                    # Released during this operation by the daemon's own gates.
                    continue
                raise StateError("carry_other_managed_work")
    finally:
        store.close()
    proof = dict(
        schema_version=1,
        task_ids=task_ids,
        tasks=tasks,
        released=sorted(set(task_ids) - held),
        capacity=state.capacity(),
    )
    proof["sha256"] = content_hash(proof)
    return proof
