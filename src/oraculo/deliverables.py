"""Deliverable policy is independent of work/memory/publication receipts."""

from oraculo.state import StateError

KINDS = frozenset({"implementation", "plan", "validation", "research"})


def kind(task):
    return task.get("deliverable_kind") or task.get("request", {}).get(
        "deliverable_kind", "implementation"
    )


def require_implementation(task):
    selected = kind(task)
    if selected != "implementation":
        raise StateError(
            "plan_not_publishable"
            if selected == "plan"
            else "validation_not_publishable"
        )


def certified_phase(selected):
    return (
        "awaiting_approval"
        if selected == "plan"
        else "validation_ready"
        if selected in {"validation", "research"}
        else "pr_pending"
    )
