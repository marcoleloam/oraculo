import pytest
from test_managed_tasks import report, start
from test_managed_tasks import system as system_fixture

from oraculo.presentation import task_view

system = system_fixture


@pytest.mark.parametrize("kind", ["validation", "research"])
async def test_validation_research_end_reviewable_with_work_and_memory_without_pr(
    system, kind
):
    task = await start(system, deliverable_kind=kind)
    child = task["child"]["childThreadId"]
    system.host.complete(child)
    data = report(task) | {"deliverable_kind": kind}
    result = await system.engine.command(
        "finish", {"task_id": task["id"], "report": data}
    )
    assert result["phase"] == "validation_ready", result
    assert (
        result["work_status"] == "succeeded"
        and result["memory"]["write_state"] == "saved"
    )
    assert result["memory"]["write_receipt"]["state"] == "confirmed"
    assert (
        result["publication"] is None and not result["merged"] and not result["settled"]
    )
    view = task_view(system.store.get(task["id"]))
    assert view["phase"] == "Validação entregue"
    assert "revisão" in view["result"] and "publicação" in view["result"]
    for action, extra in [
        ("publish", {"publication": {}}),
        ("pr-check", {}),
        ("merge", {}),
        ("settle", {}),
        ("unwatch", {}),
        ("deployment", {"deployment": {"state": "confirmed"}}),
    ]:
        response = await system.engine.command(action, {"task_id": task["id"]} | extra)
        assert response["blocked_reason"] == "validation_not_publishable", (
            action,
            response,
        )
    assert not any(
        name in {"link_pull_request", "watch_pull_request"}
        for name, _ in system.host.calls
    )


@pytest.mark.parametrize(
    "kind,phase", [("plan", "awaiting_approval"), ("implementation", "pr_pending")]
)
async def test_existing_deliverables_keep_their_phase(system, kind, phase):
    task = await start(system, deliverable_kind=kind)
    system.host.complete(task["child"]["childThreadId"])
    result = await system.engine.command(
        "finish",
        {"task_id": task["id"], "report": report(task) | {"deliverable_kind": kind}},
    )
    assert result["phase"] == phase, result


async def test_deliverable_cannot_be_reclassified_at_finish(system):
    task = await start(system, deliverable_kind="validation")
    system.host.complete(task["child"]["childThreadId"])
    response = await system.engine.command(
        "finish",
        {
            "task_id": task["id"],
            "report": report(task) | {"deliverable_kind": "implementation"},
        },
    )
    assert response["blocked_reason"] == "deliverable_kind_conflict"
