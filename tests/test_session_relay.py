"""`bridge relay`: literal results from a synthetic Claude Code session log.

Every fixture is generated here. No model, host or real session is involved.
"""

import hashlib
import json
import os
import time
from datetime import datetime, timezone

import pytest

from oraculo.cli import main
from oraculo.host_ack import ACK_PROTOCOL, decode_ack
from oraculo.session_relay import resolve_session_log
from oraculo.state import StateError

SESSION = "11111111-2222-4333-8444-555555555555"
ARGS = {"threadId": "thread-example", "text": "hello"}


def stamp(moment):
    return (
        datetime.fromtimestamp(moment, timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def make_frame(tmp_path, tool="t3_thread_send", arguments=None, age=5.0):
    frame = dict(
        marker="ORACULO_HOST_CALL_V1",
        ack_protocol=ACK_PROTOCOL,
        request_id="request-example",
        tool=tool,
        arguments=ARGS if arguments is None else arguments,
        issued_at=time.time() - age,
    )
    path = tmp_path / "frame.json"
    path.write_text(json.dumps(frame))
    return frame, path


def use(call_id, name, arguments, moment=None):
    entry = dict(
        type="assistant",
        sessionId=SESSION,
        message=dict(
            role="assistant",
            content=[
                dict(type="text", text="calling"),
                dict(type="tool_use", id=call_id, name=name, input=arguments),
            ],
        ),
    )
    if moment is not None:
        entry["timestamp"] = stamp(moment)
    return entry


def result(call_id, body, moment=None, **extra):
    entry = dict(
        type="user",
        sessionId=SESSION,
        message=dict(
            role="user",
            content=[
                dict(type="tool_result", tool_use_id=call_id, content=body) | extra
            ],
        ),
    )
    if moment is not None:
        entry["timestamp"] = stamp(moment)
    return entry


def write_log(tmp_path, entries, name=SESSION):
    path = tmp_path / f"{name}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(e) + "\n" for e in entries))
    return path


def sent(message="message-1"):
    return json.dumps({"threadId": ARGS["threadId"], "messageId": message})


def relay(frame_path, log, *extra):
    return main(
        [
            "bridge",
            "relay",
            "--frame",
            str(frame_path),
            "--session-log",
            str(log),
            "--wait",
            "0",
            *extra,
        ]
    )


def test_relays_literal_result_of_caller_bound_call(tmp_path, capsys):
    frame, frame_path = make_frame(tmp_path)
    literal = sent()
    log = write_log(
        tmp_path,
        [
            use("call-1", "mcp__t3-code__t3_thread_send", ARGS, frame["issued_at"] + 1),
            result("call-1", [dict(type="text", text=literal)], frame["issued_at"] + 2),
        ],
    )
    assert relay(frame_path, log) == 0
    out = capsys.readouterr().out
    ack = json.loads(out)
    assert out.endswith("\n") and out.count("\n") == 1
    assert ack["result_sha256"] == hashlib.sha256(literal.encode()).hexdigest()
    assert decode_ack(frame, ack) == json.loads(literal)


def test_same_line_as_bridge_ack_from_file(tmp_path, capsys):
    frame, frame_path = make_frame(tmp_path)
    literal = sent()
    log = write_log(
        tmp_path,
        [
            use("call-1", "mcp__t3-code__t3_thread_send", ARGS, frame["issued_at"] + 1),
            result("call-1", literal, frame["issued_at"] + 2),
        ],
    )
    saved = tmp_path / "result.json"
    saved.write_text(literal)
    assert relay(frame_path, log) == 0
    relayed = capsys.readouterr().out
    assert (
        main(["bridge", "ack", "--frame", str(frame_path), "--from-file", str(saved)])
        == 0
    )
    assert capsys.readouterr().out == relayed


def test_duplicate_entries_without_timestamp_use_the_dated_record(tmp_path, capsys):
    frame, frame_path = make_frame(tmp_path)
    name = "mcp__t3-code__t3_thread_send"
    log = write_log(
        tmp_path,
        [
            use("call-1", name, ARGS),
            use("call-1", name, ARGS, frame["issued_at"] + 1),
            result("call-1", sent(), frame["issued_at"] + 2),
            result("call-1", sent()),
            use("call-1", name, ARGS),
        ],
    )
    assert relay(frame_path, log) == 0
    captured = capsys.readouterr()
    assert decode_ack(frame, json.loads(captured.out))["messageId"] == "message-1"
    assert captured.err == ""


def test_entry_without_any_established_time_is_refused(tmp_path, capsys):
    frame, frame_path = make_frame(tmp_path)
    log = write_log(
        tmp_path,
        [
            use("call-1", "mcp__t3-code__t3_thread_send", ARGS),
            # A dated result does not date the call that produced it.
            result("call-1", sent(), frame["issued_at"] + 2),
        ],
    )
    assert relay(frame_path, log) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "relay_result_not_found" in captured.err
    assert "without an established time: 1" in captured.err


def test_stale_earlier_call_with_same_arguments_is_refused(tmp_path, capsys):
    frame, frame_path = make_frame(tmp_path)
    log = write_log(
        tmp_path,
        [
            use(
                "call-old", "mcp__t3-code__t3_thread_send", ARGS, frame["issued_at"] - 3
            ),
            result("call-old", sent("old-message"), frame["issued_at"] + 1),
        ],
    )
    assert relay(frame_path, log) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err.splitlines()[-1])["message"] == (
        "relay_result_not_found"
    )
    assert "older calls refused: 1" in captured.err


def test_fresh_call_wins_over_stale_one_and_earliest_fresh_is_used(tmp_path, capsys):
    frame, frame_path = make_frame(tmp_path)
    name = "mcp__t3-code__t3_thread_send"
    log = write_log(
        tmp_path,
        [
            use("call-old", name, ARGS, frame["issued_at"] - 60),
            result("call-old", sent("old-message"), frame["issued_at"] - 59),
            use("call-late", name, ARGS, frame["issued_at"] + 3),
            result("call-late", sent("late-message"), frame["issued_at"] + 4),
            use("call-first", name, ARGS, frame["issued_at"] + 1),
            result("call-first", sent("first-message"), frame["issued_at"] + 2),
        ],
    )
    assert relay(frame_path, log) == 0
    captured = capsys.readouterr()
    assert decode_ack(frame, json.loads(captured.out))["messageId"] == "first-message"
    assert "2 fresh matching calls" in captured.err
    assert "call-first" in captured.err


@pytest.mark.parametrize(
    "name,arguments",
    [
        ("mcp__t3__t3_thread_send", ARGS),  # Not the caller-bound server.
        ("mcp__t3-code__t3_thread_send", dict(ARGS, text="other")),
        ("mcp__t3-code__t3_thread_read", ARGS),
        ("mcp__other__t3_thread_send", ARGS),
    ],
)
def test_other_server_tool_or_arguments_never_match(tmp_path, capsys, name, arguments):
    frame, frame_path = make_frame(tmp_path)
    log = write_log(
        tmp_path,
        [
            use("call-1", name, arguments, frame["issued_at"] + 1),
            result("call-1", sent(), frame["issued_at"] + 2),
        ],
    )
    assert relay(frame_path, log) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and "relay_result_not_found" in captured.err
    if name.startswith("mcp__t3__"):
        assert "non caller-bound mcp__t3__ server: 1" in captured.err


def test_argument_key_order_is_irrelevant_but_values_are_exact(tmp_path, capsys):
    frame, frame_path = make_frame(tmp_path)
    reordered = dict(reversed(list(ARGS.items())))
    log = write_log(
        tmp_path,
        [
            use(
                "call-1",
                "mcp__t3-code__t3_thread_send",
                reordered,
                frame["issued_at"] + 1,
            ),
            result("call-1", sent(), frame["issued_at"] + 2),
        ],
    )
    assert relay(frame_path, log) == 0
    assert json.loads(capsys.readouterr().out)["request_id"] == "request-example"


def thread_list(rows, pad):
    return dict(
        projectId="project-example",
        currentThreadId="thread-example",
        threads=[
            dict(
                threadId=f"thread-{index}",
                parentThreadId=None,
                status="completed",
                latestRunId=f"run-{index}",
                itemCount=index,
                updatedAt="2026-01-01T00:00:00Z",
                title="synthetic " + "x" * pad,
            )
            for index in range(rows)
        ],
        nextCursor=None,
        total=rows,
    )


def test_200_kb_inline_result_is_relayed_byte_for_byte(tmp_path, capsys):
    arguments = {"projectId": "project-example"}
    frame, frame_path = make_frame(tmp_path, "t3_thread_list", arguments)
    # Non canonical spacing and escapes must survive untouched.
    literal = json.dumps(thread_list(400, 400), indent=1, ensure_ascii=True)
    assert len(literal) > 200_000
    log = write_log(
        tmp_path,
        [
            use(
                "call-1",
                "mcp__t3-code__t3_thread_list",
                arguments,
                frame["issued_at"] + 1,
            ),
            result("call-1", [dict(type="text", text=literal)], frame["issued_at"] + 2),
        ],
    )
    assert relay(frame_path, log) == 0
    ack = json.loads(capsys.readouterr().out)
    assert ack["result_sha256"] == hashlib.sha256(literal.encode()).hexdigest()
    assert decode_ack(frame, ack) == json.loads(literal)


def truncated_fixture(tmp_path, *, directory=None, when=None, notice_dir="/elsewhere"):
    arguments = {"projectId": "project-example"}
    frame, frame_path = make_frame(tmp_path, "t3_thread_list", arguments)
    literal = json.dumps(thread_list(300, 300))
    file_name = "mcp-t3-code-t3_thread_list-1700000000000.txt"
    notice = (
        f"Error: result ({len(literal):,} characters) exceeds maximum allowed "
        f"tokens. Output has been saved to {notice_dir}/{file_name}.\n"
        "Format: JSON with schema: {projectId: string, threads: [{...}]}\n"
        "- For targeted queries: use jq on the file directly.\n"
    )
    log = write_log(
        tmp_path,
        [
            use(
                "call-1",
                "mcp__t3-code__t3_thread_list",
                arguments,
                frame["issued_at"] + 1,
            ),
            result("call-1", notice, frame["issued_at"] + 2),
            result("call-1", notice),
        ],
    )
    directory = directory or tmp_path / SESSION / "tool-results"
    directory.mkdir(parents=True)
    saved = directory / file_name
    saved.write_text(literal)
    if when is not None:
        os.utime(saved, (when(frame), when(frame)))
    return frame, frame_path, log, literal, saved


def test_truncated_result_is_read_from_the_session_tool_results(tmp_path, capsys):
    frame, frame_path, log, literal, _saved = truncated_fixture(tmp_path)
    assert relay(frame_path, log) == 0
    ack = json.loads(capsys.readouterr().out)
    assert ack["result_sha256"] == hashlib.sha256(literal.encode()).hexdigest()
    assert len(decode_ack(frame, ack)["threads"]) == 300


def test_tool_results_directory_can_be_overridden(tmp_path, capsys):
    frame, frame_path, log, literal, _saved = truncated_fixture(
        tmp_path, directory=tmp_path / "custom-results"
    )
    assert relay(frame_path, log) == 1
    assert "relay_result_file_missing" in capsys.readouterr().err
    assert (
        relay(frame_path, log, "--tool-results-dir", str(tmp_path / "custom-results"))
        == 0
    )
    ack = json.loads(capsys.readouterr().out)
    assert ack["result_sha256"] == hashlib.sha256(literal.encode()).hexdigest()


def test_notice_path_cannot_leave_the_tool_results_directory(tmp_path, capsys):
    outside = tmp_path / "outside"
    frame, frame_path, log, _literal, _saved = truncated_fixture(
        tmp_path, directory=outside, notice_dir=str(outside)
    )
    (tmp_path / SESSION / "tool-results").mkdir(parents=True)
    # The file exists exactly where the notice says, but not in tool-results.
    assert relay(frame_path, log) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and "relay_result_file_missing" in captured.err


def test_saved_result_older_than_the_frame_is_refused(tmp_path, capsys):
    frame, frame_path, log, _literal, _saved = truncated_fixture(
        tmp_path, when=lambda frame: frame["issued_at"] - 30
    )
    assert relay(frame_path, log) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and "native_bridge_stale_result_file" in captured.err


def test_call_made_by_a_subagent_is_found_in_its_transcript(tmp_path, capsys):
    frame, frame_path = make_frame(tmp_path)
    log = write_log(tmp_path, [dict(type="user", message=dict(content="go"))])
    write_log(
        tmp_path / SESSION / "subagents",
        [
            use("call-1", "mcp__t3-code__t3_thread_send", ARGS, frame["issued_at"] + 1),
            result(
                "call-1",
                [dict(type="text", text=sent("from-subagent"))],
                frame["issued_at"] + 2,
            ),
        ],
        name="agent-a000000000000001",
    )
    assert relay(frame_path, log) == 0
    ack = json.loads(capsys.readouterr().out)
    assert decode_ack(frame, ack)["messageId"] == "from-subagent"


def test_errored_or_pending_calls_produce_no_ack(tmp_path, capsys):
    frame, frame_path = make_frame(tmp_path)
    name = "mcp__t3-code__t3_thread_send"
    log = write_log(
        tmp_path,
        [
            use("call-1", name, ARGS, frame["issued_at"] + 1),
            result("call-1", "tool failed", frame["issued_at"] + 2, is_error=True),
            use("call-2", name, ARGS, frame["issued_at"] + 3),
        ],
    )
    assert relay(frame_path, log) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "errored: 1" in captured.err and "still without a result: 1" in captured.err


def test_conflicting_results_for_one_call_are_refused(tmp_path, capsys):
    frame, frame_path = make_frame(tmp_path)
    log = write_log(
        tmp_path,
        [
            use("call-1", "mcp__t3-code__t3_thread_send", ARGS, frame["issued_at"] + 1),
            result("call-1", sent("one"), frame["issued_at"] + 2),
            result("call-1", sent("two")),
        ],
    )
    assert relay(frame_path, log) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and "relay_result_ambiguous" in captured.err


def test_result_that_is_not_the_tool_json_is_refused(tmp_path, capsys):
    frame, frame_path = make_frame(tmp_path)
    log = write_log(
        tmp_path,
        [
            use("call-1", "mcp__t3-code__t3_thread_send", ARGS, frame["issued_at"] + 1),
            result("call-1", "Sent the message successfully.", frame["issued_at"] + 2),
        ],
    )
    assert relay(frame_path, log) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and "relay_result_not_json" in captured.err


def test_result_must_still_satisfy_the_ack_contract(tmp_path, capsys):
    frame, frame_path = make_frame(tmp_path)
    log = write_log(
        tmp_path,
        [
            use("call-1", "mcp__t3-code__t3_thread_send", ARGS, frame["issued_at"] + 1),
            result(
                "call-1",
                json.dumps({"threadId": "another-thread", "messageId": "m"}),
                frame["issued_at"] + 2,
            ),
        ],
    )
    assert relay(frame_path, log) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and "native_bridge_identity_mismatch" in captured.err


def test_expired_frame_is_refused(tmp_path, capsys):
    frame, frame_path = make_frame(tmp_path, age=700)
    log = write_log(
        tmp_path,
        [
            use("call-1", "mcp__t3-code__t3_thread_send", ARGS, frame["issued_at"] + 1),
            result("call-1", sent(), frame["issued_at"] + 2),
        ],
    )
    assert relay(frame_path, log) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and "native_bridge_stale_frame" in captured.err


def test_waits_for_the_harness_to_record_the_call(tmp_path, capsys):
    import threading

    frame, frame_path = make_frame(tmp_path, age=0.0)
    log = write_log(tmp_path, [])

    def append():
        with log.open("a") as stream:
            for entry in (
                use("call-1", "mcp__t3-code__t3_thread_send", ARGS, time.time() + 1),
                result("call-1", sent("late-write")),
            ):
                stream.write(json.dumps(entry) + "\n")

    timer = threading.Timer(0.4, append)
    timer.start()
    try:
        code = main(
            [
                "bridge",
                "relay",
                "--frame",
                str(frame_path),
                "--session-log",
                str(log),
                "--wait",
                "10",
            ]
        )
    finally:
        timer.join()
    assert code == 0
    ack = json.loads(capsys.readouterr().out)
    assert decode_ack(frame, ack)["messageId"] == "late-write"


def test_auto_session_log_from_environment(tmp_path, monkeypatch, capsys):
    frame, frame_path = make_frame(tmp_path)
    home = tmp_path / "claude-home"
    work = tmp_path / "work.tree" / "project"
    work.mkdir(parents=True)
    slug = "".join(c if c.isalnum() else "-" for c in str(work))
    write_log(
        home / "projects" / slug,
        [
            use("call-1", "mcp__t3-code__t3_thread_send", ARGS, frame["issued_at"] + 1),
            result("call-1", sent("auto"), frame["issued_at"] + 2),
        ],
    )
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SESSION)
    monkeypatch.chdir(work)
    assert relay(frame_path, "auto") == 0
    ack = json.loads(capsys.readouterr().out)
    assert decode_ack(frame, ack)["messageId"] == "auto"


def test_auto_session_log_falls_back_to_a_unique_project(tmp_path, monkeypatch):
    home = tmp_path / "claude-home"
    only = write_log(home / "projects" / "-some-other-cwd", [])
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SESSION)
    assert resolve_session_log("auto", cwd=tmp_path) == only
    write_log(home / "projects" / "-second-cwd", [])
    with pytest.raises(StateError, match="^relay_session_log_ambiguous$"):
        resolve_session_log("auto", cwd=tmp_path)


@pytest.mark.parametrize("session", [None, "", "../escape", "a/b"])
def test_auto_session_log_requires_a_plain_session_id(tmp_path, monkeypatch, session):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    if session is None:
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    else:
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", session)
    with pytest.raises(StateError, match="^relay_session_log_unresolved$"):
        resolve_session_log("auto", cwd=tmp_path)


def test_missing_session_log_is_an_explicit_error(tmp_path, capsys):
    _frame, frame_path = make_frame(tmp_path)
    assert relay(frame_path, tmp_path / "absent.jsonl") == 1
    captured = capsys.readouterr()
    assert captured.out == "" and "relay_session_log_unresolved" in captured.err
