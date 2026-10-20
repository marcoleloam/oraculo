import json

from test_plan_service import finish, grant, report, submit, task
from test_service import command
from test_settlement import Remote, binding_payload

from oraculo.cli import parser


async def test_public_protocol_plan_acceptance_and_restart(running_service):
    service = running_service
    plan = await submit(
        service,
        [
            task("implement"),
            task("validate", stage="validation", dependencies=["implement"]),
        ],
    )
    objective = plan["objective_id"]
    jobs = service.plans.get(objective)["job_ids"]
    implementation = await grant(service, jobs["implement"], "implementation")
    payload = binding_payload(objective, "implement")
    payload.update(
        thread_id=implementation["thread_id"], run_id=implementation["run_id"]
    )
    bound = await command(
        service,
        "settlement.bind",
        payload,
        origin={"thread_id": "coordinator", "run_id": "coordinator-run"},
        request_id="bind-delivery",
    )
    assert bound["ok"], bound
    binding_id = bound["data"]["id"]
    assert bound["state"] == "pending"
    remote = Remote()
    remote.snapshot["thread"].update(
        threadId=implementation["thread_id"], latestRunId=implementation["run_id"]
    )
    remote.snapshot["recentRuns"][0]["runId"] = implementation["run_id"]
    # Merged PRs cannot skip the remaining validation task.
    await report(service, implementation)
    await finish(service, implementation)
    await service.reconcile_settlements(remote)
    assert service.settlements.get(binding_id)["state"] == "blocked"
    assert not remote.mutations
    validation = await grant(service, jobs["validate"], "validation")
    await report(service, validation)
    await finish(service, validation)
    await service.reconcile_settlements(remote)
    assert service.settlements.get(binding_id)["state"] == "blocked"
    assert (
        service.settlements.get(binding_id)["last_error"] == "required_memory_not_saved"
    )
    assert not remote.mutations
    await service.memory_io("publish_pending")
    await service.reconcile_settlements(remote)
    assert service.settlements.get(binding_id)["state"] == "requested"
    assert remote.mutations == [implementation["thread_id"]]
    remote.snapshot["thread"]["settled"] = True
    await service.reconcile_settlements(remote)
    response = await command(service, "settlement.status", {"binding_id": binding_id})
    assert response["data"]["settlement"]["state"] == "confirmed"
    assert response["data"]["events"][-1]["evidence"]["delivery_stages"] == {
        "implementation": "succeeded",
        "validation": "succeeded",
        "review": "not_requested",
        "research": "not_requested",
        "memory": "not_requested",
        "publication": "not_requested",
        "deployment": "not_requested",
        "acceptance": "not_requested",
    }
    status = await command(service, "status.get", {})
    assert status["data"]["max_executors"] == 1
    assert status["data"]["settlements"][0]["state"] == "confirmed"
    # Replay cannot add a second intent, even after the original turn ended.
    replay = await command(
        service,
        "settlement.bind",
        payload,
        origin={"thread_id": "coordinator", "run_id": "coordinator-run"},
        request_id="bind-delivery",
    )
    assert replay == bound
    assert len(service.settlements.list()) == 1


async def test_foreign_origin_cannot_bind(running_service):
    service = running_service
    objective = service.state.submit_objective("alpha", "Scoped change")
    job = service.state.enqueue_job(objective["id"], "implementation", {})
    response = await command(
        service,
        "settlement.bind",
        binding_payload(objective["id"], job["id"]),
        origin={"thread_id": "unrelated", "run_id": "unrelated-run"},
    )
    assert response["ok"] is False
    assert not service.settlements.list()


async def test_service_scope_revocation_does_not_mutate_remote(running_service):
    service = running_service
    objective = service.state.submit_objective("alpha", "Scoped change")
    job = service.state.enqueue_job(objective["id"], "implementation", {})
    payload = binding_payload(objective["id"], job["id"])
    binding = service.settlements.bind("alpha", payload)
    service.state.register_project(
        "alpha", service.config.projects_root / "alpha", "unregistered"
    )
    remote = Remote()
    await service.reconcile_settlements(remote)
    assert (
        service.settlements.get(binding["id"])["last_error"]
        == "project_not_registered_or_outside_scope"
    )
    assert not remote.mutations
    assert remote.reads == 0


async def test_service_preserves_reopened_when_project_scope_changes(running_service):
    service = running_service
    objective = service.state.submit_objective("alpha", "Scoped change")
    job = service.state.enqueue_job(objective["id"], "implementation", {})
    binding = service.settlements.bind(
        "alpha", binding_payload(objective["id"], job["id"])
    )
    service.settlements.record(binding["id"], "reopened", error="new_run")
    service.state.register_project(
        "alpha", service.config.projects_root / "alpha", "unregistered"
    )
    remote = Remote()
    await service.reconcile_settlements(remote)
    assert service.settlements.get(binding["id"])["state"] == "reopened"
    assert remote.reads == 0


def test_cli_settlement_contract(tmp_path):
    payload_path = tmp_path / "binding.json"
    payload_path.write_text(json.dumps(binding_payload("objective", "job")))
    parsed = parser().parse_args(
        ["settlement", "bind", "alpha", "--file", str(payload_path)]
    )
    assert parsed.command == "settlement"
    assert parsed.action == "bind"
    assert parsed.file == payload_path
    parsed = parser().parse_args(
        ["settlement", "status", "alpha", "--binding-id", "binding"]
    )
    assert parsed.binding_id == "binding"
