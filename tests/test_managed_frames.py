"""Host frame budget of the bridged origin path: count and size, fake host.

Each host call is one frame a model may have to relay by hand. The fixtures
are synthetic: a project with many ended threads, none of them a descendant.
"""

import copy
import hashlib
import json
import os

import pytest
from test_managed_tasks import Host, git, report, start, system  # noqa: F401

from oraculo.protocol import canonical_json

FRAME_LIMIT = 10_000


class CountingHost(Host):
    """Honors the list filters and paging the real host has; measures frames."""

    def __init__(self, root):
        super().__init__(root)
        self.frames = []
        self.clock = 0
        for number in range(150):
            # Ended work of the project: the bulk of a real catalog. Every
            # tenth one failed long ago, before any task of these tests.
            self.threads[f"old-{number}"] = {
                "threadId": f"old-{number}",
                "projectId": "alpha",
                "parentThreadId": "origin" if number % 2 else None,
                "activeRunId": None,
                "latestRunId": f"old-run-{number}",
                "status": "failed" if number % 10 == 0 else "completed",
                "title": "Ended work " + "x" * 600,
                "createdAt": "2025-01-01T00:00:00Z",
                "updatedAt": f"2025-01-02T00:{number % 60:02d}:00Z",
            }

    def stamp(self):
        self.clock += 1
        return f"2026-01-01T00:00:{self.clock:02d}Z"

    def allocate(self, args, *, native):
        receipt = super().allocate(args, native=native)
        thread = self.threads[receipt["childThreadId" if native else "threadId"]]
        thread["createdAt"] = thread["updatedAt"] = self.stamp()
        return receipt

    def complete(self, child_id, **kwargs):
        super().complete(child_id, **kwargs)
        self.threads[child_id]["updatedAt"] = self.stamp()

    async def call(self, name, args):
        if name == "t3_thread_list":
            self.calls.append((name, copy.deepcopy(args)))
            rows = sorted(
                (
                    t
                    for t in self.threads.values()
                    if t["projectId"] == args["projectId"]
                    and ("statuses" not in args or t["status"] in args["statuses"])
                    and args.get("titleContains", "") in t.get("title", "")
                ),
                key=lambda t: t.get("updatedAt", "2026-01-01T00:00:00Z"),
                reverse=True,
            )
            first = args.get("cursor") or 0
            page = rows[first : first + args["limit"]]
            result = {
                "projectId": args["projectId"],
                "nextCursor": first + len(page)
                if first + len(page) < len(rows)
                else None,
                "total": len(rows),
                "threads": [
                    dict(
                        {"updatedAt": "2026-01-01T00:00:00Z"},
                        **copy.deepcopy(t),
                        itemCount=2,
                    )
                    for t in page
                ],
            }
        else:
            result = await super().call(name, args)
        self.frames.append(
            (
                name,
                len(canonical_json(args).encode()),
                len(canonical_json(result).encode()),
            )
        )
        return result


@pytest.fixture
def counted(system):  # noqa: F811
    host = CountingHost(system.service.config.projects_root)
    system.host = host
    system.engine.host = host
    return system


def budget(host, limit):
    names = [name for name, _, _ in host.frames]
    assert len(names) <= limit, [(n, a) for n, a, _ in host.frames]
    oversized = [f for f in host.frames if max(f[1], f[2]) > FRAME_LIMIT]
    assert not oversized, oversized
    return names


async def test_start_frames_are_few_and_small(counted):
    task = await start(counted, text="Implement the bounded change. " * 400)
    assert task["phase"] == "executing"
    names = budget(counted.host, 7)
    assert names == [
        "orchestrator_capabilities",
        "t3_thread_read",
        "t3_project_list",
        "delegate_task",
        "t3_thread_read",
        "task_status",
        "t3_thread_send",
    ]


@pytest.mark.parametrize("action", ["finish", "resume"])
async def test_origin_closure_frames_are_few_and_small(counted, action):
    task = await start(counted)
    child = task["child"]["childThreadId"]
    if action == "resume":
        # The child reports while it still runs; the origin closes later.
        counted.host.caller = child
        await counted.engine.command(
            "finish", {"task_id": task["id"], "report": report(task)}
        )
        counted.host.caller = "origin"
    counted.host.complete(child)
    counted.host.frames.clear()
    payload = {"task_id": task["id"]}
    if action == "finish":
        payload["report"] = report(task)
    done = await counted.engine.command(action, payload)
    assert done["phase"] == "pr_pending", (done["blocked_reason"], counted.host.frames)
    assert done["work_status"] == "succeeded"
    names = budget(counted.host, 10)
    assert names == [
        "orchestrator_capabilities",
        "t3_thread_read",
        "t3_project_list",
        # before memory: the child, the filtered list, the host's own status
        "t3_thread_read",
        "t3_thread_list",
        "task_status",
        # after memory: the same three reads again, all fresh
        "t3_thread_read",
        "t3_thread_list",
        "task_status",
        "t3_thread_send",
    ]
    lists = [a for name, a in counted.host.calls if name == "t3_thread_list"]
    assert len(lists) == 2
    assert all("completed" not in a["statuses"] and a["limit"] <= 10 for a in lists)


async def closed_by_origin(counted, **overrides):
    task = await start(counted, **overrides)
    child = task["child"]["childThreadId"]
    counted.host.complete(child)
    return task, child


def descendant(host, parent, status, *, name="direct-child"):
    """A thread the child started directly, outside the ledger."""
    host.threads[name] = {
        "threadId": name,
        "projectId": "alpha",
        "parentThreadId": parent,
        "activeRunId": "run-" + name if status == "running" else None,
        "latestRunId": "run-" + name,
        "status": status,
        "title": "Direct descendant",
        "createdAt": host.stamp(),
        "updatedAt": host.stamp(),
    }


async def test_live_descendant_still_blocks_through_the_full_catalog(counted):
    task, child = await closed_by_origin(counted)
    descendant(counted.host, child, "running")
    counted.host.native_status[task["child"]["taskId"]]["hasPendingChildRuns"] = True
    result = await counted.engine.command(
        "finish", {"task_id": task["id"], "report": report(task)}
    )
    assert result["phase"] == "blocked"
    assert result["blocked_reason"] == "children_still_running"
    assert result["work_status"] == "pending"
    lists = [a for name, a in counted.host.calls if name == "t3_thread_list"]
    assert "statuses" in lists[0] and "statuses" not in lists[-1]
    assert result["recovery"]["command"] == "oraculo task resume"
    assert result["recovery"]["payload"] == {"task_id": task["id"]}


@pytest.mark.parametrize("status", ["failed", "interrupted", "cancelled"])
async def test_unsuccessful_direct_descendant_still_requires_review(counted, status):
    task, child = await closed_by_origin(counted)
    descendant(counted.host, child, status)
    result = await counted.engine.command(
        "finish", {"task_id": task["id"], "report": report(task)}
    )
    assert result["work_status"] != "succeeded"
    assert result["blocked_reason"] == "terminal_descendant_review_required"
    assert result["recovery"]["command"] == "oraculo task diagnose-review"


async def test_grandchild_is_traced_through_a_completed_parent(counted):
    task, child = await closed_by_origin(counted)
    descendant(counted.host, child, "completed", name="middle")
    descendant(counted.host, "middle", "running", name="leaf")
    counted.host.frames.clear()
    result = await counted.engine.command(
        "finish", {"task_id": task["id"], "report": report(task)}
    )
    assert result["blocked_reason"] == "children_still_running"
    # The completed parent is not listed; one small read links the leaf.
    reads = [a["threadId"] for n, a in counted.host.calls if n == "t3_thread_read"]
    assert "middle" in reads


async def test_ledger_child_of_the_child_uses_the_full_catalog(counted):
    task, child = await closed_by_origin(counted)
    counted.store.create(
        {"thread_id": child, "run_id": "child-run", "project_id": "alpha"},
        "nested",
        {"title": "Nested task", "text": "Nested"},
    )
    counted.host.calls.clear()
    result = await counted.engine.command(
        "finish", {"task_id": task["id"], "report": report(task)}
    )
    assert result["work_status"] == "succeeded"
    lists = [a for name, a in counted.host.calls if name == "t3_thread_list"]
    assert lists and all("statuses" not in a for a in lists)


async def test_shifted_page_fails_closed(counted):
    from oraculo.t3.client import T3Client
    from oraculo.t3.contracts import ContractError

    for number in range(8):
        descendant(counted.host, "origin", "running", name=f"busy-{number}")
    client = T3Client("http://127.0.0.1/mcp")
    original = counted.host.call

    async def shifting(name, args):
        result = await original(name, args)
        if name == "t3_thread_list" and args.get("cursor"):
            result["threads"][0]["threadId"] = "busy-7"  # already on page one
        return result

    client.call = shifting
    with pytest.raises(ContractError, match="Duplicate settlement thread identity"):
        await client.unclear_descendants("origin", "alpha", None, None)


async def test_request_travels_by_reference_and_replays_identically(counted):
    text = "Implement the bounded change. " * 400
    task = await start(counted, text=text)
    stored = counted.store.get(task["id"])
    arguments = stored["launch_arguments"]
    brief = json.loads(arguments["task"])
    assert "request" not in brief and text not in arguments["task"]
    reference = brief["request_artifact"]
    raw = open(reference["path"], "rb").read()
    assert raw == text.encode() and reference["bytes"] == len(raw)
    assert hashlib.sha256(raw).hexdigest() == reference["sha256"]
    assert "managed-contexts" in reference["path"]
    assert oct(os.stat(reference["path"]).st_mode & 0o777) == "0o600"
    assert brief["memory"]["sha256"] and brief["task_id"] == task["id"]
    assert "request_artifact.path" in brief["rules"][0]
    sent = [a for name, a in counted.host.calls if name == "delegate_task"]
    assert sent == [arguments]
    # Same key again: the adapter replays the stored bytes, nothing is rebuilt.
    again = await start(counted, text=text)
    assert again["id"] == task["id"]
    assert counted.store.get(task["id"])["launch_arguments"] == arguments


async def test_inline_brief_of_an_already_launched_task_is_replayed_as_stored(
    counted,
):
    counted.host.fail_delegate_once = True
    blocked = await start(counted)
    assert blocked["phase"] == "blocked" and blocked["launch_attempted"]
    # A task persisted by the previous version carries the request inline.
    task = counted.store.get(blocked["id"])
    brief = json.loads(task["launch_arguments"]["task"])
    del brief["request_artifact"]
    brief["request"] = task["request"]["text"]
    brief["rules"] = brief["rules"][1:]
    legacy = dict(task["launch_arguments"], task=canonical_json(brief))
    counted.store.update(
        blocked["id"],
        "blocked",
        {"fixture": "legacy_inline"},
        launch_arguments=legacy,
    )
    counted.host.calls.clear()
    resumed = await counted.engine.command("resume", {"task_id": blocked["id"]})
    assert resumed["phase"] == "executing", resumed
    assert [a for n, a in counted.host.calls if n == "delegate_task"] == [legacy]
