import copy

import pytest

from oraculo.t3.client import MutationUncertain, T3Client, TransportError
from oraculo.t3.contracts import REQUIRED_TOOLS, ContractError

OPTIONAL_TOOLS = {
    "list_thread_pull_requests",
    "link_pull_request",
    "t3_thread_organize",
}


class Session:
    def __init__(self, responses, *, optional=True):
        self.responses = iter(responses)
        self.calls = []
        self.names = REQUIRED_TOOLS | (OPTIONAL_TOOLS if optional else set())

    async def list_tools(self, params=None):
        return {
            "tools": [
                {"name": name, "inputSchema": {"type": "object"}} for name in self.names
            ]
        }

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        response = next(self.responses)
        if isinstance(response, BaseException):
            raise response
        return {"structuredContent": copy.deepcopy(response)}


def state(thread_id="target", parent=None, **updates):
    thread = {
        "threadId": thread_id,
        "projectId": "project",
        "parentThreadId": parent,
        "activeRunId": None,
        "latestRunId": "run-" + thread_id,
        "status": "completed",
        "pendingRequestCount": 0,
        "settled": False,
    }
    thread.update(updates)
    return {
        "thread": thread,
        "recentRuns": [{"runId": "run-" + thread_id, "status": "completed"}],
        "items": [],
        "hasMore": False,
        "nextPosition": None,
    }


def page(*snapshots, cursor=None):
    return {
        "projectId": "project",
        "threads": [
            {
                key: snapshot["thread"][key]
                for key in ("threadId", "parentThreadId", "status")
            }
            for snapshot in snapshots
        ],
        "nextCursor": cursor,
    }


async def test_optional_tools_do_not_change_legacy_startup():
    session = Session([], optional=False)
    async with T3Client("http://127.0.0.1/mcp", session=session) as client:
        for operation in (
            client.list_thread_pull_requests("target"),
            client.link_pull_request("target", {"url": "https://example.org/pull/1"}),
            client.settle_thread("target"),
        ):
            with pytest.raises(ContractError, match="Unavailable"):
                await operation
    assert not session.calls
    assert not REQUIRED_TOOLS & OPTIONAL_TOOLS


@pytest.mark.parametrize(
    ("operation", "exception"),
    [
        ("list_thread_pull_requests", TransportError),
        ("settle_thread", MutationUncertain),
    ],
)
async def test_optional_transport_loss_classification(operation, exception):
    session = Session([TimeoutError("response lost")])
    async with T3Client("http://127.0.0.1/mcp", session=session) as client:
        with pytest.raises(exception) as caught:
            await getattr(client, operation)("target")
    assert type(caught.value) is exception
    assert len(session.calls) == 1


async def test_settle_receipt_stays_deferred_and_does_not_claim_observation():
    receipt = {"accepted": True, "settlesWhenTurnEnds": True}
    session = Session([receipt])
    async with T3Client("http://127.0.0.1/mcp", session=session) as client:
        assert await client.settle_thread("target") == receipt
    assert session.calls == [
        ("t3_thread_organize", {"action": "settle", "threadId": "target"})
    ]


async def test_mutation_preflight_validates_schema_without_remote_effect():
    session = Session([], optional=False)
    async with T3Client("http://127.0.0.1/mcp", session=session) as client:
        with pytest.raises(ContractError, match="Unavailable"):
            client.validate_settle("target")
        client.tools["t3_thread_organize"] = {
            "name": "t3_thread_organize",
            "inputSchema": {"type": "object", "required": ["newRequiredField"]},
        }
        with pytest.raises(ContractError, match="input schema mismatch"):
            client.validate_settle("target")
        client.tools["t3_thread_organize"]["inputSchema"] = {"type": "object"}
        client.validate_settle("target")
    assert not session.calls


async def test_link_uses_explicit_target_and_only_pr_identity():
    session = Session([{"alreadyLinked": True}])
    async with T3Client("http://127.0.0.1/mcp", session=session) as client:
        await client.link_pull_request(
            "target",
            {
                "host": "github.com",
                "repository": "example/product",
                "number": 17,
                "threadId": "wrong",
                "private_notes": "must not transmit",
            },
        )
    assert session.calls == [
        (
            "link_pull_request",
            {
                "host": "github.com",
                "repository": "example/product",
                "number": 17,
                "threadId": "target",
            },
        )
    ]


async def test_optional_schema_change_fails_before_mutation():
    session = Session([])
    async with T3Client("http://127.0.0.1/mcp", session=session) as client:
        client.tools["t3_thread_organize"]["inputSchema"] = {
            "type": "object",
            "properties": {"action": {"enum": ["pin"]}},
        }
        with pytest.raises(ContractError, match="input schema mismatch"):
            await client.settle_thread("target")
    assert not session.calls


async def test_snapshot_recursively_observes_children_and_refreshes_target():
    root = state()
    child = state("child", "target")
    grandchild = state("grandchild", "child")
    current = state(settled=True)
    session = Session([root, page(root, child, grandchild), child, grandchild, current])
    async with T3Client("http://127.0.0.1/mcp", session=session) as client:
        snapshot = await client.settlement_snapshot("target")
    assert snapshot["thread"]["settled"] is True
    assert snapshot["hasPendingChildRuns"] is False
    assert snapshot["childrenObserved"] is True
    assert snapshot["observedChildCount"] == 2
    assert snapshot["childrenScope"] == "project"
    assert session.calls[1][1] == {
        "projectId": "project",
        "includeSubagents": True,
        "limit": 100,
    }
    assert not any(name in OPTIONAL_TOOLS for name, _ in session.calls)


@pytest.mark.parametrize(
    "updates",
    [
        {"status": "running", "activeRunId": "child-run"},
        {"status": "failed"},
        {"status": "interrupted"},
        {"status": "cancelled"},
        {"status": "rolled_back"},
        {"status": "unknown"},
        {"pendingRequestCount": 1},
        {"latestRunId": "missing"},
    ],
)
async def test_unresolved_descendant_blocks_snapshot(updates):
    root = state()
    child = state("child", "target", **updates)
    session = Session([root, page(root, child), child, root])
    async with T3Client("http://127.0.0.1/mcp", session=session) as client:
        snapshot = await client.settlement_snapshot("target")
    assert snapshot["hasPendingChildRuns"] is True


async def test_idle_child_without_runs_is_clear():
    root = state()
    child = state("child", "target", status="idle", latestRunId=None)
    child["recentRuns"] = []
    session = Session([root, page(root, child), child, root])
    async with T3Client("http://127.0.0.1/mcp", session=session) as client:
        assert not (await client.settlement_snapshot("target"))["hasPendingChildRuns"]


@pytest.mark.parametrize("field", ["settled", "projectId", "parentThreadId"])
async def test_snapshot_rejects_missing_optional_read_evidence(field):
    root = state()
    del root["thread"][field]
    session = Session([root])
    async with T3Client("http://127.0.0.1/mcp", session=session) as client:
        with pytest.raises(ContractError, match="settlement read"):
            await client.settlement_snapshot("target")


@pytest.mark.parametrize(
    ("field", "value"),
    [("settled", "true"), ("pendingRequestCount", True), ("projectId", "")],
)
async def test_snapshot_rejects_coerced_state(field, value):
    root = state(**{field: value})
    session = Session([root])
    async with T3Client("http://127.0.0.1/mcp", session=session) as client:
        with pytest.raises(ContractError, match="settlement read"):
            await client.settlement_snapshot("target")


async def test_snapshot_rejects_wrong_identity():
    session = Session([state("wrong")])
    async with T3Client("http://127.0.0.1/mcp", session=session) as client:
        with pytest.raises(ContractError, match="identity"):
            await client.settlement_snapshot("target")


@pytest.mark.parametrize(
    "issue", ["project", "parent", "missing", "duplicate", "cursor"]
)
async def test_snapshot_rejects_incomplete_or_malformed_tree(issue):
    root = state()
    listed = page(root)
    if issue == "project":
        listed["projectId"] = "wrong"
    elif issue == "parent":
        del listed["threads"][0]["parentThreadId"]
    elif issue == "missing":
        listed["threads"] = []
    elif issue == "duplicate":
        listed["threads"] *= 2
    else:
        listed["nextCursor"] = "unrecognized"
    session = Session([root, listed])
    async with T3Client("http://127.0.0.1/mcp", session=session) as client:
        with pytest.raises(ContractError):
            await client.settlement_snapshot("target")


@pytest.mark.parametrize("change", ["projectId", "parentThreadId"])
async def test_snapshot_rejects_child_scope_change(change):
    root = state()
    child = state("child", "target")
    changed = copy.deepcopy(child)
    changed["thread"][change] = "another"
    session = Session([root, page(root, child), changed])
    async with T3Client("http://127.0.0.1/mcp", session=session) as client:
        with pytest.raises(ContractError):
            await client.settlement_snapshot("target")


async def test_final_target_read_exposes_new_work_instead_of_old_idle_state():
    root = state()
    current = state(status="running", activeRunId="new-run", pendingRequestCount=1)
    session = Session([root, page(root), current])
    async with T3Client("http://127.0.0.1/mcp", session=session) as client:
        snapshot = await client.settlement_snapshot("target")
    assert snapshot["thread"]["activeRunId"] == "new-run"
    assert snapshot["thread"]["pendingRequestCount"] == 1


async def test_snapshot_rejects_cyclic_parent_links():
    root = state(parent="child")
    child = state("child", "target")
    session = Session([root, page(root, child), child])
    async with T3Client("http://127.0.0.1/mcp", session=session) as client:
        with pytest.raises(ContractError, match="Cyclic"):
            await client.settlement_snapshot("target")


async def test_descendant_reads_have_a_hard_bound():
    root = state()
    children = [state(f"child-{i}", "target") for i in range(101)]
    session = Session(
        [
            root,
            page(root, *children[:99], cursor=100),
            page(*children[99:]),
            *reversed(children),
        ]
    )
    async with T3Client("http://127.0.0.1/mcp", session=session) as client:
        with pytest.raises(ContractError, match="descendant limit"):
            await client.settlement_snapshot("target")
    assert len([name for name, _ in session.calls if name == "t3_thread_read"]) == 101


async def test_project_pages_have_a_hard_bound():
    root = state()
    pages = [page(root, cursor=1)]
    pages.extend(page(cursor=i) for i in range(2, 11))
    session = Session([root, *pages])
    async with T3Client("http://127.0.0.1/mcp", session=session) as client:
        with pytest.raises(ContractError, match="page limit"):
            await client.settlement_snapshot("target")
    assert len(session.calls) == 11


async def test_repeated_cursor_fails_without_looping():
    root = state()
    session = Session([root, page(root, cursor=10), page(cursor=10)])
    async with T3Client("http://127.0.0.1/mcp", session=session) as client:
        with pytest.raises(ContractError, match="Repeated"):
            await client.settlement_snapshot("target")
    assert len(session.calls) == 3


async def test_missing_descendant_read_proof_is_not_inferred_from_list():
    root = state()
    child = state("child", "target")
    unreadable = copy.deepcopy(child)
    del unreadable["thread"]["pendingRequestCount"]
    session = Session([root, page(root, child), unreadable])
    async with T3Client("http://127.0.0.1/mcp", session=session) as client:
        with pytest.raises(ContractError, match="settlement read"):
            await client.settlement_snapshot("target")
