"""A later queued cancellation can finish before the older executing run."""

import pytest
from test_host_ack import snapshot

from oraculo.host_ack import HostResults
from oraculo.state import StateError


def observed(window=50):
    read = snapshot()
    read["thread"].update(runCount=2, status="cancelled", latestRunId="queued-cancel")
    read["recentRuns"].append(
        dict(
            runId="queued-cancel",
            ordinal=2,
            status="cancelled",
            startedAt=None,
            requestedAt="2026-10-21T11:30:00Z",
            completedAt="2026-10-21T11:30:01Z",
        )
    )
    if window == 1:
        read["recentRuns"] = read["recentRuns"][-1:]
    native = dict(
        taskId="task",
        childThreadId="child",
        childRunId="run",
        status="completed",
        workState="result_available",
        hasPendingChildRuns=False,
        hasPendingQuestion=False,
        latestTerminalRunId="run",
        latestTerminalStatus="completed",
    )
    return read, native


@pytest.mark.parametrize("window", [1, 50])
def test_native_completion_uses_completion_time_after_unstarted_cancel(window):
    read, native = observed(window)
    validator = HostResults()
    validator.validate(
        "t3_thread_read", {"threadId": "child", "runLimit": window}, read
    )
    assert validator.validate("task_status", {"taskId": "task"}, native) == native


@pytest.mark.parametrize(
    "change", ["wrong_id", "ordinal_instead_of_completion", "wrong_status"]
)
def test_complete_history_rejects_inconsistent_native_terminal_result(change):
    read, native = observed()
    if change == "wrong_id":
        native["latestTerminalRunId"] = "unrelated"
    elif change == "ordinal_instead_of_completion":
        native.update(
            latestTerminalRunId="queued-cancel", latestTerminalStatus="cancelled"
        )
    else:
        native["latestTerminalStatus"] = "failed"
    validator = HostResults()
    validator.validate("t3_thread_read", {"threadId": "child", "runLimit": 50}, read)
    with pytest.raises(StateError, match="incoherent"):
        validator.validate("task_status", {"taskId": "task"}, native)
