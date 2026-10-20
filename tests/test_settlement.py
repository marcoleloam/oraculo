import copy

import pytest

from oraculo.plans import Plans
from oraculo.settlement import Settlements
from oraculo.settlement_store import SettlementStore
from oraculo.state import State
from oraculo.t3.client import MutationUncertain
from oraculo.t3.contracts import ContractError


def binding_payload(objective, job):
    return {
        "dedupe_key": "delivery-closure",
        "objective_id": objective,
        "task_id": job,
        "thread_id": "task-thread",
        "run_id": "task-run",
        "policy": "all_merged",
        "pull_requests": [
            {
                "host": "github",
                "repository": "example/project",
                "number": 12,
                "base_branch": "develop",
            }
        ],
        "authorization": {
            "actor": "operator",
            "reason": "Close this dedicated delivery after the complete contract",
            "scope": "dedicated_task",
            "covers_all_objectives": True,
        },
    }


class Remote:
    def __init__(self):
        self.snapshot = {
            "thread": {
                "threadId": "task-thread",
                "projectId": "alpha",
                "status": "completed",
                "activeRunId": None,
                "latestRunId": "task-run",
                "pendingRequestCount": 0,
                "archived": False,
                "settled": False,
                "updatedAt": "revision-1",
                "itemCount": 8,
            },
            "recentRuns": [{"runId": "task-run", "status": "completed"}],
            "childrenObserved": True,
            "hasPendingChildRuns": False,
        }
        self.prs = {
            "pullRequests": [
                {
                    "host": "github",
                    "repository": "example/project",
                    "number": 12,
                    "baseBranch": "develop",
                    "isDraft": False,
                    "state": "merged",
                }
            ]
        }
        self.mutations = []
        self.reads = 0
        self.error = None
        self.on_second_read = None

    async def settlement_snapshot(self, thread):
        self.reads += 1
        if self.reads == 2 and self.on_second_read:
            self.on_second_read(self.snapshot)
        return copy.deepcopy(self.snapshot)

    async def list_thread_pull_requests(self, thread):
        return copy.deepcopy(self.prs)

    async def settle_thread(self, thread):
        self.mutations.append(thread)
        if self.error:
            raise self.error
        return {"settlesWhenTurnEnds": True}

    def validate_settle(self, thread):
        pass


@pytest.fixture
def system(tmp_path):
    state = State(tmp_path / "state.db")
    state.register_project("alpha", tmp_path / "alpha")
    state.set_coordinator("alpha", "permanent")
    objective = state.submit_objective("alpha", "Deliver bounded change")
    job = state.enqueue_job(objective["id"], "implementation", {})
    state.connection.execute("UPDATE jobs SET state='succeeded'")
    state.connection.execute("UPDATE objectives SET status='succeeded'")
    store = SettlementStore(state)
    binding = store.bind("alpha", binding_payload(objective["id"], job["id"]))
    engine = Settlements(state, store, Plans(state))
    yield engine, binding, Remote()
    state.close()


async def test_deferred_response_requires_later_read_and_never_replays(system):
    engine, binding, remote = system
    result = await engine.reconcile(remote, binding["id"])
    assert result["state"] == "requested"
    assert result["evidence"]["request_response"]["settlesWhenTurnEnds"]
    result = await engine.reconcile(remote, binding["id"])
    assert result["state"] == "uncertain"
    remote.snapshot["thread"]["settled"] = True
    result = await engine.reconcile(remote, binding["id"])
    assert result["state"] == "confirmed"
    assert remote.mutations == ["task-thread"]
    assert [e["state"] for e in engine.store.events(binding["id"])] == [
        "pending",
        "requested",
        "requested",
        "uncertain",
        "confirmed",
    ]


async def test_existing_t3_auto_settlement_is_observed_without_mutation(system):
    engine, binding, remote = system
    remote.snapshot["thread"]["settled"] = True
    result = await engine.reconcile(remote, binding["id"])
    assert result["state"] == "confirmed"
    assert result["evidence"]["closure_source"] == "observed_existing_t3_settlement"
    assert not remote.mutations


@pytest.mark.parametrize("pr_state", ["open", "closed", "failed", "abandoned", None])
async def test_only_verified_merged_host_state_is_eligible(system, pr_state):
    engine, binding, remote = system
    remote.prs["pullRequests"][0]["state"] = pr_state
    assert (await engine.reconcile(remote, binding["id"]))["state"] == "blocked"
    assert not remote.mutations


@pytest.mark.parametrize(
    "change",
    [
        {"baseBranch": "main"},
        {"isDraft": True},
        {"isDraft": None},
        {"host": "other-host"},
        {"repository": "other/project"},
        {"number": 13},
    ],
)
async def test_exact_host_repository_target_and_nondraft_required(system, change):
    engine, binding, remote = system
    remote.prs["pullRequests"][0].update(change)
    assert (await engine.reconcile(remote, binding["id"]))["state"] == "blocked"
    assert not remote.mutations


@pytest.mark.parametrize("mode", ["extra", "missing", "duplicate"])
async def test_complete_linked_set_required(system, mode):
    engine, binding, remote = system
    rows = remote.prs["pullRequests"]
    if mode == "missing":
        rows.clear()
    else:
        rows.append(dict(rows[0], number=13 if mode == "extra" else 12))
    assert (await engine.reconcile(remote, binding["id"]))["state"] == "blocked"
    assert not remote.mutations


@pytest.mark.parametrize(
    "change",
    [
        {"activeRunId": "active"},
        {"pendingRequestCount": 1},
        {"status": "failed"},
        {"status": "interrupted"},
        {"status": "queued"},
        {"archived": True},
        {"projectId": "other"},
        {"threadId": "wrong"},
        {"settled": None},
        {"updatedAt": None},
        {"itemCount": None},
    ],
)
async def test_thread_guards(system, change):
    engine, binding, remote = system
    remote.snapshot["thread"].update(change)
    assert (await engine.reconcile(remote, binding["id"]))["state"] == "blocked"
    assert not remote.mutations


@pytest.mark.parametrize(
    "change",
    [
        {"hasPendingChildRuns": True},
        {"childrenObserved": False},
        {"recentRuns": []},
        {"recentRuns": [{"runId": "task-run", "status": "failed"}]},
    ],
)
async def test_run_and_descendants_must_be_observed(system, change):
    engine, binding, remote = system
    remote.snapshot.update(change)
    assert (await engine.reconcile(remote, binding["id"]))["state"] == "blocked"
    assert not remote.mutations


async def test_new_message_during_check_blocks_mutation(system):
    engine, binding, remote = system
    remote.on_second_read = lambda snap: snap["thread"].update(
        updatedAt="revision-2", itemCount=9
    )
    result = await engine.reconcile(remote, binding["id"])
    assert result["state"] == "blocked"
    assert result["last_error"] == "thread_changed_during_settlement_check"
    assert not remote.mutations


async def test_reopened_after_confirmation_never_loops(system):
    engine, binding, remote = system
    remote.snapshot["thread"]["settled"] = True
    await engine.reconcile(remote, binding["id"])
    remote.snapshot["thread"]["settled"] = False
    result = await engine.reconcile(remote, binding["id"])
    assert result["state"] == "reopened"
    remote.snapshot["thread"]["settled"] = True
    assert (await engine.reconcile(remote, binding["id"]))["state"] == "reopened"
    assert not remote.mutations


async def test_new_run_invalidates_old_authorization(system):
    engine, binding, remote = system
    remote.snapshot["thread"]["latestRunId"] = "another-run"
    assert (await engine.reconcile(remote, binding["id"]))["state"] == "reopened"
    assert not remote.mutations


async def test_lost_mutation_response_then_restart_reconciles_without_repeat(system):
    engine, binding, remote = system
    remote.error = MutationUncertain("response lost")
    result = await engine.reconcile(remote, binding["id"])
    assert result["state"] == "uncertain"
    reopened = State(engine.state.path)
    restarted = Settlements(reopened, SettlementStore(reopened), Plans(reopened))
    try:
        result = await restarted.reconcile(remote, binding["id"])
        assert result["state"] == "uncertain"
        remote.snapshot["thread"]["settled"] = True
        assert (await restarted.reconcile(remote, binding["id"]))[
            "state"
        ] == "confirmed"
        assert remote.mutations == ["task-thread"]
    finally:
        reopened.close()


async def test_crash_after_intent_before_effect_does_not_duplicate(system):
    engine, binding, remote = system
    assert engine.store.claim_request(binding["id"], {"before_effect": True})
    assert (await engine.reconcile(remote, binding["id"]))["state"] == "uncertain"
    assert not remote.mutations


async def test_missing_mutation_capability_does_not_consume_request_claim(system):
    engine, binding, remote = system

    def unavailable(_):
        raise ContractError("Unavailable T3 tool: t3_thread_organize")

    remote.validate_settle = unavailable
    result = await engine.reconcile(remote, binding["id"])
    assert result["state"] == "blocked"
    assert result["request_claimed"] is False
    assert not remote.mutations
    remote.validate_settle = lambda _: None
    assert (await engine.reconcile(remote, binding["id"]))["state"] == "requested"


async def test_reopened_history_is_final_even_if_later_status_changes(system):
    engine, binding, remote = system
    remote.snapshot["thread"]["latestRunId"] = "new-run"
    assert (await engine.reconcile(remote, binding["id"]))["state"] == "reopened"
    engine.store.record(binding["id"], "blocked", error="scope_unavailable")
    remote.snapshot["thread"]["latestRunId"] = "task-run"
    assert (await engine.reconcile(remote, binding["id"]))["state"] == "blocked"
    assert not remote.mutations


async def test_missing_t3_capability_blocks_only_settlement(system):
    engine, binding, remote = system

    async def missing(_):
        raise ContractError("Unavailable T3 tool: list_thread_pull_requests")

    remote.list_thread_pull_requests = missing
    result = await engine.reconcile(remote, binding["id"])
    assert result["state"] == "blocked"
    assert "Unavailable T3 tool" in result["last_error"]
    assert not remote.mutations


@pytest.mark.parametrize("status", ["queued", "running", "failed", "stopped"])
async def test_merged_is_not_objective_success(system, status):
    engine, binding, remote = system
    engine.state.connection.execute("UPDATE objectives SET status=?", (status,))
    assert (await engine.reconcile(remote, binding["id"]))["state"] == "blocked"
    assert not remote.mutations


async def test_pending_job_blocks_even_if_objective_was_succeeded(system):
    engine, binding, remote = system
    engine.state.enqueue_job(binding["objective_id"], "tests", {})
    assert (await engine.reconcile(remote, binding["id"]))["state"] == "blocked"
    assert not remote.mutations


async def test_coordinator_assigned_after_bind_is_never_settled(system):
    engine, binding, remote = system
    engine.state.set_coordinator("alpha", "task-thread")
    assert (await engine.reconcile(remote, binding["id"]))["state"] == "blocked"
    assert not remote.mutations


async def test_t3_settled_with_unsatisfied_contract_does_not_mean_acceptance(system):
    engine, binding, remote = system
    engine.state.enqueue_job(binding["objective_id"], "tests", {})
    remote.snapshot["thread"]["settled"] = True
    result = await engine.reconcile(remote, binding["id"])
    assert result["state"] == "blocked"
    assert result["evidence"]["thread"]["settled"] is True
    assert not remote.mutations


async def test_observed_confirmation_survives_read_failure_without_resettling(system):
    engine, binding, remote = system
    remote.snapshot["thread"]["settled"] = True
    await engine.reconcile(remote, binding["id"])
    saved = remote.settlement_snapshot

    async def unavailable(_):
        raise ContractError("read unavailable")

    remote.settlement_snapshot = unavailable
    result = await engine.reconcile(remote, binding["id"])
    assert result["state"] == "blocked"
    remote.settlement_snapshot = saved
    remote.snapshot["thread"]["settled"] = False
    assert (await engine.reconcile(remote, binding["id"]))["state"] == "reopened"
    assert not remote.mutations


async def test_stack_requires_every_bound_pr_to_merge(tmp_path):
    state = State(tmp_path / "stack.db")
    try:
        state.register_project("alpha", tmp_path / "alpha")
        objective = state.submit_objective("alpha", "Complete stack")
        job = state.enqueue_job(objective["id"], "implementation", {})
        state.connection.execute("UPDATE jobs SET state='succeeded'")
        state.connection.execute("UPDATE objectives SET status='succeeded'")
        payload = binding_payload(objective["id"], job["id"])
        payload["pull_requests"].append(dict(payload["pull_requests"][0], number=13))
        store = SettlementStore(state)
        binding = store.bind("alpha", payload)
        engine = Settlements(state, store, Plans(state))
        remote = Remote()
        remote.prs["pullRequests"].append(
            dict(remote.prs["pullRequests"][0], number=13, state="open")
        )
        assert (await engine.reconcile(remote, binding["id"]))["state"] == "blocked"
        assert not remote.mutations
        remote.prs["pullRequests"][1]["state"] = "merged"
        assert (await engine.reconcile(remote, binding["id"]))["state"] == "requested"
    finally:
        state.close()
