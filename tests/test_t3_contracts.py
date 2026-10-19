import copy
import json
from pathlib import Path

import pytest

from oraculo.t3.client import MutationUncertain, T3Client
from oraculo.t3.contracts import REQUIRED_TOOLS, ContractError, execution_terminal


@pytest.fixture
def contract():
    return json.loads(
        (Path(__file__).parent / "fixtures/t3_contracts.json").read_text()
    )


class Session:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    async def list_tools(self, params=None):
        return {
            "tools": [
                {"name": name, "inputSchema": {"type": "object"}}
                for name in REQUIRED_TOOLS
            ]
        }

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        response = next(self.responses)
        if isinstance(response, BaseException):
            raise response
        return {"structuredContent": copy.deepcopy(response)}


def test_old_run_does_not_complete_current(contract):
    snapshot = contract["snapshot"]
    assert not execution_terminal(snapshot, "run-current")
    snapshot["thread"]["activeRunId"] = None
    assert not execution_terminal(snapshot, "run-current")
    snapshot["recentRuns"][1]["status"] = "completed"
    snapshot["thread"]["status"] = "completed"
    assert execution_terminal(snapshot, "run-current")
    snapshot["hasPendingChildRuns"] = True
    assert not execution_terminal(snapshot, "run-current")
    snapshot.pop("hasPendingChildRuns")
    snapshot["thread"]["pendingRequestCount"] = 1
    assert not execution_terminal(snapshot, "run-current")


async def test_utf16_pagination_completes_before_cursor(contract):
    session = Session([contract["snapshot"], contract["continuation"]])
    async with T3Client("http://127.0.0.1/mcp", session=session) as client:
        result = await client.read_thread("thread-1")
    assert session.calls[1][1]["textOffset"] == 3
    assert result["items"][0]["text"] == "A😀B"
    assert result["nextPosition"] == 1
    assert not result["items"][0]["textTruncated"]


async def test_partial_item_never_commits_cursor(contract):
    continuation = contract["continuation"]
    continuation["items"][0]["updatedAt"] = "later"
    async with T3Client(
        "http://127.0.0.1/mcp", session=Session([contract["snapshot"], continuation])
    ) as client:
        with pytest.raises(ContractError, match="changed"):
            await client.read_thread("thread-1")


async def test_mutation_transport_failure_is_not_retried():
    session = Session([TimeoutError("connection lost")])
    async with T3Client("http://127.0.0.1/mcp", session=session) as client:
        with pytest.raises(MutationUncertain):
            await client.launch(
                project_id="p",
                title="marker",
                message="goal",
                model_selection={"model": "m", "instanceId": "codex"},
                workspace_strategy={
                    "type": "worktree",
                    "baseRef": "main",
                    "startFromOrigin": False,
                },
            )
    assert len(session.calls) == 1
    assert session.calls[0][1]["workspaceStrategy"]["type"] == "worktree"
    assert "prompt" not in session.calls[0][1]


async def test_project_pages_detect_repeated_cursor():
    session = Session([{"projects": [], "nextCursor": 1}] * 2)
    async with T3Client("http://127.0.0.1/mcp", session=session) as client:
        with pytest.raises(ContractError, match="cursor"):
            await client.list_projects()


async def test_missing_tool_fails_startup():
    session = Session([])

    async def empty(params=None):
        return {"tools": []}

    session.list_tools = empty
    with pytest.raises(ContractError, match="missing"):
        async with T3Client("http://127.0.0.1/mcp", session=session):
            pass


async def test_auth_exception_group_preserves_auth_required():
    from oraculo.t3.auth import AuthRequired

    session = Session(
        [ExceptionGroup("transport taskgroup", [AuthRequired("login required")])]
    )
    async with T3Client("http://127.0.0.1/mcp", session=session) as client:
        with pytest.raises(AuthRequired):
            await client.capabilities()


async def test_read_respects_live_server_run_limit(contract):
    session = Session([contract["snapshot"], contract["continuation"]])
    async with T3Client("http://127.0.0.1/mcp", session=session) as client:
        client.tools["t3_thread_read"]["inputSchema"] = contract["readInputSchema"]
        result = await client.read_thread("thread-1")
    assert session.calls[0][1]["runLimit"] <= 50
    assert result["items"][0]["text"] == "A😀B"


async def test_invalid_input_identifies_tool_without_echoing_payload():
    session = Session([])
    async with T3Client("http://127.0.0.1/mcp", session=session) as client:
        client.tools["t3_thread_send"]["inputSchema"] = {
            "type": "object",
            "properties": {"message": {"type": "integer"}},
        }
        with pytest.raises(ContractError) as caught:
            await client.send("thread", "private message", "stable-request")
    assert caught.value.tool == "t3_thread_send"
    assert caught.value.direction == "input"
    assert "private message" not in str(caught.value)
    assert session.calls == []


async def test_invalid_output_identifies_tool_without_echoing_payload():
    session = Session([{"providers": "private response"}])
    async with T3Client("http://127.0.0.1/mcp", session=session) as client:
        client.tools["orchestrator_capabilities"]["outputSchema"] = {
            "type": "object",
            "properties": {"providers": {"type": "array"}},
        }
        with pytest.raises(ContractError) as caught:
            await client.capabilities()
    assert caught.value.tool == "orchestrator_capabilities"
    assert caught.value.direction == "output"
    assert "private response" not in str(caught.value)
