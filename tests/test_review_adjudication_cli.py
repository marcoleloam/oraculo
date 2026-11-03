"""Exercise both review commands through real CLI, FIFO, daemon and memory.

Only the caller-bound host is synthetic; human messages are not real reviews.
"""

import pytest
from test_review_adjudication import flow, human_evidence, interrupted  # noqa: F401
from test_stdio_fifo import run_cli_fifo


@pytest.mark.parametrize("action", ["diagnose-review", "adjudicate-review"])
async def test_review_cli_roundtrip_and_replay(flow, tmp_path, action):  # noqa: F811
    task, evidence, _ = await interrupted(flow)
    # The API fixture's synthetic descendant omits optional display metadata;
    # the actual host bridge correctly requires those keys in every read.
    flow.host.threads["failed-review"].update(worktreePath=None, branch=None)
    payload = {"task_id": task["id"]}
    if action == "adjudicate-review":
        payload, _, messages = await human_evidence(flow, task, evidence)
        for item in messages.values():
            item.update(position=0, runId=None)
    before = flow.snapshot(task)
    code, result, sizes, stderr = await run_cli_fifo(
        flow, tmp_path, action, payload, pad_hex=14000, minimum=8192
    )
    assert code == 0, (result, stderr)
    assert result["source"] == "service"
    assert result["id"] == task["id"]
    assert sizes
    if action == "diagnose-review":
        diagnosis = result["review_diagnosis"]
        assert diagnosis["reason"] == "terminal_descendant_review_required"
        assert diagnosis["certification"] == "not_granted_by_diagnosis"
        assert flow.snapshot(task) == before
    else:
        assert result["phase"] == "pr_pending"
        assert result["work_status"] == "succeeded"
        assert result["memory"]["write_receipt"]["state"] == "confirmed"
        assert result["publication"] is None
        assert flow.service.state.capacity()["occupied"] == 0
        assert flow.host.threads["failed-review"]["status"] == "failed"
        after = flow.snapshot(task)
        code, replay, _, stderr = await run_cli_fifo(
            flow, tmp_path, action, payload, pad_hex=14000, minimum=8192
        )
        assert code == 0, (replay, stderr)
        assert replay["phase"] == "pr_pending"
        assert flow.snapshot(task) == after
