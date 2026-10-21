from test_managed_service import Caller
from test_managed_tasks import initialize, report

from oraculo.managed_service import command


async def test_managed_top_level_background_completion(running_service):
    service = running_service
    initialize(service.config.projects_root / "alpha")
    host = Caller(service.config.projects_root)
    task = await command(
        service.config,
        "start",
        dict(
            kind="work",
            key="review-liveness",
            text="Fixture",
            cwd=str(service.config.projects_root / "alpha"),
            independent=True,
        ),
        host,
    )
    assert task["transport_mode"] == "managed_thread", task
    host.caller = task["child"]["childThreadId"]
    submitted = await command(
        service.config, "finish", dict(task_id=task["id"], report=report(task)), host
    )
    assert submitted["phase"] == "validation_submitted", submitted
    host.complete(task["child"]["childThreadId"])
    host.caller = "origin"
    await service.reconcile_managed_tasks(host)
    if service.managed_flush:
        await service.managed_flush
    result = service.managed_service.store.get(task["id"])
    assert result["work_status"] == "succeeded"
