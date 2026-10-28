"""Steering may leave an older run active while a newer run is cancelled."""

import json

import pytest
from test_host_ack import frame, snapshot

from oraculo.host_ack import HostResults, decode_ack, make_ack
from oraculo.run_continuity import terminal_chain
from oraculo.state import StateError


def steered_snapshot(include_active=False):
    value = snapshot()
    value["thread"].update(
        activeRunId="active",
        latestRunId="cancelled-steer",
        runCount=2,
        status="running",
    )
    latest = dict(
        value["recentRuns"][0],
        runId="cancelled-steer",
        ordinal=2,
        status="cancelled",
        startedAt=None,
    )
    value["recentRuns"] = [latest]
    if include_active:
        value["recentRuns"].append(
            dict(latest, runId="active", ordinal=1, status="running", completedAt=None)
        )
    return value


@pytest.mark.parametrize("include_active", [False, True])
def test_literal_ack_allows_active_run_older_than_latest(include_active):
    value = steered_snapshot(include_active)
    request = frame("t3_thread_read", {"threadId": "child", "runLimit": 1})
    ack = make_ack(request, json.dumps(value).encode())
    assert (
        HostResults().validate(
            request["tool"], request["arguments"], decode_ack(request, ack)
        )
        == value
    )
    with pytest.raises(StateError, match="terminal_run_chain_incomplete"):
        terminal_chain(value)


@pytest.mark.parametrize("status", ["completed", "cancelled", "failed", "interrupted"])
def test_literal_ack_rejects_active_pointer_to_observed_terminal_run(status):
    value = steered_snapshot(include_active=True)
    value["recentRuns"][-1]["status"] = status
    with pytest.raises(StateError, match="native_bridge_result_incoherent"):
        HostResults().validate("t3_thread_read", {"threadId": "child"}, value)


@pytest.mark.parametrize("active", ["", 7, False])
def test_literal_ack_rejects_invalid_active_identity(active):
    value = steered_snapshot()
    value["thread"]["activeRunId"] = active
    with pytest.raises(StateError, match="native_bridge_result_incoherent"):
        HostResults().validate("t3_thread_read", {"threadId": "child"}, value)
