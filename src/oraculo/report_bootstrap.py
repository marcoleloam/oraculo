"""Caller-bound not_run recovery for the exact 3d857301 service.

Only native children: even a cancelled legacy background waiter can leave a
worker alive, but that worker's native branch only flushes the return outbox.
A real withheld ACK excludes caller mutations. Never use this for managed_thread.
The original invalid submission stays in place until confirmed memory and release;
legacy resume therefore cannot release an unfinished recovery after disconnect.
See docs/REPORT_BOOTSTRAP.md for the audited boundary and crash ordering.
"""

from __future__ import annotations

import asyncio
import copy
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import time
from contextlib import asynccontextmanager, contextmanager, nullcontext
from types import SimpleNamespace

from oraculo.cli import make_envelope, request
from oraculo.legacy_failure import Observation, daemon_running, legacy_database
from oraculo.managed_tasks import ManagedTasks, git, workspace
from oraculo.memory.bridge import public_cycle
from oraculo.memory.cycles import report_projection
from oraculo.protocol import MAX_ENVELOPE_BYTES, canonical_json, content_hash
from oraculo.runtime import config_hash
from oraculo.state import StateError
from oraculo.task_store import TaskStore
from oraculo.task_transport import SocketMemory

KNOWN_RELEASE = "3d857301cf8de81725645e04c25a17ce4929b6af"
BARRIER_SECONDS = 840  # fixed manual bridge budget; below legacy's 900-second ACK wait
AUDIT_KIND = "managed.report_bootstrap_released"


@contextmanager
def transaction(db):
    db.execute("BEGIN IMMEDIATE")
    try:
        yield
        db.commit()
    except BaseException:
        db.rollback()
        raise


async def live_service(config, config_path, expected, *, release=KNOWN_RELEASE):
    daemon_running(config)
    envelope = make_envelope("status.get", "*", {})
    reply = await request(config, envelope)
    runtime = reply.get("data", {}).get("runtime", {})
    if (
        reply.get("ok") is not True
        or reply.get("request_id") != envelope["request_id"]
        or runtime != expected
        or set(runtime)
        != {"pid", "release_id", "config_path", "config_sha256", "protocol_version"}
        or runtime.get("release_id") != release
        or runtime.get("config_path") != str(config_path.resolve())
        or runtime.get("config_sha256") != config_hash(config_path)
        or runtime.get("protocol_version") != 1
        or type(runtime.get("pid")) is not int
        or runtime["pid"] <= 1
    ):
        raise StateError("bootstrap_exact_service_required")
    os.kill(runtime["pid"], 0)
    if reply["data"].get("maintenance", {}).get("admission_closed") is not False:
        raise StateError("bootstrap_maintenance_active")
    return runtime


@asynccontextmanager
async def barrier(config, task_id, host):
    reader, writer = await asyncio.open_unix_connection(
        str(config.socket_path), limit=MAX_ENVELOPE_BYTES + 1
    )
    monitor = None
    try:
        envelope = make_envelope(
            "managed.execute",
            "*",
            dict(protocol_version=1, action="status", payload={"task_id": task_id}),
        )
        writer.write(canonical_json(envelope).encode() + b"\n")
        await writer.drain()
        async with asyncio.timeout(BARRIER_SECONDS):
            frame = json.loads(await reader.readline())
            if (
                frame.get("marker") != "ORACULO_SERVICE_HOST_CALL_V1"
                or frame.get("operation_id") != envelope["request_id"]
                or not frame.get("request_id")
                or frame.get("tool") != "orchestrator_capabilities"
                or frame.get("arguments") != {}
            ):
                raise StateError("bootstrap_barrier_unavailable")
            # Invoke the actual caller-bound tool. Never fabricate its answer.
            await host.call(frame["tool"], frame["arguments"])
            owner = asyncio.current_task()

            async def disconnected():
                await reader.read(1)
                owner.cancel()

            monitor = asyncio.create_task(disconnected())
            yield
    finally:
        if monitor:
            monitor.cancel()
            await asyncio.gather(monitor, return_exceptions=True)
        writer.close()
        await writer.wait_closed()


def replacement_for(task, original):
    """One lossless repair, not a license to rewrite a review or invent checks."""
    if not isinstance(original, dict) or not isinstance(original.get("checks"), list):
        raise StateError("bootstrap_not_run_report_required")
    replacement = copy.deepcopy(original)
    unexecuted = []
    metadata = []
    checks = []
    for check in original["checks"]:
        if not isinstance(check, dict) or check.get("status") not in {
            "passed",
            "not_run",
            "blocked",
        }:
            raise StateError("bootstrap_positive_executed_checks_required")
        if check["status"] in {"not_run", "blocked"}:
            if (
                not isinstance(check.get("command"), str)
                or not check["command"].strip()
            ):
                raise StateError("bootstrap_not_run_report_required")
            label = (
                "Not executed (original check): "
                if check["status"] == "not_run"
                else "Blocked validation (original check): "
            )
            unexecuted.append(label + canonical_json(check))
        else:
            executed = copy.deepcopy(check)
            if "detail" in executed:
                # Legacy producers used this unsupported metadata key. Preserve
                # the complete original check without interpreting or replacing
                # result/proof, and keep its executed verdict unchanged.
                metadata.append(
                    "Original check with legacy detail: " + canonical_json(check)
                )
                del executed["detail"]
            checks.append(executed)
    if (
        not unexecuted
        or not checks
        or not isinstance(original.get("limitations", []), list)
    ):
        raise StateError("bootstrap_not_run_report_required")
    replacement["checks"] = checks
    replacement["limitations"] = original.get("limitations", []) + metadata + unexecuted
    commit = original.get("commit")
    if isinstance(commit, str) and re.fullmatch(r"[0-9a-f]{7,39}", commit):
        path = (task.get("child_workspace") or {}).get("path")
        if not path:
            raise StateError("bootstrap_abbreviated_commit_unproven")
        # Git rejects ambiguous object prefixes. Resolve only in the persisted
        # child checkout, then require that same clean HEAD again in fresh_host.
        resolved = git(
            path, "rev-parse", "--verify", "--end-of-options", commit + "^{commit}"
        )
        ws = workspace(path)
        if ws["dirty"] or ws["head"] != resolved:
            raise StateError("bootstrap_abbreviated_commit_unproven")
        replacement["commit"] = resolved
        replacement["limitations"].append(
            "Original abbreviated commit: " + canonical_json(commit)
        )
    kind, failed = ManagedTasks._validate_report(task, replacement)
    if kind != "implementation" or failed:
        raise StateError("bootstrap_positive_implementation_required")
    return replacement


async def cycle_status(config, task):
    parent = task["parent"]
    cycle_id = "cycle-" + content_hash(
        [
            task["project"]["project_id"],
            f"direct:{parent['thread_id']}:{parent['run_id']}:{task['id']}",
        ]
    )
    if (task.get("memory") or {}).get("id") != cycle_id:
        raise StateError("bootstrap_memory_binding_mismatch")
    envelope = make_envelope(
        "memory.cycle.status", task["project"]["project_id"], {"cycle_id": cycle_id}
    )
    envelope["origin"] = {k: parent[k] for k in ("thread_id", "run_id")}
    reply = await request(config, envelope)
    cycle = reply.get("data", {})
    if (
        reply.get("ok") is not True
        or reply.get("request_id") != envelope["request_id"]
        or cycle.get("id") != cycle_id
        or cycle.get("project_id") != task["project"]["project_id"]
        or cycle.get("identity") != dict(envelope["origin"], entry="direct")
        or cycle.get("read_state") not in {"found", "empty"}
        or cycle.get("read_receipt") != task["memory"].get("read_receipt")
    ):
        raise StateError("bootstrap_memory_binding_mismatch")
    return cycle


def memory_matches(cycle, replacement, *, work_status="succeeded"):
    expected = dict(
        schema_version=1,
        cycle_id=cycle["id"],
        project_id=cycle["project_id"],
        identity=cycle["identity"],
        work_status=work_status,
        started_at=cycle["created_at"],
        finished_at=cycle.get("finished_at"),
        read_state=cycle["read_state"],
        read_receipt=cycle["read_receipt"],
        deliveries=cycle["deliveries"],
        evidence=report_projection(replacement)[0],
        scope="selected_structured_report; not a conversation archive",
    )
    if cycle.get("work_status") != work_status or cycle.get(
        "report_sha256"
    ) != content_hash(expected):
        raise StateError("bootstrap_memory_report_mismatch")


def cycle_unchanged(cycle, before):
    if any(
        cycle.get(k) != before.get(k)
        for k in (
            "id",
            "project_id",
            "identity",
            "created_at",
            "read_state",
            "read_receipt",
            "deliveries",
        )
    ):
        raise StateError("bootstrap_memory_changed")


async def fresh_host(config, task, host, replacement):
    engine = ManagedTasks(config, Observation(task), host, None)
    actor, _ = await engine._identity()
    if (
        task["parent"] != task["origin"]
        or any(actor[k] != task["origin"][k] for k in ("thread_id", "project_id"))
        or actor["thread_id"] == task["child"]["childThreadId"]
    ):
        raise StateError("bootstrap_original_origin_required")
    await engine._scope(task)
    engine._validate_report(task, replacement)
    _, _, positive = await engine._execution_gate(task)
    # One complete observation supplies both positive and terminal evidence.
    # Do not re-read the same child three times through the manual bridge.
    # fresh_host is still called independently before and after memory, and
    # checkout validation below uses this observation's actual thread binding.
    thread, runs = positive["thread"], positive["recentRuns"]
    if (
        thread["latestRunId"] != task["child"]["childRunId"]
        or thread["activeRunId"] is not None
        or thread["pendingRequestCount"] != 0
        or thread["status"] != "completed"
        or positive["hasPendingChildRuns"] is not False
        or positive["childrenObserved"] is not True
        or len({r["runId"] for r in runs}) != len(runs)
        or not any(r["runId"] == task["child"]["childRunId"] for r in runs)
        or any(r["status"] != "completed" for r in runs)
    ):
        raise StateError("bootstrap_terminal_unproven")
    ws = engine._report_workspace(task, thread)
    if ws["dirty"] or ws["head"] != replacement["commit"]:
        raise StateError("validated_commit_or_clean_workspace_required")
    # _execution_gate now checks native task status (including original identity,
    # result availability and pending questions) in this same observation. Do not
    # duplicate that call and exhaust the fixed manual bridge budget.
    return actor, positive


def reservation(db, task_id):
    db.row_factory = sqlite3.Row
    row = db.execute(
        "SELECT * FROM managed_reservations WHERE id=?", (task_id,)
    ).fetchone()
    return dict(row) if row else None


async def recover(config, config_path, payload, host):
    if (
        not isinstance(payload, dict)
        or set(payload)
        != {"task_id", "expected_report_sha256", "report", "reason", "expected_service"}
        or not isinstance(payload.get("task_id"), str)
        or not payload["task_id"]
        or not isinstance(payload.get("reason"), str)
        or not payload["reason"].strip()
        or len(payload["reason"]) > 2000
    ):
        raise StateError("bootstrap_payload_required")
    fd = os.open(
        config.state_dir / "report-bootstrap.lock",
        os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
        0o600,
    )
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        runtime = await live_service(config, config_path, payload["expected_service"])
        async with barrier(config, payload["task_id"], host):
            with legacy_database(
                config.state_dir / "managed-tasks.sqlite3", version=4, writable=True
            ) as ledger:
                with legacy_database(config.db_path, version=4, writable=True) as main:
                    return await _recover(
                        config, config_path, payload, host, runtime, ledger, main
                    )
    except StateError:
        raise
    except (sqlite3.Error, KeyError, TypeError, ValueError, AttributeError) as exc:
        raise StateError("bootstrap_evidence_invalid") from exc
    finally:
        os.close(fd)


async def _recover(config, config_path, payload, host, runtime, ledger, main):
    task_id = payload["task_id"]
    store = TaskStore.__new__(TaskStore)
    store.db = ledger
    store.state = SimpleNamespace(
        path=config.state_dir / "managed-tasks.sqlite3",
        transaction=lambda: transaction(ledger),
    )
    task = store.get(task_id)
    raw = ledger.execute(
        "SELECT payload FROM managed_tasks WHERE id=?", (task_id,)
    ).fetchone()[0]
    if (
        task.get("transport_mode") != "native_child"
        or not task.get("child")
        or any(
            task.get(k)
            for k in (
                "publication",
                "publication_attempted",
                "publication_request",
                "publication_spec",
                "merged",
                "settled",
            )
        )
    ):
        raise StateError("bootstrap_native_unpublished_task_required")
    prior = task.get("report_bootstrap")
    original = prior["original_report"] if prior else task.get("submitted_report")
    replacement = replacement_for(task, original)
    if payload["report"] != replacement or payload[
        "expected_report_sha256"
    ] != content_hash(original):
        raise StateError("bootstrap_lossless_replacement_required")
    submissions = [
        e["proof"].get("report_sha256")
        for e in task["events"]
        if e["phase"] == "validation_submitted"
    ]
    if not submissions or submissions[-1] != content_hash(original):
        raise StateError("bootstrap_submission_digest_mismatch")
    actor, snapshot = await fresh_host(config, task, host, replacement)
    cycle = await cycle_status(config, task)
    row = reservation(main, task_id)
    if prior is None:
        invalid_submission = task.get("blocked_reason") == "malformed_validation_check"
        if task.get("blocked_reason") == "terminal_task_requires_explicit_new_revision":
            # A refused continue can replace the visible reason without starting
            # another run. Require the original validation failure after the
            # most recent submission; fresh_host still proves the original run.
            for event in reversed(task["events"]):
                if event["phase"] == "validation_submitted":
                    break
                if event.get("proof", {}).get("reason") == "malformed_validation_check":
                    invalid_submission = True
        if (
            task.get("work_status") != "pending"
            or task.get("report") is not None
            or task.get("report_recovery") is not None
            or not invalid_submission
            or cycle.get("work_status") != "running"
            or cycle.get("write_state") != "not_requested"
            or cycle.get("write_receipt") is not None
            or cycle.get("report_sha256") is not None
            or not row
            or row["state"] != "executing"
            or any(
                row[k] != (task.get("admission") or {}).get(k)
                for k in ("id", "provider", "slots")
            )
            or json.loads(row["receipt"] or "null")
            != task["child"].get("launch_receipt")
        ):
            raise StateError("bootstrap_pristine_reserved_submission_required")
        prior = dict(
            version=1,
            original_report=original,
            original_sha256=content_hash(original),
            original_payload=raw,
            original_payload_sha256=hashlib.sha256(raw.encode()).hexdigest(),
            replacement=replacement,
            replacement_sha256=content_hash(replacement),
            reason=payload["reason"],
            service=runtime,
            reservation=row,
            actor=actor,
            terminal_snapshot=snapshot,
            cycle_before=cycle,
        )
    elif (
        prior.get("version") != 1
        or prior.get("service") != runtime
        or prior.get("reason") != payload["reason"]
        or prior.get("replacement") != replacement
        or prior.get("original_sha256") != content_hash(original)
        or prior.get("replacement_sha256") != content_hash(replacement)
        or hashlib.sha256(prior["original_payload"].encode()).hexdigest()
        != prior["original_payload_sha256"]
    ):
        raise StateError("bootstrap_intent_mismatch")
    completed = task.get("report_recovery") is not None
    cycle_unchanged(cycle, prior["cycle_before"])
    original_task = json.loads(prior["original_payload"])
    if any(
        task.get(k) != original_task.get(k)
        for k in (
            "origin",
            "parent",
            "project",
            "request",
            "child",
            "child_workspace",
            "parent_workspace",
            "base_sha",
            "expected_branch",
            "transport_mode",
            "admission",
        )
    ):
        raise StateError("bootstrap_task_binding_changed")
    if completed and (
        task.get("report") != replacement
        or task.get("work_status") != "succeeded"
        or task["report_recovery"].get("original_report") != original
        or task["report_recovery"].get("replacement_sha256")
        != content_hash(replacement)
    ):
        raise StateError("bootstrap_completed_result_changed")
    if task.get("submitted_report") != (replacement if completed else original):
        raise StateError("bootstrap_submission_changed")
    if not completed and (
        task.get("report") is not None or task.get("work_status") != "pending"
    ):
        raise StateError("bootstrap_task_changed")
    if row is not None and row != prior["reservation"]:
        raise StateError("bootstrap_reservation_changed")

    def cas():
        if (
            ledger.execute(
                "SELECT payload FROM managed_tasks WHERE id=?", (task_id,)
            ).fetchone()[0]
            != raw
        ):
            raise StateError("bootstrap_task_changed")

    def update(phase, proof, **changes):
        nonlocal task, raw
        with transaction(ledger):
            cas()
            store.state.transaction = nullcontext
            try:
                task = store.update(task_id, phase, proof, **changes)
            finally:
                store.state.transaction = lambda: transaction(ledger)
        raw = canonical_json(task)

    if cycle["write_state"] != "not_requested":
        if task.get("report_bootstrap") is None:
            raise StateError("bootstrap_memory_already_requested")
        memory_matches(cycle, replacement)
    elif cycle != prior["cycle_before"]:
        raise StateError("bootstrap_memory_changed")
    await live_service(config, config_path, runtime)
    if task.get("report_bootstrap") is None:
        # Durable intention before any service memory write. Keep malformed report.
        if cycle != await cycle_status(config, task):
            raise StateError("bootstrap_memory_changed")
        update(
            task["phase"],
            {"bootstrap_intent": content_hash(prior)},
            report_bootstrap=prior,
        )
    if not ManagedTasks._saved(cycle):
        await SocketMemory(config).finish(
            task["project"]["project_id"],
            task["parent"],
            cycle["id"],
            "succeeded",
            replacement,
        )
    cycle = await cycle_status(config, task)
    cycle_unchanged(cycle, prior["cycle_before"])
    memory_matches(cycle, replacement)
    if not ManagedTasks._saved(cycle):
        raise StateError("memory_write_unconfirmed")
    final_actor, final_snapshot = await fresh_host(config, task, host, replacement)
    if final_actor != actor or final_snapshot != snapshot:
        raise StateError("bootstrap_host_changed")
    await live_service(config, config_path, runtime)
    if cycle != await cycle_status(config, task):
        raise StateError("bootstrap_memory_changed")
    audit = canonical_json(
        dict(
            task_id=task_id,
            intent_sha256=content_hash(prior),
            memory_sha256=cycle["report_sha256"],
            memory_commit=cycle["write_receipt"]["commit"],
        )
    )
    # Main commits first. A crash can leave an audited release with the original
    # malformed ledger, never a valid report that legacy resume can release early.
    with transaction(ledger), transaction(main):
        cas()
        daemon_running(config)
        os.kill(runtime["pid"], 0)
        if config_hash(config_path) != runtime["config_sha256"]:
            raise StateError("bootstrap_exact_service_required")
        stored_cycle = json.loads(
            main.execute(
                "SELECT payload FROM memory_cycles WHERE id=?", (cycle["id"],)
            ).fetchone()[0]
        )
        memory_matches(stored_cycle, replacement)
        if public_cycle(stored_cycle) != cycle:
            raise StateError("bootstrap_memory_changed")
        current = reservation(main, task_id)
        logged = main.execute(
            "SELECT 1 FROM audit_events WHERE kind=? AND payload=?", (AUDIT_KIND, audit)
        ).fetchone()
        if current is None:
            if not logged:
                raise StateError("bootstrap_release_unproven")
        elif current != prior["reservation"] or logged:
            raise StateError("bootstrap_reservation_changed")
        else:
            main.execute(
                "INSERT INTO audit_events(kind,payload,created_at) VALUES(?,?,?)",
                (AUDIT_KIND, audit, time.time()),
            )
            main.execute("DELETE FROM managed_reservations WHERE id=?", (task_id,))
    if not completed:
        update(
            "pr_pending",
            {
                "bootstrap_confirmed": content_hash(prior),
                "memory_commit": cycle["write_receipt"]["commit"],
            },
            submitted_report=replacement,
            report=replacement,
            memory=cycle,
            work_status="succeeded",
            deliverable_kind="implementation",
            approval_state="not_required",
            report_recovery=dict(
                original_report=original,
                original_sha256=content_hash(original),
                replacement_sha256=content_hash(replacement),
                invalid_reason="malformed_validation_check",
                reason=payload["reason"],
                actor=prior["actor"],
                terminal_snapshot=snapshot,
            ),
        )
    await ManagedTasks(config, store, host, None).flush_returns(task_id)
    result = store.describe(task_id)
    result.pop("report_bootstrap", None)
    return dict(
        result,
        recovery="confirmed",
        admission={"state": "not_reserved"},
        source="bootstrap_with_exact_live_service",
    )
