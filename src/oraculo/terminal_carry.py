"""Fresh host evidence for an explicitly authorized, reportless maintenance carry.

This preserves reservations and execution outcomes. It is not a completion gate
or cryptographic attestation: the caller-bound transport and local operator are
the trust boundary, exactly as for managed task ACKs.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
from pathlib import Path

from oraculo.protocol import content_hash
from oraculo.run_continuity import terminal_chain
from oraculo.state import State, StateError
from oraculo.t3.client import T3Client
from oraculo.t3.contracts import TERMINAL_STATUSES
from oraculo.task_store import TaskStore

MAX_AGE = 300


class ReadState(State):
    """Only read methods of State; no constructors, migrations or live writes."""

    def __init__(self, config):
        self.connection = sqlite3.connect(
            config.db_path.absolute().as_uri() + "?mode=ro", uri=True
        )
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA query_only=ON")
        self.max_executors = config.max_executors


def load(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
        raise StateError("terminal_carry_proof_invalid")
    try:
        value = json.loads(path.read_bytes())
    except (ValueError, OSError):
        raise StateError("terminal_carry_proof_invalid") from None
    if not isinstance(value, dict):
        raise StateError("terminal_carry_proof_invalid")
    return value


def validate(proof, task_ids, *, fresh=True):
    try:
        if (
            proof["schema_version"] != 1
            or proof["kind"] != "terminal-carry-v1"
            or proof["task_ids"] != task_ids
            or sorted(t["task_id"] for t in proof["tasks"]) != task_ids
            or not proof["caller_thread_id"]
        ):
            raise ValueError
        at = proof["observed_at"]
        if type(at) not in (int, float) or not math.isfinite(at):
            raise ValueError
        if at > time.time() or (fresh and time.time() - at > MAX_AGE):
            raise StateError("terminal_carry_proof_stale")
        runtime = proof["runtime"]
        if (
            type(runtime["pid"]) is not int
            or runtime["pid"] < 1
            or runtime["protocol_version"] != 1
            or not runtime["config_path"]
            or not runtime["config_sha256"]
            or not runtime["release_id"]
        ):
            raise ValueError
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, StateError):
            raise
        raise StateError("terminal_carry_proof_invalid") from None


def task_proof(proof, task, row):
    try:
        record = next(t for t in proof["tasks"] if t["task_id"] == task["id"])
        if record["task_sha256"] != content_hash(task) or record[
            "reservation_sha256"
        ] != content_hash(row):
            raise StateError("terminal_carry_binding_changed")
        snapshot = record["snapshot"]
        thread = snapshot["thread"]
        child = task["child"]
        parent = (
            task["origin"]["thread_id"]
            if task["transport_mode"] == "native_child"
            else None
        )
        if (
            thread["threadId"] != child["childThreadId"]
            or thread["projectId"] != task["project"]["project_id"]
            or thread["parentThreadId"] != parent
            or thread["activeRunId"] is not None
            or thread["pendingRequestCount"] != 0
            or thread["status"] not in TERMINAL_STATUSES
            or snapshot["childrenObserved"] is not True
            or snapshot["hasPendingChildRuns"] is not False
        ):
            raise StateError("terminal_carry_execution_unproven")
        chain = terminal_chain(snapshot)
        if chain[0]["runId"] != child["childRunId"]:
            raise StateError("terminal_carry_binding_changed")
        return dict(
            task_id=task["id"],
            basis="fresh_host_terminal_observation",
            task_sha256=record["task_sha256"],
            reservation_sha256=record["reservation_sha256"],
            child_thread_id=child["childThreadId"],
            child_run_id=child["childRunId"],
            latest_run_id=thread["latestRunId"],
            runs=chain,
            terminal_proof_sha256=content_hash(proof),
        )
    except (KeyError, TypeError, StopIteration):
        raise StateError("terminal_carry_proof_invalid") from None


async def collect(config, controller, host, task_ids):
    from oraculo.reservation_carry import normalize, prove
    from oraculo.service_control import maintenance_acceptance

    task_ids = normalize(task_ids)
    before = controller.status()
    if not maintenance_acceptance(controller, before) or not before.get("runtime"):
        raise StateError("terminal_carry_service_unproven")
    caps = await host.call("orchestrator_capabilities", {})
    caller = caps.get("parentThreadId")
    if not caller:
        raise StateError("caller_bound_parent_required")
    client = T3Client("http://127.0.0.1/mcp")
    client.call = host.call
    records = []
    state = ReadState(config)
    store = TaskStore(config.state_dir / "managed-tasks.sqlite3", read_only=True)
    try:
        for ident in task_ids:
            task = store.get(ident)
            row = state._one(
                "SELECT id,provider,slots,state,receipt FROM managed_reservations WHERE id=?",
                (ident,),
            )
            if row is None:
                raise StateError("carry_reservation_missing")
            snapshot = await client.settlement_snapshot(
                task["child"]["childThreadId"], terminal_only=True, complete_runs=True
            )
            records.append(
                dict(
                    task_id=ident,
                    task_sha256=content_hash(task),
                    reservation_sha256=content_hash(row),
                    snapshot=snapshot,
                )
            )
    finally:
        state.close()
        store.close()
    proof = dict(
        schema_version=1,
        kind="terminal-carry-v1",
        observed_at=time.time(),
        caller_thread_id=caller,
        runtime=before["runtime"],
        task_ids=task_ids,
        tasks=records,
    )
    # A fresh ledger transaction must still match every captured task.
    state = ReadState(config)
    try:
        prove(state, config, task_ids, terminal_proof=proof)
    finally:
        state.close()
    return proof


def pre_stop(config, controller, proof, task_ids, backup_root):
    """Check fresh host/runtime/ledger bindings and back up before any stop."""
    from oraculo.reservation_carry import prove
    from oraculo.runtime import config_hash, write_json

    validate(proof, task_ids)
    current = controller.status()
    if (
        current.get("runtime") != proof["runtime"]
        or config_hash(controller.config) != proof["runtime"]["config_sha256"]
    ):
        raise StateError("terminal_carry_runtime_changed")
    observed = controller.request(
        "managed.read", dict(action="status", payload={"task_id": task_ids[0]})
    )
    if (
        observed.get("source") != "service"
        or observed.get("adapter", {}).get("busy") is not False
    ):
        raise StateError("maintenance_managed_operation_active")
    state = ReadState(config)
    store = TaskStore(config.state_dir / "managed-tasks.sqlite3", read_only=True)
    try:
        result = prove(state, config, task_ids, terminal_proof=proof)
        backup_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        for name, connection in [
            ("state.sqlite3", state.connection),
            ("managed-tasks.sqlite3", store.db),
        ]:
            destination = backup_root / name
            if destination.exists():
                raise StateError("terminal_carry_backup_exists")
            target = sqlite3.connect(destination)
            try:
                connection.backup(target)
            finally:
                target.close()
            destination.chmod(0o600)
        write_json(backup_root / "proof.json", proof)
        for name, raw in [
            ("config.toml", controller.config.read_bytes()),
            ("service.plist", controller.plist_bytes()),
        ]:
            if raw is not None:
                (backup_root / name).write_bytes(raw)
                (backup_root / name).chmod(0o600)
        hashes = {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in backup_root.iterdir()
            if path.is_file()
        }
        write_json(backup_root / "manifest.json", hashes)
        return dict(result, backup_root=str(backup_root), backup_sha256=hashes)
    finally:
        state.close()
        store.close()
