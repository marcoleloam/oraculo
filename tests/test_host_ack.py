"""Transport contract tests: no model or operational T3 service involved."""

import copy
import io
import json
import time

import pytest

from oraculo.cli import main
from oraculo.host_ack import ACK_PROTOCOL, HostResults, decode_ack, make_ack
from oraculo.state import StateError
from oraculo.task_transport import StdioHostTransport


def frame(tool="example", args=None):
    return dict(
        marker="ORACULO_HOST_CALL_V1",
        ack_protocol=ACK_PROTOCOL,
        request_id="unique-request",
        tool=tool,
        arguments=args or {},
        issued_at=time.time(),
    )


def snapshot():
    return dict(
        thread=dict(
            threadId="child",
            projectId="project",
            parentThreadId="origin",
            activeRunId=None,
            latestRunId="run",
            status="completed",
            pendingRequestCount=0,
            runCount=1,
            itemCount=1,
            updatedAt="2026-10-21T12:00:00Z",
            worktreePath="/fixture",
            branch="fixture",
            archived=False,
            settled=False,
        ),
        recentRuns=[
            dict(
                runId="run",
                ordinal=1,
                status="completed",
                startedAt="2026-10-21T11:00:00Z",
                completedAt="2026-10-21T12:00:00Z",
            )
        ],
        items=[],
        hasMore=False,
        nextPosition=None,
    )


def capabilities():
    return dict(
        parentThreadId="origin",
        inheritedProviderInstanceId="example",
        inheritedModel="model",
        runtimeMode="full-access",
        interactionMode="default",
        features={"appOwnedSubagents": True},
        providers=[
            dict(
                providerInstanceId="example",
                driverKind="example",
                models=[
                    dict(
                        id="model",
                        options=[
                            dict(id="effort", type="select", options=[dict(id="high")])
                        ],
                    )
                ],
                canRunChildTask=True,
                canRunCrossProviderChildTask=True,
                constraints=[],
            )
        ],
    )


def test_literal_compression_roundtrip_preserves_all_options_and_unknown_fields(
    tmp_path, capsys
):
    f = frame("orchestrator_capabilities")
    value = capabilities()
    value["futureHostField"] = {"nested": ["arbitrary", "ç", "😀"]}
    raw = json.dumps(
        dict(
            structuredContent=value, content=[dict(type="text", text=json.dumps(value))]
        ),
        indent=2,
    ).encode()
    (tmp_path / "frame.json").write_text(json.dumps(f))
    (tmp_path / "tool.json").write_bytes(raw)
    assert (
        main(
            [
                "bridge",
                "ack",
                "--frame",
                str(tmp_path / "frame.json"),
                "--from-file",
                str(tmp_path / "tool.json"),
            ]
        )
        == 0
    )
    ack = json.loads(capsys.readouterr().out)
    result = decode_ack(f, ack)
    assert result == json.loads(raw)
    assert HostResults().validate(f["tool"], {}, result) == value
    assert len(json.dumps(ack).encode()) < len(raw)


@pytest.mark.parametrize(
    "field",
    [
        "request_id",
        "tool",
        "arguments_sha256",
        "issued_at",
        "result_sha256",
        "result_zlib",
        "ack_protocol",
    ],
)
def test_ack_rejects_mixed_frames_or_modified_payload(field):
    f = frame()
    ack = make_ack(f, b'{"real":"value"}')
    ack[field] = "changed"
    with pytest.raises(StateError, match="native_bridge_ack_"):
        decode_ack(f, ack)


def test_stale_frame_and_old_protocol_refused():
    f = frame()
    f["issued_at"] -= 601
    with pytest.raises(StateError, match="stale_frame"):
        make_ack(f, b"{}")
    with pytest.raises(StateError, match="correlation"):
        decode_ack(frame(), dict(request_id="unique-request", result={}))


@pytest.mark.parametrize("missing", ["providers", "features", "options", "choice"])
def test_incomplete_capabilities_cannot_authorize_model(missing):
    value = capabilities()
    if missing in ("providers", "features"):
        value.pop(missing)
    elif missing == "options":
        value["providers"][0]["models"][0].pop("options")
    else:
        value["providers"][0]["models"][0]["options"][0].pop("options")
    with pytest.raises(StateError, match="incomplete"):
        make_ack(frame("orchestrator_capabilities"), json.dumps(value).encode())


def test_disagreeing_mcp_representations_rejected():
    f = frame()
    with pytest.raises(StateError, match="incoherent"):
        make_ack(
            f,
            b'{"structuredContent":{"a":1},"content":[{"type":"text","text":"{\\"a\\":2}"}]}',
        )


@pytest.mark.parametrize(
    "defect",
    [
        "missing",
        "wrong_id",
        "bad_count",
        "duplicate",
        "stale",
        "revision",
        "terminal_edited",
        "parent",
        "position",
    ],
)
def test_thread_result_shape_and_cross_frame_consistency(defect):
    state = HostResults()
    first = snapshot()
    state.validate("t3_thread_read", {"threadId": "child"}, first)
    second = copy.deepcopy(first)
    if defect == "missing":
        second["thread"].pop("activeRunId")
    elif defect == "wrong_id":
        second["thread"]["threadId"] = "another"
    elif defect == "bad_count":
        second["thread"]["runCount"] = 0
    elif defect == "duplicate":
        second["recentRuns"].append(copy.deepcopy(second["recentRuns"][0]))
    elif defect == "stale":
        second["thread"]["updatedAt"] = "2026-10-20T12:00:00Z"
    elif defect == "revision":
        second["thread"]["itemCount"] = 8
    elif defect == "terminal_edited":
        second["recentRuns"][0]["completedAt"] = "2026-10-21T12:01:00Z"
    elif defect == "parent":
        second["thread"]["parentThreadId"] = "other"
    else:
        second["items"] = [
            dict(
                itemId="i",
                sourceThreadId="child",
                runId="run",
                position=2,
                text="x",
                textTruncated=False,
            )
        ]
    with pytest.raises(StateError, match="native_bridge_"):
        state.validate("t3_thread_read", {"threadId": "child"}, second)


def test_paginated_catalog_missing_page_or_wrong_total_fails_closed():
    state = HostResults()
    row = dict(
        threadId="child",
        parentThreadId=None,
        status="completed",
        latestRunId="r",
        itemCount=1,
        updatedAt="2026-10-21T00:00:00Z",
    )
    state.validate(
        "t3_thread_list",
        {"projectId": "p"},
        dict(projectId="p", threads=[row], nextCursor=1, total=2),
    )
    with pytest.raises(StateError, match="catalog_incomplete"):
        state.validate(
            "t3_thread_list",
            {"projectId": "p", "cursor": 1},
            dict(projectId="p", threads=[], nextCursor=None, total=2),
        )


def test_status_cannot_claim_quiescence_after_active_thread_read():
    state = HostResults()
    read = snapshot()
    read["thread"].update(activeRunId="run", status="running")
    read["recentRuns"][0].update(status="running", completedAt=None)
    state.validate("t3_thread_read", {"threadId": "child"}, read)
    with pytest.raises(StateError, match="incoherent"):
        state.validate(
            "task_status",
            {"taskId": "task"},
            dict(
                taskId="task",
                childThreadId="child",
                childRunId="run",
                status="completed",
                workState="result_available",
                hasPendingChildRuns=False,
                hasPendingQuestion=False,
                latestTerminalRunId="run",
                latestTerminalStatus="completed",
            ),
        )


async def test_stdio_uses_actual_current_frame_and_validates_unwrapped_result():
    writer = io.StringIO()

    class Reader:
        def readline(self):
            f = json.loads(writer.getvalue())
            return json.dumps(make_ack(f, json.dumps(snapshot()).encode()))

    assert (
        await StdioHostTransport(Reader(), writer).call(
            "t3_thread_read", {"threadId": "child"}
        )
    ) == snapshot()


def test_from_file_rejects_capture_older_than_frame(tmp_path, capsys):
    import os

    f = frame()
    (tmp_path / "frame").write_text(json.dumps(f))
    p = tmp_path / "result"
    p.write_text("{}")
    os.utime(p, (f["issued_at"] - 10, f["issued_at"] - 10))
    assert (
        main(
            ["bridge", "ack", "--frame", str(tmp_path / "frame"), "--from-file", str(p)]
        )
        != 0
    )
    assert "native_bridge_stale_result_file" in capsys.readouterr().err


@pytest.mark.parametrize("defect", ["feature", "duplicate", "selection"])
def test_capability_selection_consistency(defect):
    value = capabilities()
    option = value["providers"][0]["models"][0]["options"][0]
    if defect == "feature":
        value["features"].clear()
    elif defect == "duplicate":
        option["options"].append(dict(id="high"))
    else:
        option["currentValue"] = "not-an-option"
    with pytest.raises(StateError, match="native_bridge_"):
        HostResults().validate("orchestrator_capabilities", {}, value)


def test_file_helper_large_ack_survives_nonblocking_stdout(tmp_path):
    import secrets
    import subprocess
    import sys

    f = frame()
    frame_path, raw_path = tmp_path / "frame", tmp_path / "raw"
    frame_path.write_text(json.dumps(f))
    raw = json.dumps({"payload": secrets.token_hex(180000)}).encode()
    raw_path.write_bytes(raw)
    script = """import fcntl,os,sys
from oraculo.cli import main
fcntl.fcntl(1, fcntl.F_SETFL, fcntl.fcntl(1, fcntl.F_GETFL) | os.O_NONBLOCK)
raise SystemExit(main())
"""
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            script,
            "bridge",
            "ack",
            "--frame",
            str(frame_path),
            "--from-file",
            str(raw_path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        # Read in small blocks: the large encoded ACK must traverse pipe backpressure.
        chunks = []
        while chunk := process.stdout.read(1024):
            chunks.append(chunk)
            time.sleep(0.0005)
        assert process.wait(timeout=5) == 0, process.stderr.read()
        encoded = b"".join(chunks)
        assert len(encoded) > 100000
        assert decode_ack(f, json.loads(encoded)) == json.loads(raw)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
