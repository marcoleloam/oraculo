"""The concrete next command for a blocked or interrupted managed task.

Pure text: nothing here reads the host or the ledger. The command always names
the same task; a blocked task is never a reason to start a new one.
"""

from __future__ import annotations

# Actions whose replay needs the caller's original payload, not just the id.
_SAME_PAYLOAD = {
    "finish": "the same report",
    "publish": "the same publication",
    "continue": "the same key and text",
    "approve": "the same commit and evidence",
    "recover-report": "the same payload",
    "recover-delivery": "the same payload",
    "adjudicate-review": "the same payload",
    "complete-origin": "the same payload",
    "supersede": "the same payload",
    "pr-check": "the same payload",
    "merge": "the same payload",
    "settle": "the same payload",
    "unwatch": "the same payload",
    "deployment": "the same deployment evidence",
}
_WAIT = "Wait until the child run and every descendant have ended."
_REASONS = {
    "children_still_running": ("resume", _WAIT),
    "terminal_execution_or_descendants_unproven": ("resume", _WAIT),
    "thread_children_pending_or_unknown": ("resume", _WAIT),
    "child_workspace_not_ready": (
        "resume",
        "Wait until the host has bound the child checkout.",
    ),
    "terminal_descendant_review_required": (
        "diagnose-review",
        "A descendant ended without success. Inspect it, then use "
        "adjudicate-review or legacy delivery recovery.",
    ),
    "terminal_task_report_missing": (
        "finish",
        "The child ended without a report. Finish with the structured report.",
    ),
    "memory_write_unconfirmed": (
        "resume",
        "The memory write was not confirmed. Resume saves the same cycle.",
    ),
    "managed_cancel_candidate_child_exists_resume": (
        "resume",
        "A child may already exist for this launch. Resume binds it.",
    ),
    "validated_commit_or_clean_workspace_required": (
        "finish",
        "Commit or clean the child checkout, then report its full HEAD SHA.",
    ),
    "managed_task_cancelled": ("status", "The task was cancelled. Nothing to retry."),
}


def recovery(action: str | None, task_id: str | None, reason: str | None) -> dict:
    """What to run next for a blocked response."""
    next_action, why = _REASONS.get(reason or "", (None, None))
    if next_action is None:
        # start is replayed as resume: the task and its launch intent exist.
        next_action = "resume" if action in {None, "start", "status"} else action
        why = "Fix the blocker named in blocked_reason, then run the command."
    payload = {"task_id": task_id} if task_id else {}
    same = _SAME_PAYLOAD.get(next_action)
    return {
        "command": "oraculo task " + next_action,
        "payload": payload,
        "instruction": why
        + (f" Send the same task_id with {same}." if same else " Send this task_id.")
        + " Do not start a new task.",
    }


def repeat(action: str, payload: dict, cause: str) -> dict:
    """An operation interrupted before any host effect: repeat it unchanged."""
    keys = sorted(k for k in ("task_id", "key") if isinstance(payload.get(k), str))
    result = {
        "command": "oraculo task " + action,
        "payload": {k: payload[k] for k in keys},
        "instruction": "Repeat the same command with the same payload ("
        + (", ".join(keys + ["report"] if "report" in payload else keys) or "unchanged")
        + "). The host channel failed while only reads had been issued, so "
        "the task kept its previous phase and no host effect is pending.",
        "cause": cause,
        "task_state": "unchanged",
    }
    return result
