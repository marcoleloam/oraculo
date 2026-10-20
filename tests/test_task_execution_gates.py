import pytest
from test_managed_tasks import publication_setup, report, start
from test_managed_tasks import system as system_fixture

system = system_fixture


@pytest.mark.parametrize("independent", [False, True])
@pytest.mark.parametrize("failure", ["failed", "cancelled", "interrupted"])
async def test_failed_descendant_must_not_certify_success(system, independent, failure):
    task = await start(system, independent=independent)
    child = task["child"]["childThreadId"]
    system.host.complete(child, pending=True)
    system.host.complete(child)
    system.host.threads["reviewer-" + child].update(status=failure, activeRunId=None)
    result = await system.engine.command(
        "finish", {"task_id": task["id"], "report": report(task)}
    )
    assert result["work_status"] != "succeeded", (
        "failed review must not certify task success"
    )
    assert result["memory"]["write_state"] != "saved"
    assert result["blocked_reason"] == "thread_children_pending_or_unknown"


@pytest.mark.parametrize("independent", [False, True])
@pytest.mark.parametrize(
    "case", ["active_followup", "pending_descendant", "later_failed_run"]
)
async def test_publication_must_recheck_execution(
    system, monkeypatch, independent, case
):
    original = system.host.call

    async def mode(name, args):
        result = await original(name, args)
        if name == "orchestrator_capabilities":
            result["features"]["appOwnedSubagents"] = not independent
        return result

    monkeypatch.setattr(system.host, "call", mode)
    task, spec, github = await publication_setup(system)
    child = task["child"]["childThreadId"]
    if case == "pending_descendant":
        system.host.complete(child, pending=True)
    elif case == "active_followup":
        system.host.threads[child].update(
            status="running", activeRunId="later", latestRunId="later"
        )
        for row in system.host.native_status.values():
            row["hasPendingChildRuns"] = True
    else:
        system.host.threads[child].update(
            status="failed", activeRunId=None, latestRunId="later-failed"
        )
        for row in system.host.native_status.values():
            row.update(
                latestTerminalRunId="later-failed", latestTerminalStatus="failed"
            )
    result = await system.engine.command(
        "publish", {"task_id": task["id"], "publication": spec}
    )
    assert github.created == 0, (
        "must not emit PR while execution or descendants are unresolved"
    )
    assert not result.get("publication_attempted")
    assert result["phase"] == "blocked"


async def test_lost_publication_reconciles_receipt_but_blocks_stale_success(system):
    task, spec, github = await publication_setup(system)
    github.lose_response = True
    github.visible = False
    first = await system.engine.command(
        "publish", {"task_id": task["id"], "publication": spec}
    )
    assert first["blocked_reason"] == "publication_uncertain"
    assert github.created == 1
    system.host.threads[task["child"]["childThreadId"]].update(
        status="failed", activeRunId=None, latestRunId="later-failed"
    )
    github.visible = True
    recovered = await system.engine.command(
        "publish", {"task_id": task["id"], "publication": spec}
    )
    assert recovered["publication"]["url"]
    assert recovered["phase"] == "blocked"
    assert recovered["blocked_reason"] == "thread_active_failed_or_pending_question"
    assert not recovered.get("publication_memory")
    assert github.created == 1
