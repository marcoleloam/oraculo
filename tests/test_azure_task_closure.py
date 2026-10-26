"""Managed Azure flow: real isolated memory/service/Git, synthetic host REST/T3."""

import copy

import pytest
from test_azure_repos import REMOTE, AzureHost, policy
from test_managed_tasks import git, report, start
from test_managed_tasks import system as system_fixture

from oraculo.azure_repos import AzureRepos
from oraculo.pr_lifecycle import RepositoryPRLifecycle
from oraculo.publication import RepositoryPublisher

system = system_fixture


async def integrated_setup(system, count=1, status="completed"):
    root = system.service.config.projects_root / "alpha"
    tasks, commits = [], []
    for index in range(count):
        git(
            root,
            "-c",
            "user.name=Example",
            "-c",
            "user.email=example@example.org",
            "commit",
            "--allow-empty",
            "-m",
            f"Part {index}",
        )
        task = await start(system, key=f"part-{index}")
        assert task["phase"] == "executing", task
        system.host.complete(task["child"]["childThreadId"])
        result = await system.engine.command(
            "finish", {"task_id": task["id"], "report": report(task)}
        )
        assert result["work_status"] == "succeeded", result
        result = system.store.get(task["id"])
        tasks.append(result)
        commits.append(result["report"]["commit"])
    public = root.parent / "integration"
    git(root, "worktree", "add", "-b", "task/integration", str(public), "HEAD")
    git(root, "remote", "add", "origin", REMOTE)
    spec = {
        "cwd": str(public),
        "publication_root": str(root),
        "remote": "origin",
        "base": "main",
        "repository": "example/Example Project/example-repo",
        "branch": "task/integration",
        "head_sha": commits[-1],
        "title": "Integrated example",
        "body": "Sanitized integration",
        "task_id": tasks[0]["id"],
        "source_project_id": "alpha",
        "source_commit": commits[0],
        "pr_id": 17,
        "reconcile_only": True,
        "integration": {"head_sha": commits[-1], "contains": commits},
    }
    host = AzureHost(spec)
    host.rows = [host.row(status)]
    adapter = AzureRepos(host)
    system.engine.publisher = RepositoryPublisher(azure_factory=lambda _: adapter)
    system.engine.lifecycle = RepositoryPRLifecycle(azure_factory=lambda _: adapter)
    original = system.host.call
    counts = {"settle": 0}

    async def call(name, args):
        assert name not in {
            "link_pull_request",
            "watch_pull_request",
            "unwatch_pull_request",
        }, "Azure native capability is deliberately unavailable, no fake ACK"
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
    return tasks, spec, host, counts


async def publish(system, task, spec):
    return await system.engine.command(
        "publish",
        {
            "task_id": task["id"],
            "publication": {
                **spec,
                "task_id": task["id"],
                "source_commit": task["report"]["commit"],
            },
        },
    )


async def test_five_tasks_share_completed_pr_preserve_reports_and_memories(system):
    tasks, spec, host, counts = await integrated_setup(system, count=5)
    reports = [copy.deepcopy(t["report"]) for t in tasks]
    memory_ids = []
    for task in tasks:
        result = await publish(system, task, spec)
        assert result["phase"] == "pr_reconciled", result
        assert result["native_pr_tracking"]["state"] == "unsupported"
        p = {**policy(spec), "merge_authorized": False, "observe_only": True}
        closed = await system.engine.command(
            "pr-check", {"task_id": task["id"], "policy": p}
        )
        assert closed["settled"] and closed["merged"], closed
        assert system.store.get(task["id"])["report"] == task["report"]
        assert closed["publication"]["source_commit"] == task["report"]["commit"]
        assert closed["pending_returns"] == 0
        memory_ids.append(closed["memory"]["id"])
    assert len(set(memory_ids)) == 5 and counts["settle"] == 5
    assert host.created == host.merges == 0
    assert [system.store.get(t["id"])["report"] for t in tasks] == reports


async def test_lost_settle_ack_reobserves_no_second_effect_or_memory(system):
    tasks, spec, host, counts = await integrated_setup(system)
    task = tasks[0]
    await publish(system, task, spec)
    original = system.host.call

    async def lost(name, args):
        result = await original(name, args)
        if name == "t3_thread_organize":
            raise TimeoutError("synthetic ACK loss")
        return result

    system.host.call = lost
    first = await system.engine.command(
        "pr-check", {"task_id": task["id"], "policy": policy(spec)}
    )
    assert first["merge_receipt"]["state"] == "merged" and not first["settled"]
    memory_head = system.service.publisher.git.head()
    again = await system.engine.command("settle", {"task_id": task["id"]})
    assert again["settled"] and counts["settle"] == 1, again
    assert system.service.publisher.git.head() == memory_head
    await system.engine.command("pr-check", {"task_id": task["id"]})
    assert system.service.publisher.git.head() == memory_head
    assert counts["settle"] == 1 and host.created == host.merges == 0
    replay = await publish(system, task, spec)
    assert replay["phase"] == "settled" and replay["settled"], replay
    assert system.service.publisher.git.head() == memory_head


@pytest.mark.parametrize(
    "case,reason",
    [
        ("source", "publication_source_binding_mismatch"),
        ("contains", "publication_integration_commit_missing"),
        ("repo", "publication_repository_mismatch"),
    ],
)
async def test_bad_integration_does_not_change_terminal_report(system, case, reason):
    tasks, spec, host, counts = await integrated_setup(system)
    task = tasks[0]
    if case == "source":
        task = {**task, "report": {**task["report"], "commit": "f" * 40}}
    elif case == "contains":
        spec["integration"]["contains"].append("f" * 40)
    else:
        spec["repository"] = "example/Example Project/another-repo"
    before = system.store.get(task["id"])["report"]
    result = await publish(system, task, spec)
    assert result["blocked_reason"] == reason, result
    assert system.store.get(task["id"])["report"] == before
    assert host.created == host.merges == counts["settle"] == 0


async def test_active_reconcile_waits_without_native_watch_and_never_merges(system):
    tasks, spec, host, counts = await integrated_setup(system, status="active")
    task = tasks[0]
    await publish(system, task, spec)
    host.policies[0]["status"] = "running"
    waiting = await system.engine.command(
        "pr-check", {"task_id": task["id"], "policy": policy(spec)}
    )
    assert waiting["phase"] == "pr_waiting", waiting
    assert not waiting["merged"] and host.merges == 0
    host.policies[0]["status"] = "approved"
    blocked = await system.engine.command("merge", {"task_id": task["id"]})
    assert blocked["blocked_reason"] == "reconciled_pr_observation_only"
    assert host.merges == counts["settle"] == 0


async def test_observed_merge_receipt_cannot_be_rebound_on_replay(system):
    tasks, spec, host, counts = await integrated_setup(system)
    task = tasks[0]
    await publish(system, task, spec)
    first = await system.engine.command(
        "pr-check", {"task_id": task["id"], "policy": policy(spec)}
    )
    original = copy.deepcopy(first["merge_receipt"])
    host.rows[0]["lastMergeCommit"]["commitId"] = "c" * 40
    result = await system.engine.command("pr-check", {"task_id": task["id"]})
    assert result["blocked_reason"] == "merge_receipt_changed"
    assert result["merge_receipt"] == original and counts["settle"] == 1
    result = await publish(system, task, spec)
    assert result["blocked_reason"] == "publication_merge_receipt_changed"
    assert result["merge_receipt"] == original


@pytest.mark.parametrize(
    "case",
    [
        "other_pr",
        "negative_report",
        "pending_question",
        "changed_remote",
        "changed_merge",
    ],
)
async def test_settle_keeps_all_execution_and_pr_gates(system, case):
    tasks, spec, host, counts = await integrated_setup(system)
    task = tasks[0]
    await publish(system, task, spec)
    original = system.host.call
    if case == "negative_report":
        system.store.update(
            task["id"],
            "pr_reconciled",
            {"test": "legacy negative report"},
            report={**task["report"], "verdict": "FAIL"},
        )
    elif case == "changed_remote":
        git(
            spec["cwd"],
            "remote",
            "set-url",
            "origin",
            "https://dev.azure.com/example/Other/_git/example-repo",
        )

    async def gate(name, args):
        result = await original(name, args)
        if name == "list_thread_pull_requests" and case == "other_pr":
            result["pullRequests"] = [
                {
                    "host": "github.com",
                    "repository": "example/other",
                    "number": 99,
                    "state": "open",
                }
            ]
        if (
            name == "t3_thread_read"
            and args["threadId"] == task["child"]["childThreadId"]
            and case == "pending_question"
        ):
            result["thread"]["pendingRequestCount"] = 1
        if name == "list_thread_pull_requests" and case == "changed_merge":
            host.rows[0]["lastMergeCommit"]["commitId"] = "c" * 40
        return result

    system.host.call = gate
    result = await system.engine.command(
        "pr-check", {"task_id": task["id"], "policy": policy(spec)}
    )
    assert not result["settled"] and counts["settle"] == 0, result
    assert result["phase"] == "blocked"
    assert host.created == host.merges == 0


async def test_deployment_is_separate_reported_optional_and_idempotent(system):
    tasks, spec, host, _ = await integrated_setup(system)
    task = tasks[0]
    await publish(system, task, spec)
    result = await system.engine.command(
        "pr-check", {"task_id": task["id"], "policy": policy(spec)}
    )
    assert result["settled"] and not result.get("deployments")
    evidence = {
        "release": "b" * 40,
        "environment": "Example VM",
        "occurred_at": "2026-10-26T12:00:00Z",
        "state": "reported",
        "proof": [
            {
                "source": "origin",
                "reference": "private:receipt.json",
                "sha256": "c" * 64,
            }
        ],
    }
    for _ in range(2):
        result = await system.engine.command(
            "deployment", {"task_id": task["id"], "deployment": evidence}
        )
    assert (
        len(result["deployments"]) == 1
        and result["deployments"][0]["verification"] == "not_observed"
    )
    assert result["settled"] and result["merged"]
    result = await system.engine.command(
        "deployment",
        {"task_id": task["id"], "deployment": {**evidence, "state": "confirmed"}},
    )
    assert result["blocked_reason"] == "deployment_confirmation_unavailable"
    assert len(result["deployments"]) == 1


@pytest.mark.parametrize(
    "field,value",
    [("release", "f" * 40), ("occurred_at", "2026-10-26T12:00:00"), ("proof", [])],
)
async def test_bad_deployment_never_changes_merge_or_fabricates_confirmation(
    system, field, value
):
    tasks, spec, host, _ = await integrated_setup(system)
    task = tasks[0]
    await publish(system, task, spec)
    result = await system.engine.command(
        "pr-check", {"task_id": task["id"], "policy": policy(spec)}
    )
    merge = copy.deepcopy(result["merge_receipt"])
    evidence = {
        "release": "b" * 40,
        "environment": "Example VM",
        "occurred_at": "2026-10-26T12:00:00Z",
        "state": "reported",
        "proof": [
            {
                "source": "origin",
                "reference": "private:receipt.json",
                "sha256": "c" * 64,
            }
        ],
        field: value,
    }
    result = await system.engine.command(
        "deployment", {"task_id": task["id"], "deployment": evidence}
    )
    assert result["phase"] == "blocked" and not result.get("deployments")
    assert result["merge_receipt"] == merge
    assert result["settled"] and result["merged"] and host.created == host.merges == 0
