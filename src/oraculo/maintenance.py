"""Official quiescence and maintenance interface, also usable with daemon stopped."""

from __future__ import annotations

import fcntl
import json
import os
import uuid
from contextlib import contextmanager

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


def begin(state, operation_id):
    if not isinstance(operation_id, str) or not operation_id or len(operation_id) > 200:
        raise StateError("maintenance_operation_id_required")
    with state.transaction():
        old = state.get_meta("runtime_maintenance")
        if old and old["operation_id"] != operation_id:
            raise StateError("maintenance_already_active")
        if not quiescent(state):
            raise StateError("maintenance_work_active_or_uncertain")
        completed = state.get_meta("runtime_maintenance_completed")
        if not old and completed and completed["operation_id"] == operation_id:
            # A filesystem journal may fail after end committed. Reacquire the
            # same operation only after proving quiescence again, retaining its
            # durable token rather than stranding the caller with a new one.
            old = completed
        state.set_meta(
            "runtime_maintenance",
            old or dict(operation_id=operation_id, token=uuid.uuid4().hex),
        )
        return status(state)


def end(state, operation_id, token):
    with state.transaction():
        old = state.get_meta("runtime_maintenance")
        if not old or old != dict(operation_id=operation_id, token=token):
            raise StateError("maintenance_token_mismatch")
        if not quiescent(state):
            raise StateError("maintenance_work_active_or_uncertain")
        state.set_meta("runtime_maintenance_completed", old)
        state.set_meta("runtime_maintenance", None)
        return status(state)


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
def maintenance_guard(config, operation_id):
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
            check_legacy_tasks(config)
            yield begin(state, operation_id)
        finally:
            if state:
                state.close()
            os.close(fd)


def end_maintenance(config, operation_id, token):
    with maintenance_guard(config, operation_id):
        state = State(config.db_path, max_executors=config.max_executors)
        try:
            return end(state, operation_id, token)
        finally:
            state.close()
