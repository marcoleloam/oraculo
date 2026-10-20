"""Closure gates with real isolated memory, fake T3 and a deterministic PR port."""

import copy

from test_managed_tasks import publication_setup
from test_managed_tasks import system as system_fixture

system = system_fixture


class Lifecycle:
    def __init__(self):
        self.ready = True
        self.blockers = []
        self.merged = False
        self.calls = 0
        self.lost = False

    def inspect(self, pr, policy, cwd):
        return {
            **pr,
            "ready": self.ready,
            "blockers": self.blockers,
            "state": "merged" if self.merged else "open",
            "merge_sha": "b" * 40 if self.merged else None,
        }

    def merge(self, pr, policy, cwd, *, before_merge=None, reconcile_only=False):
        if not self.merged:
            assert not reconcile_only
            before_merge()
            self.calls += 1
            self.merged = True
            if self.lost:
                raise TimeoutError("response lost")
        return self.inspect(pr, policy, cwd)


async def setup(system):
    task, spec, _ = await publication_setup(system)
    result = await system.engine.command(
        "publish", {"task_id": task["id"], "publication": spec}
    )
    assert result["phase"] == "pr_open"
    lifecycle = Lifecycle()
    system.engine.lifecycle = lifecycle
    original = system.host.call
    counts = {"watch": 0, "unwatch": 0, "settle": 0}

    async def call(name, args):
        if name in {"watch_pull_request", "unwatch_pull_request"}:
            counts["watch" if name == "watch_pull_request" else "unwatch"] += 1
            return {"url": args["url"], "watching": name == "watch_pull_request"}
        if name == "t3_thread_organize":
            counts["settle"] += 1
            system.host.threads[args["threadId"]]["settled"] = True
            return {"accepted": True}
        result = await original(name, args)
        if name == "t3_thread_read":
            result["thread"].update(
                pendingRequestCount=0,
                archived=False,
                itemCount=2,
                updatedAt="2026-01-01T00:00:00Z",
            )
            result["thread"].setdefault("settled", False)
            result.update(items=[], hasMore=False, nextPosition=None)
        return result

    system.host.call = call
    policy = {
        "repository": spec["repository"],
        "base": spec["base"],
        "head_sha": spec["head_sha"],
        "merge_authorized": True,
        "required_checks": ["unit"],
        "required_review": True,
        "method": "squash",
        "policy_source": "project rules plus explicit origin authorization",
    }
    return task, policy, lifecycle, counts


async def test_required_checks_wait_uses_host_watch_not_merge(system):
    task, policy, lifecycle, calls = await setup(system)
    lifecycle.ready = False
    lifecycle.blockers = ["checks_pending"]
    result = await system.engine.command(
        "merge", {"task_id": task["id"], "policy": policy}
    )
    assert result["phase"] == "pr_waiting"
    assert calls["watch"] == 1
    assert lifecycle.calls == 0
    assert not result["merged"] and not result["settled"]


async def test_merge_receipt_final_memory_and_observed_settlement(system):
    task, policy, lifecycle, calls = await setup(system)
    result = await system.engine.command(
        "merge", {"task_id": task["id"], "policy": policy}
    )
    assert result["phase"] == "settled", result
    assert result["merged"] and result["settled"]
    assert result["final_memory"]["write_state"] == "saved"
    assert result["final_memory"]["write_receipt"]["commit"]
    assert calls == {"watch": 1, "unwatch": 1, "settle": 1}
    assert lifecycle.calls == 1
    # Neither daemon nor an ordinary resume should become a PR polling loop.
    before = copy.deepcopy(system.host.calls)
    await system.engine.reconcile(task["id"])
    assert system.host.calls == before


async def test_lost_merge_response_observes_without_second_merge(system):
    task, policy, lifecycle, calls = await setup(system)
    lifecycle.lost = True
    lost = await system.engine.command(
        "merge", {"task_id": task["id"], "policy": policy}
    )
    assert lost["phase"] == "blocked" and lost["merge_attempted"]
    result = await system.engine.command("merge", {"task_id": task["id"]})
    assert result["phase"] == "settled", result
    assert lifecycle.calls == 1 and calls["settle"] == 1


async def test_merge_policy_requires_explicit_origin_and_policy_source(system):
    task, policy, _, _ = await setup(system)
    policy.pop("policy_source")
    result = await system.engine.command(
        "merge", {"task_id": task["id"], "policy": policy}
    )
    assert result["blocked_reason"] == "project_merge_policy_source_required"
    system.host.caller = task["child"]["childThreadId"]
    system.host.threads[system.host.caller]["activeRunId"] = "followup"
    policy["policy_source"] = "rules"
    result = await system.engine.command(
        "merge", {"task_id": task["id"], "policy": policy}
    )
    assert result["blocked_reason"] == "merge_policy_requires_origin"


async def test_new_head_requires_new_validation(system):
    task, policy, lifecycle, _ = await setup(system)
    policy["head_sha"] = "c" * 40
    result = await system.engine.command(
        "merge", {"task_id": task["id"], "policy": policy}
    )
    assert (
        result["blocked_reason"] == "new_head_requires_new_validation_and_publication"
    )
    assert lifecycle.calls == 0


async def test_pending_nested_work_blocks_merge(system):
    task, policy, lifecycle, _ = await setup(system)
    system.host.complete(task["child"]["childThreadId"], pending=True)
    result = await system.engine.command(
        "merge", {"task_id": task["id"], "policy": policy}
    )
    assert result["blocked_reason"] == "task_work_pending_before_merge"
    assert lifecycle.calls == 0


async def test_final_memory_failure_preserves_merge_and_retry_settles(
    system, monkeypatch
):
    task, policy, lifecycle, calls = await setup(system)
    original = system.service.publisher.publish
    monkeypatch.setattr(
        system.service.publisher,
        "publish",
        lambda *a, **k: (_ for _ in ()).throw(OSError("unavailable")),
    )
    result = await system.engine.command(
        "merge", {"task_id": task["id"], "policy": policy}
    )
    assert result["merged"] and not result["settled"]
    assert result["blocked_reason"] == "final_memory_write_unconfirmed"
    assert calls["settle"] == 0
    monkeypatch.setattr(system.service.publisher, "publish", original)
    result = await system.engine.command("settle", {"task_id": task["id"]})
    assert result["phase"] == "settled"
    assert lifecycle.calls == 1


async def test_lost_settlement_ack_is_observed_without_second_mutation(system):
    task, policy, _, counts = await setup(system)
    original = system.host.call

    async def lost(name, args):
        result = await original(name, args)
        if name == "t3_thread_organize":
            raise TimeoutError("settlement acknowledgement lost")
        return result

    system.host.call = lost
    result = await system.engine.command(
        "merge", {"task_id": task["id"], "policy": policy}
    )
    assert result["merged"] and result["settlement_attempted"] and not result["settled"]
    result = await system.engine.command("settle", {"task_id": task["id"]})
    assert result["settled"] and counts["settle"] == 1


async def test_settlement_rejects_later_thread_run_even_after_verified_merge(system):
    task, policy, lifecycle, counts = await setup(system)
    original = system.host.call

    async def changed(name, args):
        result = await original(name, args)
        if (
            lifecycle.merged
            and name == "t3_thread_read"
            and args["threadId"] == task["child"]["childThreadId"]
        ):
            result["thread"]["latestRunId"] = "later-run"
        return result

    system.host.call = changed
    result = await system.engine.command(
        "merge", {"task_id": task["id"], "policy": policy}
    )
    assert result["merged"] and not result["settled"]
    assert result["blocked_reason"] == "thread_changed_since_authorization"
    assert counts["settle"] == 0


async def test_check_observes_external_merge_without_emitting_merge(system):
    task, policy, lifecycle, counts = await setup(system)
    lifecycle.merged = True
    lifecycle.ready = False
    result = await system.engine.command(
        "pr-check", {"task_id": task["id"], "policy": policy}
    )
    assert result["merged"] and result["settled"]
    assert lifecycle.calls == 0
    assert counts["watch"] == 0


async def test_merge_rechecks_publication_allowlist(system):
    task, policy, lifecycle, _ = await setup(system)
    system.service.config.allowed_projects = [
        system.service.config.projects_root / "alpha"
    ]
    result = await system.engine.command(
        "merge", {"task_id": task["id"], "policy": policy}
    )
    assert result["blocked_reason"] == "publication_binding_outside_scope"
    assert lifecycle.calls == 0
