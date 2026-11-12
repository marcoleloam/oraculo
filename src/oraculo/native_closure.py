"""Closure of submitted tasks without a second bridge per task.

Two entry points share the engine's existing gates and add none of their own:

- ``daemon_close``: the background reconciler certifies a native child whose
  executor already submitted its report. The daemon has no parent identity, so
  the parent-scoped ``task_status`` read is the one proof it skips; the
  completion evidence says so. A missing proof never blocks the task: the
  attempt is retried later and the origin gets one actionable notice.
- ``resume_submitted``: one caller-bound operation resumes every submitted task
  of the calling origin, taking each task's scope in turn.

Nothing here launches, delegates or publishes on the daemon's behalf.
"""

from __future__ import annotations

import time

from oraculo import blocked_recovery
from oraculo.run_continuity import execution_run, terminal_chain
from oraculo.state import StateError

CLOSURE_MODE = "daemon_reconciled_without_native_task_status"
CLOSURE_RECEIPT = {
    "mode": CLOSURE_MODE,
    "skipped_read": "task_status",
    "reason": "parent_scoped_read_unavailable_to_daemon",
}
META_PREFIX = "managed_daemon_closure:"
BACKOFF_BASE_SECONDS = 60
BACKOFF_MAX_SECONDS = 1800
NOTICE_AFTER_FAILURES = 5
MAX_ATTEMPTS = 24
BATCH_LIMIT = 32
BATCH_COMMAND = "oraculo task resume --all-submitted"

# The bridge or the daemon's own client stopped answering. The host may or may
# not have acted; nothing about the task itself was judged.
INTERRUPTIONS = frozenset(
    {
        "managed_host_ack_eof",
        "managed_host_ack_timeout",
        "managed_host_ack_disconnected",
        "managed_host_ack_invalid",
        "managed_host_ack_mismatch",
        "managed_host_ack_limit",
        "managed_host_frame_limit",
        "managed_host_channel_failed",
        "host_response_uncertain",
        "managed_background_host_timeout",
        "managed_background_host_uncertain",
    }
)
# Waiting does not repair these: the origin is told on the first failure.
DEFINITIVE = frozenset(
    {
        "validated_commit_or_clean_workspace_required",
        "original_task_run_not_observed",
        "terminal_run_chain_incomplete",
        "terminal_run_chain_incoherent",
        "task_thread_binding_mismatch",
        "child_workspace_not_isolated",
        "execution_changed_after_validation",
        "latest_run_report_unproven",
        "daemon_closure_submission_unbound",
        "native_original_run_unbound",
        "terminal_descendant_review_required",
        "terminal_report_reused",
        "expected_run_not_completed",
        "managed_project_scope_revoked",
        "managed_project_binding_changed",
    }
)
_WAITING_PHASES = frozenset({"validation_submitted", "memory_pending"})


def enabled(engine) -> bool:
    return getattr(engine.config, "managed_native_daemon_closure", True) is True


def pre_effect_interruption(task: dict) -> bool:
    """Blocked by a lost bridge before the first closure effect was recorded.

    ``report`` is written together with ``memory_pending``, the first step that
    leads to a memory write. Without it the interrupted attempt changed nothing.
    """
    return (
        task.get("phase") == "blocked"
        and task.get("blocked_reason") in INTERRUPTIONS
        and task.get("report") is None
        and not task.get("publication")
        and not task.get("publication_attempted")
    )


def submitted(task: dict) -> bool:
    """A bound child whose executor delivered a report not yet certified."""
    return (
        bool((task.get("child") or {}).get("childThreadId"))
        and task.get("submitted_report") is not None
        and task.get("work_status") != "cancelled"
        and not task.get("supersession")
        and not task.get("origin_completion")
        and not task.get("legacy_delivery_decision")
        and not task.get("merged")
        and not task.get("settled")
    )


def _state(engine, task_id: str) -> dict:
    return engine.store.state.get_meta(META_PREFIX + task_id) or {}


def _due(engine, task_id: str) -> bool:
    state = _state(engine, task_id)
    return not state.get("exhausted") and state.get("next_attempt_at", 0) <= time.time()


def daemon_work(engine, task: dict) -> str | None:
    """What the daemon may do for a native child right now; None is nothing."""
    if not enabled(engine) or not task.get("child"):
        return None
    if engine._certified(task):
        # Certified results are never observed again. Only a reservation left
        # by an interrupted certification is still owed.
        if engine._reservation_held(task["id"]) and _due(engine, task["id"]):
            return "release"
        return None
    if not submitted(task):
        return None
    if task.get("phase") not in _WAITING_PHASES and not pre_effect_interruption(task):
        return None
    return "close" if _due(engine, task["id"]) else None


def require_bound_submission(task: dict, evidence: dict, snapshot: dict) -> None:
    """The report must be the executor's own, from the run that last executed.

    The caller-bound path also has the parent's ``task_status`` receipt. The
    daemon does not, so it accepts no origin-authored or legacy unbound report.
    """
    executed = execution_run(terminal_chain(snapshot))
    actor = evidence.get("submission_actor") or {}
    if (
        not evidence.get("submission_event_id")
        or evidence.get("legacy_submission")
        or actor.get("thread_id") != task["child"]["childThreadId"]
        or actor.get("run_id") != executed["runId"]
    ):
        raise StateError("daemon_closure_submission_unbound")


def _code(exc: BaseException) -> str:
    if isinstance(exc, StateError):
        return str(exc)[:200]
    return str(getattr(exc, "reason", getattr(exc, "code", type(exc).__name__)))[:200]


def _record_failure(engine, task_id: str, code: str, definitive: bool) -> None:
    store = engine.store
    state = _state(engine, task_id)
    attempts = state.get("attempts", 0) + 1
    delay = min(BACKOFF_BASE_SECONDS * 2 ** (attempts - 1), BACKOFF_MAX_SECONDS)
    notified = bool(state.get("notified"))
    if not notified and (definitive or attempts >= NOTICE_AFTER_FAILURES):
        task = store.get(task_id)
        # One event in the task's own return outbox. The phase is unchanged, so
        # the task stays resumable exactly as the executor left it.
        store.update(
            task_id,
            task["phase"],
            {
                "daemon_closure": "origin_action_required",
                "missing": code,
                "definitive": definitive,
                "attempts": attempts,
                "command": BATCH_COMMAND,
                "single_task_payload": {"task_id": task_id},
            },
            daemon_closure={
                "state": "origin_action_required",
                "missing": code,
                "phase": task["phase"],
                "blocked_reason": task.get("blocked_reason"),
                "command": BATCH_COMMAND,
            },
        )
        notified = True
    store.state.set_meta(
        META_PREFIX + task_id,
        {
            "attempts": attempts,
            "reason": code,
            "definitive": definitive,
            "next_attempt_at": time.time() + delay,
            "notified": notified,
            "exhausted": attempts >= MAX_ATTEMPTS,
        },
    )


async def daemon_close(engine, task_id: str) -> None:
    """Certify or release one native child from the background reconciler."""
    from oraculo.managed_tasks import ReportValidationError

    store = engine.store
    if engine.admission is not None and engine.admission.get_meta(
        "runtime_maintenance"
    ):
        return
    task = store.get(task_id)
    report = task["report"] if engine._certified(task) else task["submitted_report"]
    engine.native_status_unavailable = True
    try:
        await engine._scope(task)
        await engine._finish(task_id, report, task["origin"])
    except Exception as exc:
        code = _code(exc)
        current = store.get(task_id)
        if engine._certified(current) and not engine._reservation_held(task_id):
            # Certified and released: a final state, never retried. A negative
            # result keeps the same blocker the caller-bound path records.
            if code == "work_not_successful" and current["phase"] != "blocked":
                store.update(task_id, "blocked", {"reason": code}, blocked_reason=code)
            store.state.set_meta(META_PREFIX + task_id, None)
            return
        _record_failure(
            engine,
            task_id,
            code,
            isinstance(exc, ReportValidationError) or code in DEFINITIVE,
        )
    else:
        store.state.set_meta(META_PREFIX + task_id, None)
    finally:
        engine.native_status_unavailable = False


def _entry(task: dict, outcome: str, **extra) -> dict:
    return {
        "task_id": task["id"],
        "title": (task.get("request") or {}).get("title"),
        "outcome": outcome,
        "phase": task.get("phase"),
        "work_status": task.get("work_status"),
        "blocked_reason": task.get("blocked_reason"),
        **{k: v for k, v in extra.items() if v is not None},
    }


def _batch_candidate(task: dict) -> bool:
    return submitted(task) and (
        task.get("phase") == "validation_submitted" or pre_effect_interruption(task)
    )


async def resume_submitted(engine, payload: dict, actor: dict, caps: dict) -> dict:
    """Resume every submitted task of the calling origin in one operation.

    Identity was read once by the caller. The project catalog is read fresh for
    the first task and shared by the rest of this operation; every terminal,
    descendant, workspace and post-memory proof is read again for each task.
    """
    from oraculo.managed_tasks import ReportValidationError

    if not isinstance(payload, dict) or set(payload) - {"project"}:
        raise StateError("batch_resume_payload_invalid")
    project = payload.get("project")
    if project is not None and (not isinstance(project, str) or not project):
        raise StateError("batch_resume_payload_invalid")
    store = engine.store
    selected = []
    for row in store.db.execute(
        "SELECT id FROM managed_tasks WHERE origin=? ORDER BY rowid",
        (actor["thread_id"],),
    ):
        task = store.get(row[0])
        if project and (task.get("project") or {}).get("project_id") != project:
            continue
        if _batch_candidate(task):
            selected.append(row[0])
    results, attempted, catalog, stopped = [], 0, {}, None
    for task_id in selected:
        task = store.get(task_id)
        if stopped:
            results.append(_entry(task, "not_attempted", reason=stopped))
            continue
        if attempted >= BATCH_LIMIT:
            results.append(_entry(task, "deferred", reason="batch_limit"))
            continue
        if engine.scopes is not None:
            try:
                engine.scopes.claim(("task", task_id))
            except StateError as exc:
                if str(exc) != "managed_scope_busy":
                    raise
                results.append(
                    _entry(
                        task,
                        "skipped_busy",
                        reason="managed_scope_busy",
                        operation_id=getattr(exc, "operation_id", None),
                    )
                )
                continue
        task = store.get(task_id)
        if not _batch_candidate(task):
            # Another operation closed or changed it before this scope was taken.
            results.append(_entry(task, "skipped_changed"))
            continue
        attempted += 1
        # Each task is its own operation: its own record of effects, so a
        # bridge lost during this task's reads is judged without the effects
        # of the tasks closed before it. Only the project catalog crosses
        # tasks. No thread read is shared.
        engine._begin_operation()
        engine._proofs = dict(catalog)
        reason = None
        try:
            await engine._scope(task, fresh=not catalog)
            catalog = {
                k: v for k, v in engine._proofs.items() if k[0] == "t3_project_list"
            }
            await engine._advance(task_id, actor, caps)
        except ReportValidationError as exc:
            results.append(_entry(store.get(task_id), "rejected", reason=str(exc)))
            continue
        except Exception as exc:
            reason = _code(exc)
            if getattr(exc, "retryable", False) and str(exc) == "memory_cycle_timeout":
                # Same rule as a single resume: keep the reservation, block nothing.
                results.append(_entry(store.get(task_id), "retryable", reason=reason))
                continue
            if engine._interrupted_before_effect(exc):
                # Only reads were issued for this task: it keeps its phase,
                # exactly as a single resume would leave it.
                store.record_diagnostic(
                    task_id,
                    {
                        "kind": "host_channel_interrupted_before_effect",
                        "action": "resume-submitted",
                        "reason": reason,
                        "tool": engine._operation.failed_tool,
                        "phase": store.get(task_id)["phase"],
                    },
                )
                stopped = reason
                results.append(
                    _entry(
                        store.get(task_id),
                        "interrupted",
                        reason=reason,
                        recovery=blocked_recovery.recovery("resume", task_id, reason),
                    )
                )
                continue
            store.update(
                task_id,
                "blocked",
                {
                    "reason": reason,
                    **(
                        {"remote_observations": exc.observations}
                        if hasattr(exc, "observations")
                        else {}
                    ),
                },
                blocked_reason=reason,
                recovery=blocked_recovery.recovery("resume", task_id, reason),
            )
            if reason in INTERRUPTIONS:
                # The bridge is gone: later tasks would only inherit its error.
                stopped = reason
                results.append(_entry(store.get(task_id), "interrupted", reason=reason))
                continue
        if engine._operation.failure is None:
            await engine.flush_returns(task_id)
        current = store.get(task_id)
        if current["phase"] == "blocked":
            outcome = (
                "closed_negative"
                if engine._certified(current) and reason == "work_not_successful"
                else "blocked"
            )
        elif engine._certified(current):
            outcome = "closed"
        else:
            outcome = "pending"
        results.append(_entry(current, outcome, reason=reason))
    counts = {}
    for item in results:
        counts[item["outcome"]] = counts.get(item["outcome"], 0) + 1
    return {
        "batch": "resume_all_submitted",
        "origin": actor["thread_id"],
        "project": project,
        "selected": len(selected),
        "counts": counts,
        "complete": all(
            item["outcome"] in {"closed", "closed_negative"} for item in results
        ),
        "tasks": results,
    }
