"""Managed lifecycle: real socket memory/Git/SQLite; simulated caller-bound T3.

No T3 or GitHub service is contacted. All worktrees and vault commits are inside
pytest temporary directories; host observations are changed explicitly in tests.
"""

import asyncio
import copy
import hashlib
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_publication import Host as GitHubHost
from test_publication import specification
from test_service import command as service_command

from oraculo.managed_tasks import ManagedTasks
from oraculo.publication import GitHubPublisher, command_runner
from oraculo.state import DedupeConflict
from oraculo.task_store import TaskStore
from oraculo.task_transport import SocketMemory


def git(path, *args):
    return command_runner(["git", *args], Path(path))


def initialize(path):
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-b", "main")
    git(
        path,
        "-c",
        "user.name=Example",
        "-c",
        "user.email=example@example.org",
        "commit",
        "--allow-empty",
        "-m",
        "Initial project",
    )


class Host:
    def __init__(self, root):
        self.root = root
        self.caller = "origin"
        self.projects = [{"id": "alpha", "workspaceRoot": str(root / "alpha")}]
        self.threads = {
            "origin": {
                "threadId": "origin",
                "projectId": "alpha",
                "parentThreadId": None,
                "activeRunId": "origin-run",
                "latestRunId": "origin-run",
                "status": "running",
                "worktreePath": str(root / "alpha"),
                "branch": "main",
                "title": "Origin conversation",
            }
        }
        self.calls = []
        self.delegations = {}
        self.native_status = {}
        self.created = []
        self.sends = {}
        self.links = set()
        self.fail_delegate_once = False
        self.fail_launch_once = False
        self.fail_send_once = False
        self.list_visible = True

    def complete(self, child_id, *, pending=False, status="completed"):
        thread = self.threads[child_id]
        thread.update(status=status, activeRunId=None)
        for observed in self.native_status.values():
            if observed["childThreadId"] == child_id:
                observed.update(
                    status=status,
                    workState="result_available",
                    hasPendingChildRuns=pending,
                )
        if pending:
            self.threads["reviewer-" + child_id] = {
                "threadId": "reviewer-" + child_id,
                "projectId": thread["projectId"],
                "parentThreadId": child_id,
                "status": "running",
                "activeRunId": "review-run",
                "latestRunId": "review-run",
                "title": "Review",
            }

    def allocate(self, args, *, native):
        number = len(self.created) + 1
        child_id, run_id = f"child-{number}", f"child-run-{number}"
        brief = json.loads(args["task"] if native else args["message"])
        project_id = brief["project_id"]
        root = next(
            Path(p["workspaceRoot"]) for p in self.projects if p["id"] == project_id
        )
        strategy = args.get("workspaceStrategy", {})
        branch = f"host/child-{number}" if native else strategy["branch"]
        base = brief["base_sha"] if native else strategy["baseRef"]
        worktree = self.root.parent / "worktrees" / child_id
        worktree.parent.mkdir(exist_ok=True)
        git(root, "worktree", "add", "-b", branch, str(worktree), base)
        self.threads[child_id] = {
            "threadId": child_id,
            "projectId": project_id,
            "parentThreadId": self.caller if native else None,
            "activeRunId": run_id,
            "latestRunId": run_id,
            "status": "running",
            "worktreePath": str(worktree),
            "branch": branch,
            "title": args["title"],
        }
        self.created.append(child_id)
        if native:
            receipt = {
                "taskId": f"host-task-{number}",
                "childThreadId": child_id,
                "childRunId": run_id,
            }
            self.native_status[receipt["taskId"]] = {
                **receipt,
                "status": "running",
                "workState": "working",
                "hasPendingChildRuns": False,
            }
        else:
            receipt = {"threadId": child_id, "projectId": project_id, "runId": run_id}
        return receipt

    async def call(self, name, args):
        self.calls.append((name, copy.deepcopy(args)))
        if name == "orchestrator_capabilities":
            return {
                "parentThreadId": self.caller,
                "features": {"appOwnedSubagents": True},
            }
        if name == "t3_project_list":
            return {"projects": copy.deepcopy(self.projects), "nextCursor": None}
        if name == "t3_thread_read":
            thread = copy.deepcopy(self.threads[args["threadId"]])
            thread.setdefault("pendingRequestCount", 0)
            thread.setdefault("archived", False)
            thread.setdefault("settled", False)
            thread.setdefault("updatedAt", "2026-01-01T00:00:00Z")
            thread.setdefault("itemCount", 2)
            return {
                "thread": thread,
                "items": [],
                "hasMore": False,
                "nextPosition": None,
                "recentRuns": [
                    {
                        "runId": thread["latestRunId"],
                        "status": thread["status"],
                    }
                ],
            }
        if name == "delegate_task":
            key = args["clientRequestId"]
            if key not in self.delegations:
                self.delegations[key] = self.allocate(args, native=True)
            if self.fail_delegate_once:
                self.fail_delegate_once = False
                raise TimeoutError("simulated lost delegate response")
            return copy.deepcopy(self.delegations[key])
        if name == "task_status":
            return copy.deepcopy(self.native_status[args["taskId"]])
        if name == "t3_thread_launch":
            receipt = self.allocate(args, native=False)
            if self.fail_launch_once:
                self.fail_launch_once = False
                raise TimeoutError("simulated lost launch response")
            return receipt
        if name == "t3_thread_list":
            rows = list(self.threads.values()) if self.list_visible else []
            return {
                "projectId": args["projectId"],
                "nextCursor": None,
                "threads": [
                    copy.deepcopy(t)
                    for t in rows
                    if t["projectId"] == args["projectId"]
                ],
            }
        if name == "t3_thread_send":
            key = args["clientRequestId"]
            if key not in self.sends:
                self.sends[key] = {
                    "arguments": copy.deepcopy(args),
                    "receipt": {
                        "threadId": args["threadId"],
                        "messageId": "message-" + str(len(self.sends)),
                    },
                }
            else:
                assert self.sends[key]["arguments"] == args
            if self.fail_send_once:
                self.fail_send_once = False
                raise TimeoutError("simulated lost return response")
            return self.sends[key]["receipt"]
        if name == "list_thread_pull_requests":
            return {
                "pullRequests": [
                    {"url": url}
                    for thread, url in self.links
                    if thread == args["threadId"]
                ]
            }
        if name == "link_pull_request":
            self.links.add((args["threadId"], args["url"]))
            return {"url": args["url"]}
        raise AssertionError((name, args))


@pytest.fixture
async def system(running_service):
    service = running_service
    initialize(service.config.projects_root / "alpha")
    store = TaskStore(service.config.state_dir / "managed-test.db")
    host = Host(service.config.projects_root)
    memory = SocketMemory(service.config)
    engine = ManagedTasks(service.config, store, host, memory)
    try:
        yield SimpleNamespace(
            service=service, store=store, host=host, memory=memory, engine=engine
        )
    finally:
        store.close()


async def start(system, **overrides):
    return await system.engine.command(
        "start",
        {
            "kind": "work",
            "key": "implement-example",
            "cwd": str(system.service.config.projects_root / "alpha"),
            "text": "Implement and validate the bounded example change.",
            "title": "Bounded example",
            **overrides,
        },
    )


def report(task):
    return {
        "summary": "Implemented bounded example",
        "checks": [{"command": "pytest tests/test_example.py", "status": "passed"}],
        "commit": git(task["child_workspace"]["path"], "rev-parse", "HEAD"),
        "limitations": ["T3 transport simulated in isolated test"],
    }


async def test_default_same_project_uses_native_child_and_real_memory(system):
    result = await start(system)
    assert result["phase"] == "executing", result
    assert result["transport_mode"] == "native_child"
    assert result["origin"]["thread_id"] == "origin"
    child = system.host.threads[result["child"]["childThreadId"]]
    assert child["parentThreadId"] == "origin"
    assert result["child_workspace"]["path"] != result["parent_workspace"]["path"]
    assert result["child_workspace"]["branch"] != "main"
    assert result["child_workspace"]["head"] == result["base_sha"]
    assert result["memory"]["read_state"] == "empty"
    assert result["memory"]["read_receipt"]["base_commit"]
    invocation = next(
        args for name, args in system.host.calls if name == "delegate_task"
    )
    brief = json.loads(invocation["task"])
    artifact = Path(brief["memory"]["path"])
    assert (
        hashlib.sha256(artifact.read_bytes()).hexdigest() == brief["memory"]["sha256"]
    )
    assert json.loads(artifact.read_text())["cycle"]["project_id"] == "alpha"
    assert not any(name == "t3_thread_launch" for name, _ in system.host.calls)
    assert result["pending_returns"] == 0
    assert all(v["receipt"]["threadId"] == "origin" for v in system.host.sends.values())


async def test_explicit_independent_uses_top_level_with_managed_origin(system):
    result = await start(system, independent=True)
    assert result["phase"] == "executing", result
    assert result["transport_mode"] == "managed_thread"
    child = system.host.threads[result["child"]["childThreadId"]]
    assert child["parentThreadId"] is None
    assert result["origin"]["thread_id"] == "origin"
    assert result["child_workspace"]["branch"] == result["expected_branch"]
    assert not any(name == "delegate_task" for name, _ in system.host.calls)


async def test_explicit_other_project_uses_destination_git_and_memory(system):
    service = system.service
    beta = service.config.projects_root / "beta"
    initialize(beta)
    git(
        beta,
        "-c",
        "user.name=Example",
        "-c",
        "user.email=example@example.org",
        "commit",
        "--allow-empty",
        "-m",
        "Beta base",
    )
    service.publisher.initialize_project("beta")
    service.state.register_project("beta", str(beta))
    system.host.projects.append({"id": "beta", "workspaceRoot": str(beta)})
    actor = {"thread_id": "seed", "run_id": "seed-run"}
    for project, secret in (
        ("alpha", "alpha-only-knowledge"),
        ("beta", "beta-only-knowledge"),
    ):
        seed = await system.memory.begin(
            project, actor, "seed-" + project, "bounded example"
        )
        await system.memory.finish(
            project,
            actor,
            seed["cycle"]["id"],
            "succeeded",
            {"summary": "bounded example " + secret},
        )
    result = await start(
        system, project="beta", text="Implement bounded example using project knowledge"
    )
    assert result["phase"] == "executing", result
    assert result["project"]["project_id"] == "beta"
    assert result["transport_mode"] == "managed_thread"
    assert result["base_sha"] == git(beta, "rev-parse", "HEAD")
    assert result["base_sha"] != git(
        service.config.projects_root / "alpha", "rev-parse", "HEAD"
    )
    task = system.store.get(result["id"])
    assert task["memory"]["project_id"] == "beta"
    context = json.dumps(task["context"])
    assert "beta-only-knowledge" in context
    assert "alpha-only-knowledge" not in context
    assert system.host.threads[result["child"]["childThreadId"]]["projectId"] == "beta"


async def test_native_timeout_retries_same_key_after_adapter_restart(system):
    system.host.fail_delegate_once = True
    first = await start(system)
    assert first["phase"] == "blocked"
    assert first["launch_attempted"] is True
    database = system.store.state.path
    system.store.close()
    reopened = TaskStore(database)
    try:
        restarted = ManagedTasks(
            system.service.config, reopened, system.host, system.memory
        )
        resumed = await restarted.command("resume", {"task_id": first["id"]})
    finally:
        reopened.close()
    assert resumed["phase"] == "executing", resumed
    calls = [a for n, a in system.host.calls if n == "delegate_task"]
    assert len(calls) == 2 and calls[0] == calls[1]
    assert len(system.host.created) == 1


@pytest.mark.parametrize("visible", [True, False])
async def test_top_level_timeout_only_reconciles_never_relaunches(system, visible):
    system.host.fail_launch_once = True
    first = await start(system, independent=True)
    assert first["phase"] == "blocked"
    system.host.list_visible = visible
    resumed = await system.engine.command("resume", {"task_id": first["id"]})
    assert resumed["phase"] == ("executing" if visible else "blocked"), resumed
    if not visible:
        assert resumed["blocked_reason"] == "launch_uncertain_no_unique_match"
        await system.engine.command("resume", {"task_id": first["id"]})
    assert len([1 for n, _ in system.host.calls if n == "t3_thread_launch"]) == 1
    assert len(system.host.created) == 1


async def test_dirty_source_blocks_before_memory_and_launch(system):
    root = system.service.config.projects_root / "alpha"
    (root / "uncommitted.txt").write_text("unselected work")
    result = await start(system)
    assert result["blocked_reason"] == "dirty_source_commit_selection_required"
    assert result["memory"] is None
    assert system.host.created == []
    assert (root / "uncommitted.txt").read_text() == "unselected work"


async def test_status_list_and_non_work_messages_do_not_create_tasks(system):
    task = await start(system)
    for kind in ("status", "question", "memory", "continuation"):
        response = await start(system, kind=kind)
        assert response == {"state": "not_work", "created": False}
    for _ in range(2):
        status = await system.engine.command("status", {"task_id": task["id"]})
        assert status["id"] == task["id"]
    listed = await system.engine.command("list", {})
    assert [t["id"] for t in listed["tasks"]] == [task["id"]]
    assert len(system.host.created) == 1
    assert len(system.service.memory_cycles.list()) == 1


async def test_continuation_delivers_once_to_existing_child(system):
    task = await start(system)
    continuation = {
        "task_id": task["id"],
        "key": "clarification",
        "text": "Also validate the empty input.",
    }
    await system.engine.command("continue", continuation)
    await system.engine.command("continue", continuation)
    sent = [
        v
        for v in system.host.sends.values()
        if v["arguments"]["message"] == continuation["text"]
    ]
    assert len(sent) == 1
    assert sent[0]["arguments"]["threadId"] == task["child"]["childThreadId"]
    assert len(system.host.created) == 1


async def test_return_response_lost_replays_outbox_with_same_key(system):
    system.host.fail_send_once = True
    task = await start(system)
    assert task["pending_returns"] > 0
    original_keys = set(system.host.sends)
    retried = await system.engine.command("status", {"task_id": task["id"]})
    assert retried["pending_returns"] == 0
    assert original_keys <= set(system.host.sends)
    first_key = next(iter(original_keys))
    assert (
        len(
            [
                1
                for n, a in system.host.calls
                if n == "t3_thread_send" and a["clientRequestId"] == first_key
            ]
        )
        == 2
    )


@pytest.mark.parametrize("independent", [False, True])
async def test_finish_waits_for_descendant_then_writes_real_memory(system, independent):
    task = await start(system, independent=independent)
    child = task["child"]["childThreadId"]
    system.host.complete(child, pending=True)
    payload = {"task_id": task["id"], "report": report(task)}
    blocked = await system.engine.command("finish", payload)
    assert blocked["blocked_reason"] == "children_still_running", blocked
    assert blocked["work_status"] == "pending"
    system.host.complete(child)
    reviewer = system.host.threads["reviewer-" + child]
    reviewer.update(status="completed", activeRunId=None)
    finished = await system.engine.command("finish", payload)
    assert finished["phase"] == "pr_pending", finished
    assert finished["work_status"] == "succeeded"
    assert finished["memory"]["write_state"] == "saved"
    receipt = finished["memory"]["write_receipt"]
    assert receipt["state"] == "confirmed" and receipt["commit"]
    assert receipt["operation_id"] == finished["memory"]["id"]
    assert not finished["merged"] and not finished["settled"]
    vault_head = system.service.publisher.git.head()
    again = await system.engine.command("finish", payload)
    assert again["memory"]["write_receipt"]["commit"] == receipt["commit"]
    assert system.service.publisher.git.head() == vault_head


async def test_memory_read_failure_blocks_launch(system, monkeypatch):
    def unavailable():
        raise OSError("isolated unavailable vault")

    monkeypatch.setattr(system.service.publisher, "_check_integrity", unavailable)
    blocked = await start(system)
    assert blocked["phase"] == "blocked"
    assert blocked["blocked_reason"] == "memory_read_unavailable", blocked
    assert system.host.created == []


async def test_memory_write_failure_is_not_success_or_pr_ready(system, monkeypatch):
    task = await start(system)
    system.host.complete(task["child"]["childThreadId"])

    def unavailable(*args, **kwargs):
        raise OSError("isolated publication failure")

    monkeypatch.setattr(system.service.publisher, "publish", unavailable)
    result = await system.engine.command(
        "finish", {"task_id": task["id"], "report": report(task)}
    )
    assert result["phase"] == "blocked"
    assert result["blocked_reason"] == "memory_write_unconfirmed"
    assert result["work_status"] == "succeeded"
    assert result["memory"]["write_state"] == "failed"
    assert result["publication"] is None
    assert not result["settled"]


async def test_publish_requires_finished_work_and_memory(system):
    task = await start(system)
    result = await system.engine.command(
        "publish", {"task_id": task["id"], "publication": {}}
    )
    assert result["blocked_reason"] == "work_and_memory_required_before_pr"
    assert result["publication"] is None


async def test_successful_work_without_publication_adapter_stays_pr_pending(system):
    task = await start(system)
    system.host.complete(task["child"]["childThreadId"])
    finished = await system.engine.command(
        "finish", {"task_id": task["id"], "report": report(task)}
    )
    assert finished["phase"] == "pr_pending"
    result = await system.engine.command(
        "publish", {"task_id": task["id"], "publication": {}}
    )
    assert result["blocked_reason"] == "publication_host_adapter_unavailable"
    assert result["publication"] is None


async def publication_setup(system):
    task = await start(system)
    system.host.complete(task["child"]["childThreadId"])
    finished = await system.engine.command(
        "finish", {"task_id": task["id"], "report": report(task)}
    )
    assert finished["phase"] == "pr_pending", finished
    public = system.service.config.projects_root / "public-project"
    initialize(public)
    git(public, "switch", "-c", "task/example")
    git(
        public,
        "remote",
        "add",
        "origin",
        "https://github.com/example/public-project.git",
    )
    spec = specification(public)
    spec.update(
        {
            "cwd": str(public),
            "publication_root": str(public),
            "repository": "example/public-project",
            "source_project_id": "alpha",
            "source_commit": report(task)["commit"],
            "task_id": task["id"],
        }
    )
    host = GitHubHost(spec)
    system.engine.publisher = GitHubPublisher(host)
    return task, spec, host


async def test_publication_separate_safe_checkout_links_origin_and_saves_real_memory(
    system,
):
    task, spec, github = await publication_setup(system)
    private_root = system.service.config.projects_root / "alpha"
    assert git(private_root, "remote") == ""
    published = await system.engine.command(
        "publish", {"task_id": task["id"], "publication": spec}
    )
    assert published["phase"] == "pr_open", published
    assert published["publication"]["source_commit"] == spec["source_commit"]
    assert published["publication"]["source_project_id"] == "alpha"
    assert published["publication_memory"]["write_state"] == "saved"
    assert published["publication_memory"]["write_receipt"]["commit"]
    url = published["publication"]["url"]
    assert system.host.links == {("origin", url), (task["child"]["childThreadId"], url)}
    assert not published["merged"] and not published["settled"]
    assert github.created == 1
    assert git(private_root, "remote") == ""
    head = system.service.publisher.git.head()
    again = await system.engine.command(
        "publish", {"task_id": task["id"], "publication": spec}
    )
    assert again["phase"] == "pr_open", again
    assert github.created == 1
    assert system.service.publisher.git.head() == head
    assert len(system.service.memory_cycles.list()) == 2


async def test_publication_preflight_failure_does_not_persist_emission_intent(system):
    task, spec, github = await publication_setup(system)
    github.remote_sha = "a" * 40
    blocked = await system.engine.command(
        "publish", {"task_id": task["id"], "publication": spec}
    )
    assert blocked["blocked_reason"] == "publication_remote_head_mismatch", blocked
    assert not blocked.get("publication_attempted")
    assert github.created == 0
    github.remote_sha = spec["head_sha"]
    repaired = await system.engine.command(
        "publish", {"task_id": task["id"], "publication": spec}
    )
    assert repaired["phase"] == "pr_open", repaired
    assert github.created == 1


async def test_publication_timeout_reconcile_never_creates_second_pr(system):
    task, spec, github = await publication_setup(system)
    github.lose_response = True
    github.visible = False
    first = await system.engine.command(
        "publish", {"task_id": task["id"], "publication": spec}
    )
    assert first["blocked_reason"] == "publication_uncertain", first
    assert first["publication_attempted"] is True
    assert github.created == 1
    second = await system.engine.command(
        "publish", {"task_id": task["id"], "publication": spec}
    )
    assert second["blocked_reason"] == "publication_uncertain"
    assert github.created == 1
    github.visible = True
    recovered = await system.engine.command(
        "publish", {"task_id": task["id"], "publication": spec}
    )
    assert recovered["phase"] == "pr_open", recovered
    assert github.created == 1


async def test_cached_publication_refreshes_closed_state(system):
    task, spec, github = await publication_setup(system)
    first = await system.engine.command(
        "publish", {"task_id": task["id"], "publication": spec}
    )
    assert first["phase"] == "pr_open", first
    github.rows[0]["state"] = "closed"
    closed = await system.engine.command(
        "publish", {"task_id": task["id"], "publication": spec}
    )
    assert closed["phase"] == "blocked", closed
    assert closed["blocked_reason"] == "pr_not_open_requires_reconciliation"
    assert closed["publication"]["state"] == "closed"
    assert github.created == 1


@pytest.mark.parametrize("field", ["source_project_id", "source_commit"])
async def test_publication_rejects_wrong_source_binding_before_host_write(
    system, field
):
    task, spec, github = await publication_setup(system)
    spec[field] = "other-project" if field == "source_project_id" else "b" * 40
    rejected = await system.engine.command(
        "publish", {"task_id": task["id"], "publication": spec}
    )
    assert rejected["blocked_reason"] == "publication_source_binding_mismatch", rejected
    assert not rejected.get("publication_attempted")
    assert github.created == 0


async def test_pr_memory_failure_preserves_pr_and_retry_does_not_duplicate(
    system, monkeypatch
):
    task, spec, github = await publication_setup(system)
    original_publish = system.service.publisher.publish

    def unavailable(*args, **kwargs):
        raise OSError("isolated receipt publication unavailable")

    monkeypatch.setattr(system.service.publisher, "publish", unavailable)
    failed = await system.engine.command(
        "publish", {"task_id": task["id"], "publication": spec}
    )
    assert failed["blocked_reason"] == "pr_memory_write_unconfirmed", failed
    assert failed["publication"]["url"]
    assert failed["publication_memory"]["write_state"] == "failed"
    assert github.created == 1
    monkeypatch.setattr(system.service.publisher, "publish", original_publish)
    recovered = await system.engine.command(
        "publish", {"task_id": task["id"], "publication": spec}
    )
    assert recovered["phase"] == "pr_open", recovered
    assert recovered["publication_memory"]["write_state"] == "saved"
    assert github.created == 1


@pytest.mark.parametrize("invalid", ["commit", "dirty", "checks"])
async def test_finish_requires_actual_clean_validated_commit(system, invalid):
    task = await start(system)
    system.host.complete(task["child"]["childThreadId"])
    evidence = report(task)
    if invalid == "commit":
        evidence["commit"] = "f" * 40
    elif invalid == "dirty":
        (Path(task["child_workspace"]["path"]) / "uncommitted").write_text("unfinished")
    else:
        evidence["checks"] = [{"command": "pytest", "status": "failed"}]
    rejected = await system.engine.command(
        "finish", {"task_id": task["id"], "report": evidence}
    )
    assert rejected["phase"] == "blocked"
    assert rejected["work_status"] == "pending"
    assert rejected["memory"]["write_state"] == "not_requested"
    assert rejected["blocked_reason"] == (
        "passing_validation_evidence_required"
        if invalid == "checks"
        else "validated_commit_or_clean_workspace_required"
    )


async def test_active_child_report_is_submitted_without_faking_terminal_success(system):
    task = await start(system, independent=True)
    child_id = task["child"]["childThreadId"]
    system.host.caller = child_id
    submitted = await system.engine.command(
        "finish", {"task_id": task["id"], "report": report(task)}
    )
    assert submitted["phase"] == "validation_submitted", submitted
    assert submitted["work_status"] == "pending"
    assert submitted["memory"]["write_state"] == "not_requested"
    system.host.complete(child_id)
    system.host.caller = "origin"
    await system.engine.reconcile(task["id"])
    finished = system.store.describe(task["id"])
    assert finished["phase"] == "pr_pending", finished
    assert finished["memory"]["write_state"] == "saved"


async def test_repeated_start_same_key_reuses_task_and_changed_brief_conflicts(system):
    first = await start(system)
    second = await start(system)
    assert first["id"] == second["id"]
    assert first["child"] == second["child"]
    assert len(system.host.created) == 1
    with pytest.raises(DedupeConflict, match="task_key_reused"):
        await start(system, text="A different request must have a different task key.")
    assert len(system.host.created) == 1


@pytest.mark.parametrize(
    ("terminal", "work_status"),
    [
        ("failed", "failed"),
        ("cancelled", "stopped"),
        ("interrupted", "stopped"),
    ],
)
async def test_unsuccessful_host_execution_saves_memory_but_never_becomes_pr_ready(
    system, terminal, work_status
):
    task = await start(system)
    system.host.complete(task["child"]["childThreadId"], status=terminal)
    evidence = report(task)
    evidence["checks"] = [{"command": "pytest", "status": "failed"}]
    finished = await system.engine.command(
        "finish", {"task_id": task["id"], "report": evidence}
    )
    assert finished["phase"] == "blocked", finished
    assert finished["blocked_reason"] == "work_not_successful"
    assert finished["work_status"] == work_status
    assert finished["memory"]["write_state"] == "saved"
    assert finished["publication"] is None
    assert not finished["settled"]


async def test_native_timeout_retry_rechecks_revoked_project_allowlist(system):
    system.host.fail_delegate_once = True
    first = await start(system)
    assert first["phase"] == "blocked" and first["launch_attempted"]
    system.service.config.allowed_projects = []
    retried = await system.engine.command("resume", {"task_id": first["id"]})
    assert retried["phase"] == "blocked"
    assert retried["blocked_reason"] == "managed_project_scope_revoked"
    assert len([1 for name, _ in system.host.calls if name == "delegate_task"]) == 1
    assert len(system.host.created) == 1


async def test_native_timeout_retry_rechecks_origin_project_identity(system):
    system.host.fail_delegate_once = True
    first = await start(system)
    assert first["phase"] == "blocked" and first["launch_attempted"]
    system.host.threads["origin"]["projectId"] = "another-project"
    retried = await system.engine.command("resume", {"task_id": first["id"]})
    assert retried["phase"] == "blocked"
    assert retried["blocked_reason"] == "native_retry_parent_binding_changed"
    assert len([1 for name, _ in system.host.calls if name == "delegate_task"]) == 1
    assert len(system.host.created) == 1


async def test_daemon_recovers_lost_top_level_launch_response_without_relaunch(system):
    system.host.fail_launch_once = True
    first = await start(system, independent=True)
    assert first["phase"] == "blocked" and not first.get("child")
    await system.engine.reconcile(first["id"])
    recovered = system.store.describe(first["id"])
    assert recovered["phase"] == "executing", recovered
    assert recovered["child"]["childThreadId"] == system.host.created[0]
    assert recovered["pending_returns"] == 0
    assert recovered.get("blocked_reason") is None
    assert len([1 for name, _ in system.host.calls if name == "t3_thread_launch"]) == 1
    assert len(system.host.created) == 1


@pytest.mark.parametrize(
    "pending", ["queued_thread", "pending_request", "later_queued_run"]
)
async def test_completed_original_run_cannot_finish_while_thread_has_pending_work(
    system, monkeypatch, pending
):
    task = await start(system, independent=True)
    child_id = task["child"]["childThreadId"]
    run_id = task["child"]["childRunId"]
    system.host.complete(child_id)
    thread = system.host.threads[child_id]
    if pending == "queued_thread":
        thread["status"] = "queued"
    elif pending == "pending_request":
        thread["pendingRequestCount"] = 1
    else:
        thread["latestRunId"] = "later-run"
    original_call = system.host.call

    async def observed(name, args):
        response = await original_call(name, args)
        if name == "t3_thread_read" and args["threadId"] == child_id:
            response["recentRuns"] = [
                {"runId": run_id, "status": "completed", "ordinal": 1}
            ]
            if pending == "later_queued_run":
                response["recentRuns"].append(
                    {"runId": "later-run", "status": "queued", "ordinal": 2}
                )
        return response

    monkeypatch.setattr(system.host, "call", observed)
    result = await system.engine.command(
        "finish", {"task_id": task["id"], "report": report(task)}
    )
    assert result["phase"] == "blocked", result
    assert result["blocked_reason"] == "children_still_running"
    assert result["work_status"] == "pending"
    assert result["memory"]["write_state"] == "not_requested"
    assert result["publication"] is None


async def test_service_background_reconciliation_does_not_block_socket(
    system, monkeypatch
):
    service = system.service
    # Use the daemon's separate, real managed store; the ordinary test adapter
    # store remains untouched. No host write is necessary for this scheduling test.
    store = TaskStore(service.config.state_dir / "managed-tasks.sqlite3")
    task = store.create(
        {"thread_id": "origin", "run_id": "origin-run", "project_id": "alpha"},
        "background-check",
        {"kind": "work", "text": "Bounded background check"},
    )
    store.close()
    entered, release = threading.Event(), threading.Event()
    worker_threads, released = [], []

    async def blocked_reconcile(self, task_id):
        assert task_id == task["id"]
        worker_threads.append(threading.get_ident())
        entered.set()
        # Intentionally synchronous: represents slow Git/GitHub in the adapter.
        # The finite bound prevents a regression from hanging the test process.
        released.append(release.wait(timeout=3))

    monkeypatch.setattr(ManagedTasks, "reconcile", blocked_reconcile)
    try:
        await asyncio.wait_for(service.reconcile_managed_tasks(system.host), timeout=1)
        assert await asyncio.to_thread(entered.wait, 1)
        assert worker_threads == [worker_threads[0]]
        assert worker_threads[0] != threading.get_ident()
        active = service.managed_flush
        assert active is not None and not active.done()
        heartbeat = await asyncio.wait_for(
            service_command(service, "status.get", {}), timeout=1
        )
        assert heartbeat["ok"], heartbeat
        assert not release.is_set() and not active.done()
        await service.reconcile_managed_tasks(system.host)
        assert service.managed_flush is active  # Never duplicate a live batch.
    finally:
        release.set()
        if service.managed_flush is not None:
            await asyncio.wait_for(service.managed_flush, timeout=2)
    assert released == [True]
