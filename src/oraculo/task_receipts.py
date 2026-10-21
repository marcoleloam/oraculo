"""Pure consumer contract: arrival order never selects the current task state."""

from __future__ import annotations


def consume(current: dict | None, event: dict) -> dict:
    snapshot = event.get("snapshot") or {}
    revision = snapshot.get("snapshot_revision")
    if type(revision) is not int or revision < 1:
        return {"snapshot": current, "historical": True, "refresh_required": True}
    if current and snapshot.get("id") != current.get("id"):
        raise ValueError("receipt_task_mismatch")
    current_revision = (current or {}).get("snapshot_revision", 0)
    return dict(
        snapshot=snapshot if revision > current_revision else current,
        historical=revision <= current_revision or event.get("historical", False),
        # This also handles cached pre-upgrade deliveries marked false.
        # An event's observation cannot certify freshness at receipt time.
        refresh_required=True,
    )
