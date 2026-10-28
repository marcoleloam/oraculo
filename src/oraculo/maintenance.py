"""Official quiescence and maintenance interface, also usable with daemon stopped."""

from __future__ import annotations

import fcntl
import json
import os
import uuid
from contextlib import contextmanager

from oraculo.protocol import content_hash
from oraculo.state import State, StateError


def status(state):
    active = state.get_meta("runtime_maintenance")
    return dict(
        schema_version=1,
        state_schema=4,
        config_schema=1,
        admission_closed=bool(active),
        quiescent=quiescent(state),
        capacity=state.capacity(),
        **(active or {}),
    )


def quiescent(state):
    if state.connection.execute(
        "SELECT 1 FROM sqlite_master WHERE name='memory_cycles'"
    ).fetchone():
        for row in state.connection.execute("SELECT payload FROM memory_cycles"):
            if json.loads(row[0]).get("write_state") in {"pending", "failed"}:
                return False
    return (
        not state.capacity()["occupied"]
        and not state.get_meta("managed_legacy_untracked")
        and not state.connection.execute(
            "SELECT 1 FROM operations WHERE state IN ('dispatched','uncertain') LIMIT 1"
        ).fetchone()
    )


def _carry(state, config, operation_id, carry, prior, actor, phase):
    """Validate an explicit carry against the operation's recorded carry.

    Without a carry, a maintenance opened with one cannot be re-entered or
    ended: every step of that operation repeats the same explicit list.
    """
    from oraculo import reservation_carry

    recorded = ((prior or {}).get("carry") or {}).get("task_ids")
    if carry is None:
        if recorded:
            raise StateError("maintenance_carry_required")
        return None
    carry = reservation_carry.normalize(carry)
    if prior and recorded != carry:
        raise StateError("maintenance_carry_mismatch")
    if config is None:
        raise StateError("carry_ledger_unavailable")
    # The first acquisition requires every listed reservation; later steps
    # accept ones the daemon released meanwhile, never new occupancy.
    terminal = (actor or {}).get("terminal_proof")
    recorded_terminal = ((prior or {}).get("carry") or {}).get("terminal_proof_sha256")
    if prior and recorded_terminal != (content_hash(terminal) if terminal else None):
        raise StateError("maintenance_terminal_proof_mismatch")
    proof = reservation_carry.prove(
        state,
        config,
        carry,
        exact=prior is None,
        terminal_proof=terminal,
        fresh=prior is None,
    )
    state.audit(
        "maintenance.carry",
        dict(
            operation_id=operation_id,
            phase=phase,
            actor=actor or reservation_carry.actor(),
            proof=proof,
        ),
    )
    return proof


def begin(state, operation_id, carry=None, *, config=None, actor=None):
    if not isinstance(operation_id, str) or not operation_id or len(operation_id) > 200:
        raise StateError("maintenance_operation_id_required")
    with state.transaction():
        old = state.get_meta("runtime_maintenance")
        if old and old["operation_id"] != operation_id:
            raise StateError("maintenance_already_active")
        completed = state.get_meta("runtime_maintenance_completed")
        if not old and completed and completed["operation_id"] == operation_id:
            # A filesystem journal may fail after end committed. Reacquire the
            # same operation only after proving quiescence again, retaining its
            # durable token rather than stranding the caller with a new one.
            old = completed
        proof = _carry(state, config, operation_id, carry, old, actor, "begin")
        if proof is None and not quiescent(state):
            raise StateError("maintenance_work_active_or_uncertain")
        record = old or dict(operation_id=operation_id, token=uuid.uuid4().hex)
        if proof is not None:
            recorded = dict(task_ids=proof["task_ids"])
            if proof.get("terminal_proof_sha256"):
                recorded["terminal_proof_sha256"] = proof["terminal_proof_sha256"]
            record = dict(record, carry=recorded)
        state.set_meta("runtime_maintenance", record)
        result = status(state)
        if proof is not None:
            result.update(carry_quiescent=True, carry_proof=proof)
        return result


def end(state, operation_id, token, carry=None, *, config=None, actor=None):
    with state.transaction():
        old = state.get_meta("runtime_maintenance")
        if not old or (old.get("operation_id"), old.get("token")) != (
            operation_id,
            token,
        ):
            raise StateError("maintenance_token_mismatch")
        proof = _carry(state, config, operation_id, carry, old, actor, "end")
        if proof is None and not quiescent(state):
            raise StateError("maintenance_work_active_or_uncertain")
        state.set_meta("runtime_maintenance_completed", old)
        state.set_meta("runtime_maintenance", None)
        result = status(state)
        if proof is not None:
            result.update(carry_quiescent=True, carry_proof=proof)
        return result


def check_legacy_tasks(config):
    from oraculo.task_store import TaskStore

    path = config.state_dir / "managed-tasks.sqlite3"
    if not path.exists():
        return
    store = TaskStore(path, read_only=True)
    try:
        for row in store.db.execute("SELECT id FROM managed_tasks"):
            task = store.get(row[0])
            if (task.get("launch_attempted") or task.get("child")) and task.get(
                "work_status"
            ) not in {"succeeded", "failed", "stopped"}:
                raise StateError("maintenance_managed_work_uncertain")
    finally:
        store.close()


@contextmanager
def maintenance_guard(config, operation_id, carry=None, actor=None):
    from oraculo.service import Singleton

    if not config.db_path.exists():
        raise StateError("maintenance_state_missing")
    with Singleton(config.state_dir):
        fd = os.open(
            config.state_dir / "managed-tasks.lock",
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
            0o600,
        )
        state = None
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            state = State(config.db_path, max_executors=config.max_executors)
            if carry is None:
                check_legacy_tasks(config)
            # With a carry, the proof applies the same ledger rule minus the
            # explicitly listed tasks.
            yield begin(state, operation_id, carry, config=config, actor=actor)
        finally:
            if state:
                state.close()
            os.close(fd)


def end_maintenance(config, operation_id, token, carry=None, actor=None):
    with maintenance_guard(config, operation_id, carry, actor):
        state = State(config.db_path, max_executors=config.max_executors)
        try:
            return end(state, operation_id, token, carry, config=config, actor=actor)
        finally:
            state.close()
