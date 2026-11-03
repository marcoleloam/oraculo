import subprocess

from test_ci_events import binding, event, observer
from test_decisions import Transport, config
from test_service import command

from oraculo.decisions import Decisions


async def test_offline_shadow_keeps_effective_router_and_grants_unchanged(tmp_path):
    import importlib.util
    from pathlib import Path

    p = Path(__file__).parents[1] / "scripts/decisions_eval.py"
    spec = importlib.util.spec_from_file_location("shadow_eval", p)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    r = await module.evaluate(tmp_path)
    assert r["simulated_sends"] == 7 and r["replay_sends"] == 0 and r["api_calls"] == 0
    assert r["route_changes"] == r["state_mutations"] == 0
    assert all(row["unchanged"] and not row["fallback_executed"] for row in r["rows"])
    assert not r["production_shadow"] and not r["calibration_certified"]


async def setup(service):
    root = service.config.projects_root / "alpha"
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=synthetic@example.invalid",
            "-c",
            "user.name=Synthetic",
            "commit",
            "--allow-empty",
            "-qm",
            "synthetic",
        ],
        cwd=root,
        check=True,
    )
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    objective = service.state.submit_objective("alpha", "shadow")
    b = binding(head=head)
    j = service.state.enqueue_job(
        objective["id"], "implementation", {"ci_binding": b.model_dump()}
    )
    service.ci_observer = observer(event(head=head))
    r = await command(
        service,
        "ci.observe",
        {"job_id": j["id"]},
        origin={"thread_id": "coordinator", "run_id": "run"},
    )
    assert r["ok"]
    return j


async def test_service_computes_snapshot_and_records_nonbinding_advice(running_service):
    service = running_service
    j = await setup(service)
    t = Transport()
    service.config.decisions = config()
    service.decisions = Decisions(service.state, service.config.decisions, transport=t)
    origin = {"thread_id": "coordinator", "run_id": "run"}
    rejected = await command(
        service,
        "decisions.shadow",
        {"job_id": j["id"], "snapshot": {"head": "forged"}},
        origin=origin,
    )
    assert not rejected["ok"] and t.calls == []
    first = await command(
        service, "decisions.shadow", {"job_id": j["id"]}, origin=origin
    )
    assert first["ok"], first
    r = first["data"]
    assert r["recommendation"]["state"] == "answered" and not r["applied"]
    assert r["baseline"]["profile"] == r["recommended_profile"] == "implementation"
    assert (
        service.state.leases() == []
        and service.state.get_job(j["id"])["state"] == "queued"
    )
    second = await command(
        service, "decisions.shadow", {"job_id": j["id"]}, origin=origin
    )
    assert second["data"]["recommendation"]["replay"] and len(t.calls) == 1


async def test_service_state_mutated_during_advice_is_not_consumed(running_service):
    service = running_service
    j = await setup(service)

    def changed():
        service.state.cancel_objective(j["objective_id"])

    t = Transport(effect=changed)
    service.config.decisions = config()
    service.decisions = Decisions(service.state, service.config.decisions, transport=t)
    r = await command(
        service,
        "decisions.shadow",
        {"job_id": j["id"]},
        origin={"thread_id": "coordinator", "run_id": "run"},
    )
    assert r["ok"] and r["data"]["recommendation"]["state"] == "unknown", r
    assert r["data"]["recommended_profile"] is None


async def test_service_kill_switch_requires_local_operator_and_persists(
    running_service,
):
    service = running_service
    denied = await command(
        service,
        "decisions.kill",
        {"enabled": True},
        origin={"thread_id": "coordinator", "run_id": "run"},
    )
    assert not denied["ok"]
    accepted = await command(service, "decisions.kill", {"enabled": True})
    assert accepted["ok"] and accepted["data"]["kill"] is True
    assert service.state.get_meta("decisions:v1:kill") is True
