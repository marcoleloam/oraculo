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


def present(current: dict, event: dict, seen: set[str]) -> dict:
    """Call only after a fresh task status read. Never show frozen event status.

    Seen IDs are caller-owned persistent state. A newer event than that status
    requires another refresh and is not marked seen prematurely.
    """
    from oraculo.presentation import task_view

    consume(current, event)  # identity/shape checks shared with legacy consumers
    event_id = event.get("event_id")
    revision = event.get("snapshot_revision")
    snapshot = event.get("snapshot") or {}
    if event.get("task_id", current.get("id")) != current.get("id"):
        raise ValueError("receipt_task_mismatch")
    if (
        not event_id
        or type(revision) is not int
        or type(snapshot.get("snapshot_revision")) is not int
        or snapshot.get("snapshot_revision") != revision
        or snapshot.get("id") != current.get("id")
        or revision > current.get("snapshot_revision", 0)
    ):
        return {"presentation": None, "refresh_required": True}
    if (
        event_id in seen
        or revision < current.get("snapshot_revision", 0)
        or event.get("historical")
    ):
        seen.add(event_id)
        return {"presentation": None, "refresh_required": False}
    seen.add(event_id)
    return {"presentation": task_view(current), "refresh_required": False}
