"""Evidence for a bounded, complete execution history; never rebind the child."""

import math
import time
from datetime import datetime

from oraculo.protocol import content_hash
from oraculo.state import StateError
from oraculo.t3.contracts import TERMINAL_STATUSES


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError
        result = parsed.timestamp()
        if not math.isfinite(result):
            raise ValueError
        return result
    except (AttributeError, TypeError, ValueError, OverflowError):
        raise StateError("run_timestamp_unproven") from None


def terminal_chain(snapshot):
    """runCount and contiguous ordinals prove completeness, not timeline hasMore.

    T3's timeline pagination is independent of recentRuns. The host currently has
    no run-history cursor: a truncated run window must fail closed.
    """
    thread, runs = snapshot["thread"], snapshot["recentRuns"]
    count = thread.get("runCount")
    if (
        type(count) is not int
        or count < 1
        or count != len(runs)
        or any(type(r.get("ordinal")) is not int for r in runs)
        or sorted(r["ordinal"] for r in runs) != list(range(1, count + 1))
        or len({r["runId"] for r in runs}) != count
        or any(r["status"] not in TERMINAL_STATUSES for r in runs)
    ):
        raise StateError("terminal_run_chain_incomplete")
    chain = sorted(runs, key=lambda r: r["ordinal"])
    if (
        chain[-1]["runId"] != thread["latestRunId"]
        or chain[-1]["status"] != thread["status"]
    ):
        raise StateError("terminal_run_chain_incoherent")
    # Single-run legacy fixtures/hosts need no chronological continuation proof.
    if count > 1:
        prior_end = None
        for run in chain:
            start, end = (
                timestamp(run.get("startedAt")),
                timestamp(run.get("completedAt")),
            )
            if (
                end < start
                or end > time.time()
                or (prior_end is not None and start < prior_end)
            ):
                raise StateError("terminal_run_chain_incoherent")
            prior_end = end
    return chain


def report_link(task, snapshot, report, *, actor=None):
    """Use immutable submission evidence, never refresh an old report's date."""
    chain = terminal_chain(snapshot)
    original = task["child"]["childRunId"]
    if chain[0]["runId"] != original:
        raise StateError("original_task_run_not_observed")
    latest = chain[-1]
    digest = content_hash(report)
    existing = task.get("completion_evidence")
    if existing:
        if (
            existing["report_sha256"] != digest
            or existing["latest_run_id"] != latest["runId"]
            or existing["runs"] != chain
        ):
            raise StateError("execution_changed_after_validation")
        return existing
    recovered = task.get("report_recovery")
    submitted_digest = recovered["original_sha256"] if recovered else digest
    events = [
        e
        for e in task["events"]
        if e["phase"] == "validation_submitted"
        and e["proof"].get("report_sha256") == submitted_digest
    ]
    event = events[0] if events else None
    if latest["runId"] != original:
        if task["transport_mode"] != "native_child":
            raise StateError("continuation_requires_native_child")
        if event is None:
            if task.get("submitted_report") or task.get("report") or not actor:
                raise StateError("latest_run_report_unproven")
        else:
            at = event.get("at")
            if (
                type(at) not in (float, int)
                or not math.isfinite(at)
                or at > time.time()
                or at < timestamp(latest.get("startedAt"))
            ):
                raise StateError("latest_run_report_unproven")
            binding = event["proof"].get("submission")
            if binding and (
                binding.get("thread_id") != task["child"]["childThreadId"]
                or binding.get("run_id") != latest["runId"]
            ):
                raise StateError("latest_run_report_unproven")
    return dict(
        original_run_id=original,
        latest_run_id=latest["runId"],
        thread_id=task["child"]["childThreadId"],
        runs=chain,
        report_sha256=digest,
        submission_event_id=event["id"] if event else None,
        submission_actor=event["proof"].get("submission") if event else actor,
        legacy_submission=bool(event and not event["proof"].get("submission")),
    )
