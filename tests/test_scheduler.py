from oraculo.config import Config, ProviderConfig
from oraculo.routing import Router
from oraculo.scheduler import Scheduler
from oraculo.state import State


def test_priority_proactive_dedupe_and_dependency(tmp_path):
    state = State(tmp_path / "db")
    state.register_project("p", "/tmp/p")
    router = Router(
        Config(
            providers={
                "codex": ProviderConfig(
                    instance_id="codex", billing_verified=True, models=["m"]
                )
            }
        )
    )
    scheduler = Scheduler(state, router)
    auto = scheduler.propose_improvement("p", "bug", "a", "same\n evidence", "Fix bug")
    assert (
        scheduler.propose_improvement("p", "bug", "a", "same evidence", "Fix bug")["id"]
        == auto["id"]
    )
    human = state.submit_objective("p", "Now")
    job = state.enqueue_job(human["id"], "implementation", {})
    second = state.enqueue_job(human["id"], "tests", {}, [job["id"]])
    assert state.acquire_grant(second["id"], "codex", "m", "p", "r") is None
    grant = scheduler.admit_next(
        "p", "r", {"providers": [{"providerInstanceId": "codex", "models": ["m"]}]}
    )
    assert grant["job_id"] == job["id"]
    assert scheduler.schedule_discovery(now=3000) is None
