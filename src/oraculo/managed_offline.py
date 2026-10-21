"""Offline cancellation of a managed reservation while no daemon runs.

The running daemon is the only writer of state; this path exists for the one
case the live service cannot resolve itself: a reservation whose launch never
bound a child blocks quiescence, and the daemon that holds it predates
`task cancel`. The daemon lock and the ledger lock are both taken exclusively;
a live service makes this path refuse rather than contend.
"""

from __future__ import annotations

import fcntl
import os

from oraculo.managed_tasks import ManagedTasks
from oraculo.state import State, StateError
from oraculo.task_store import TaskStore


async def cancel(config, payload: dict, host) -> dict:
    from oraculo.service import Singleton

    if (
        not isinstance(payload, dict)
        or set(payload) != {"task_id"}
        or not isinstance(payload["task_id"], str)
        or not payload["task_id"]
    ):
        raise StateError("task_id_only")
    if not config.db_path.exists():
        raise StateError("maintenance_state_missing")
    ledger = config.state_dir / "managed-tasks.sqlite3"
    if not ledger.is_file():
        raise StateError("managed_ledger_read_unavailable")
    try:
        singleton = Singleton(config.state_dir).__enter__()
    except BlockingIOError:
        raise StateError("live_service_use_socket") from None
    try:
        fd = os.open(ledger.with_suffix(".lock"), os.O_RDWR | os.O_NOFOLLOW)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise StateError("managed_adapter_busy_retry_same_key") from None
            state = State(config.db_path, max_executors=config.max_executors)
            try:
                store = TaskStore(ledger)
                store.authority_owned = True
                try:
                    engine = ManagedTasks(config, store, host, None, admission=state)
                    engine.config_path = getattr(config, "_config_path", None)
                    result = await engine.command("cancel", payload)
                finally:
                    store.close()
            finally:
                state.close()
        finally:
            os.close(fd)
    finally:
        singleton.__exit__(None, None, None)
    return dict(result, source="offline_ledger", protocol_version=1)
