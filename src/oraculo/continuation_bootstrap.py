"""Strict pre-upgrade completion for native continuations on release 01efe9c.

No report repair or run rebinding. The exact legacy service owns memory; its
real held host call serializes managed mutations. Internal schema-4 access is
encapsulated here, with durable intent, fresh host proofs and final CAS.
"""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import sqlite3
import time
from contextlib import nullcontext
from types import SimpleNamespace

from oraculo import report_bootstrap as shared
from oraculo.legacy_failure import Observation, daemon_running, legacy_database
from oraculo.managed_tasks import ManagedTasks
from oraculo.memory.bridge import public_cycle
from oraculo.protocol import canonical_json, content_hash
from oraculo.run_continuity import report_link
from oraculo.runtime import config_hash
from oraculo.state import StateError
from oraculo.task_store import TaskStore
from oraculo.task_transport import SocketMemory

KNOWN_RELEASE = "01efe9cffd8a1874cb84e5230805055ad4345150"
AUDIT_KIND = "managed.continuation_bootstrap_released"


async def live_service(config, config_path, expected):
    return await shared.live_service(
        config, config_path, expected, release=KNOWN_RELEASE
    )


async def fresh_host(config, task, host, report):
    # Retain descendant histories too: a same-size change in the descendant tree
    # must not disappear behind hasPendingChildRuns=false/observedChildCount.
    reads = []

    class EvidenceHost:
        async def call(self, name, args):
            result = await host.call(name, args)
            if name == "task_status":
                reads.append(
                    dict(tool=name, arguments=args, result=copy.deepcopy(result))
                )
            elif (
                name == "t3_thread_read"
                and args["threadId"] != task["origin"]["thread_id"]
            ):
                reads.append(
                    dict(
                        tool=name,
                        arguments=args,
                        result=copy.deepcopy(
                            {k: result[k] for k in ("thread", "recentRuns")}
                        ),
                    )
                )
            return result

    engine = ManagedTasks(config, Observation(task), EvidenceHost(), None)
    actor, _ = await engine._identity()
    if (
        task["parent"] != task["origin"]
        or any(actor[k] != task["origin"][k] for k in ("thread_id", "project_id"))
        or actor["thread_id"] == task["child"]["childThreadId"]
    ):
        raise StateError("continuation_original_origin_required")
    await engine._scope(task)
    kind, failed = engine._validate_report(task, report)
    if kind != "implementation":
        raise StateError("continuation_implementation_required")
    if failed:
        snapshot = await engine._terminal_gate(task)
    else:
        _, _, snapshot = await engine._execution_gate(task, report)
    evidence = report_link(task, snapshot, report)
    if len(evidence["runs"]) < 2:
        raise StateError("continuation_multiple_runs_required")
    ws = engine._report_workspace(task, snapshot["thread"])
    # This compatibility path is deliberately stricter than ordinary negative
    # finish: even negative evidence must name the exact, clean checkout.
    if ws["dirty"] or ws["head"] != report.get("commit"):
        raise StateError("validated_commit_or_clean_workspace_required")
    original_status = evidence["runs"][0]["status"]
    status = (
        "succeeded"
        if not failed
        else "stopped"
        if original_status in {"cancelled", "interrupted"}
        else "failed"
    )
    return actor, dict(snapshot=snapshot, reads=reads, workspace=ws), evidence, status


async def recover(config, config_path, payload, host):
    if (
        not isinstance(payload, dict)
        or set(payload)
        != {"task_id", "expected_report_sha256", "reason", "expected_service"}
        or not isinstance(payload.get("task_id"), str)
        or not payload["task_id"]
        or not isinstance(payload.get("reason"), str)
        or not payload["reason"].strip()
        or len(payload["reason"]) > 2000
    ):
        raise StateError("continuation_bootstrap_payload_required")
    # Shared with the other bootstrap CLI; the daemon lifetime ledger flock is
    # intentionally left held by the daemon. Its real ACK barrier owns exclusion.
    fd = os.open(
        config.state_dir / "report-bootstrap.lock",
        os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
        0o600,
    )
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        runtime = await live_service(config, config_path, payload["expected_service"])
        async with shared.barrier(config, payload["task_id"], host):
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
        raise StateError("continuation_bootstrap_evidence_invalid") from exc
    finally:
        os.close(fd)


async def _recover(config, config_path, payload, host, runtime, ledger, main):
    task_id = payload["task_id"]
    store = TaskStore.__new__(TaskStore)
    store.db = ledger
    store.state = SimpleNamespace(
        path=config.state_dir / "managed-tasks.sqlite3",
        transaction=lambda: shared.transaction(ledger),
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
                "report_recovery",
                "report_bootstrap",
            )
        )
    ):
        raise StateError("continuation_native_unpublished_task_required")
    prior = task.get("continuation_bootstrap")
    report = task.get("submitted_report")
    if (
        not isinstance(report, dict)
        or content_hash(report) != payload["expected_report_sha256"]
    ):
        raise StateError("continuation_submission_digest_mismatch")
    submissions = [e for e in task["events"] if e["phase"] == "validation_submitted"]
    if not submissions or any(
        e["proof"].get("report_sha256") != content_hash(report) for e in submissions
    ):
        raise StateError("continuation_submission_digest_mismatch")
    # Pin provenance/chain on retry even before memory was saved. A later run
    # cannot appropriate an earlier intent. Never mask persisted evidence in CAS.
    gate_task = (
        dict(task, completion_evidence=prior["completion_evidence"]) if prior else task
    )
    actor, snapshot, evidence, status = await fresh_host(
        config, gate_task, host, report
    )
    cycle = await shared.cycle_status(config, task)
    row = shared.reservation(main, task_id)
    if prior is None:
        if (
            task.get("work_status") != "pending"
            or task.get("report") is not None
            or task.get("completion_evidence") is not None
            or task.get("phase")
            not in {"blocked", "validation_submitted", "validating"}
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
            raise StateError("continuation_pristine_reserved_submission_required")
        prior = dict(
            version=1,
            original_report=report,
            original_sha256=content_hash(report),
            original_payload=raw,
            original_payload_sha256=hashlib.sha256(raw.encode()).hexdigest(),
            reason=payload["reason"],
            service=runtime,
            reservation=row,
            actor=actor,
            terminal_snapshot=snapshot,
            completion_evidence=evidence,
            work_status=status,
            cycle_before=cycle,
        )
    elif (
        prior.get("version") != 1
        or prior.get("service") != runtime
        or prior.get("reason") != payload["reason"]
        or prior.get("original_report") != report
        or prior.get("original_sha256") != content_hash(report)
        or prior.get("completion_evidence") != evidence
        or prior.get("terminal_snapshot") != snapshot
        or prior.get("work_status") != status
        or hashlib.sha256(prior["original_payload"].encode()).hexdigest()
        != prior["original_payload_sha256"]
    ):
        raise StateError("continuation_intent_mismatch")
    shared.cycle_unchanged(cycle, prior["cycle_before"])
    original_task = json.loads(prior["original_payload"])
    if (
        any(
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
        )
        or task["events"][: len(original_task["events"])] != original_task["events"]
    ):
        raise StateError("continuation_task_binding_changed")
    completed = task.get("continuation_recovery") is not None
    if completed:
        if (
            task.get("report") != report
            or task.get("work_status") != status
            or task["continuation_recovery"] != dict(intent_sha256=content_hash(prior))
            or task.get("completion_evidence") != evidence
        ):
            raise StateError("continuation_completed_result_changed")
    elif (
        task.get("report") is not None
        or task.get("work_status") != "pending"
        or task.get("completion_evidence") is not None
    ):
        raise StateError("continuation_task_changed")
    if row is not None and row != prior["reservation"]:
        raise StateError("continuation_reservation_changed")

    def cas():
        if (
            ledger.execute(
                "SELECT payload FROM managed_tasks WHERE id=?", (task_id,)
            ).fetchone()[0]
            != raw
        ):
            raise StateError("continuation_task_changed")

    def update(phase, proof, **changes):
        nonlocal task, raw
        with shared.transaction(ledger):
            cas()
            store.state.transaction = nullcontext
            try:
                task = store.update(task_id, phase, proof, **changes)
            finally:
                store.state.transaction = lambda: shared.transaction(ledger)
        raw = canonical_json(task)

    def memory_matches(value):
        shared.memory_matches(value, report, work_status=status)

    if cycle["write_state"] != "not_requested":
        if task.get("continuation_bootstrap") is None:
            raise StateError("continuation_memory_already_requested")
        memory_matches(cycle)
    elif cycle != prior["cycle_before"]:
        raise StateError("continuation_memory_changed")
    await live_service(config, config_path, runtime)
    if task.get("continuation_bootstrap") is None:
        if cycle != await shared.cycle_status(config, task):
            raise StateError("continuation_memory_changed")
        update(
            task["phase"],
            {"continuation_intent": content_hash(prior)},
            continuation_bootstrap=prior,
        )
    if not ManagedTasks._saved(cycle):
        await SocketMemory(config).finish(
            task["project"]["project_id"], task["parent"], cycle["id"], status, report
        )
    cycle = await shared.cycle_status(config, task)
    shared.cycle_unchanged(cycle, prior["cycle_before"])
    memory_matches(cycle)
    if not ManagedTasks._saved(cycle):
        raise StateError("memory_write_unconfirmed")
    final = await fresh_host(
        config, dict(task, completion_evidence=evidence), host, report
    )
    if final != (actor, snapshot, evidence, status):
        raise StateError("continuation_host_changed")
    await live_service(config, config_path, runtime)
    if cycle != await shared.cycle_status(config, task):
        raise StateError("continuation_memory_changed")
    audit = canonical_json(
        dict(
            task_id=task_id,
            intent_sha256=content_hash(prior),
            memory_sha256=cycle["report_sha256"],
            memory_commit=cycle["write_receipt"]["commit"],
        )
    )
    # Both local snapshots are locked while the main release is committed first.
    # A crash before ledger completion is recoverable ONLY with this exact audit.
    with shared.transaction(ledger), shared.transaction(main):
        cas()
        if any(
            db.execute("SELECT max(version) FROM schema_migrations").fetchone()[0] != 4
            for db in (ledger, main)
        ):
            raise StateError("legacy_schema_required")
        daemon_running(config)
        os.kill(runtime["pid"], 0)
        if config_hash(config_path) != runtime["config_sha256"]:
            raise StateError("bootstrap_exact_service_required")
        stored_cycle = json.loads(
            main.execute(
                "SELECT payload FROM memory_cycles WHERE id=?", (cycle["id"],)
            ).fetchone()[0]
        )
        memory_matches(stored_cycle)
        if public_cycle(stored_cycle) != cycle:
            raise StateError("continuation_memory_changed")
        current = shared.reservation(main, task_id)
        logged = main.execute(
            "SELECT 1 FROM audit_events WHERE kind=? AND payload=?", (AUDIT_KIND, audit)
        ).fetchone()
        if current is None:
            if not logged:
                raise StateError("continuation_release_unproven")
        elif current != prior["reservation"] or logged:
            raise StateError("continuation_reservation_changed")
        else:
            main.execute(
                "INSERT INTO audit_events(kind,payload,created_at) VALUES(?,?,?)",
                (AUDIT_KIND, audit, time.time()),
            )
            main.execute("DELETE FROM managed_reservations WHERE id=?", (task_id,))
    if not completed:
        update(
            "pr_pending" if status == "succeeded" else "blocked",
            {
                "continuation_confirmed": content_hash(prior),
                "memory_commit": cycle["write_receipt"]["commit"],
            },
            report=report,
            memory=cycle,
            work_status=status,
            deliverable_kind="implementation",
            approval_state="not_required",
            completion_evidence=evidence,
            continuation_recovery=dict(intent_sha256=content_hash(prior)),
            blocked_reason=None if status == "succeeded" else "work_not_successful",
        )
    await ManagedTasks(config, store, host, None).flush_returns(task_id)
    result = store.describe(task_id)
    result.pop("continuation_bootstrap", None)
    return dict(
        result,
        recovery="confirmed",
        admission={"state": "not_reserved"},
        source="bootstrap_with_exact_live_service",
    )
