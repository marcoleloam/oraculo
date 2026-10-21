import json
import sys

import pytest

from oraculo.cli import main
from oraculo.state import DedupeConflict, StateError
from oraculo.task_store import TaskStore
from oraculo.task_transport import CommandTransport


def test_store_restart_dedupe_and_return_ack(tmp_path):
    path = tmp_path / "tasks.sqlite3"
    first = TaskStore(path)
    task = first.create(
        {"thread_id": "origin", "run_id": "run"}, "request", {"text": "work"}
    )
    first.close()
    store = TaskStore(path)
    replay = store.create(
        {"thread_id": "origin", "run_id": "later-run"}, "request", {"text": "work"}
    )
    assert replay["id"] == task["id"]
    assert replay["origin"]["run_id"] == "run"
    with pytest.raises(DedupeConflict):
        store.create(
            {"thread_id": "origin", "run_id": "run"}, "request", {"text": "different"}
        )
    event = store.pending_returns(task["id"])[0]
    with pytest.raises(StateError, match="origin_mismatch"):
        store.acknowledge_return(event["id"], {"threadId": "wrong", "messageId": "msg"})
    store.acknowledge_return(event["id"], {"threadId": "origin", "messageId": "msg"})
    assert store.pending_returns(task["id"]) == []
    store.close()


def test_immutable_context(tmp_path):
    store = TaskStore(tmp_path / "tasks.sqlite3")
    artifact = store.context_artifact({"context": "private"})
    assert store.context_artifact({"context": "private"}) == artifact
    from pathlib import Path

    Path(artifact["path"]).write_text("corrupted")
    with pytest.raises(StateError, match="integrity"):
        store.context_artifact({"context": "private"})
    store.close()


def test_missing_bridge_and_invalid_command(monkeypatch):
    monkeypatch.delenv("T3_ACP_MCP_NODE", raising=False)
    with pytest.raises(StateError, match="caller_bound_transport_unavailable"):
        CommandTransport()
    with pytest.raises(StateError, match="invalid_caller_bound"):
        CommandTransport("shell command")


async def test_bridge_executes_without_shell_and_unwraps(tmp_path):
    script = tmp_path / "bridge.py"
    script.write_text(
        'import json,sys\nprint(json.dumps({"structuredContent":{"name":sys.argv[1],"args":json.loads(sys.argv[2])}}))'
    )
    result = await CommandTransport([sys.executable, str(script)]).call(
        "tool", {"text": "$(false); literal"}
    )
    assert result == {"name": "tool", "args": {"text": "$(false); literal"}}


async def test_bridge_error_does_not_expose_stderr(tmp_path):
    script = tmp_path / "bridge.py"
    script.write_text('import sys\nsys.stderr.write("secret fixture")\nsys.exit(1)')
    with pytest.raises(StateError, match="^host_response_uncertain$"):
        await CommandTransport([sys.executable, str(script)]).call("tool", {})


def test_cli_list_without_host_bridge(service_config, monkeypatch, capsys):
    from oraculo.task_store import TaskStore

    config, config_path = service_config
    TaskStore(config.state_dir / "managed-tasks.sqlite3").close()
    monkeypatch.delenv("T3_ACP_MCP_NODE", raising=False)
    assert main(["--config", str(config_path), "task", "list"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "tasks": [],
        "source": "local_ledger",
    }


async def test_native_stdio_bridge_correlates_real_tool_response_shape():
    import io

    from oraculo.task_transport import StdioHostTransport

    output = io.StringIO()

    class Reader:
        def readline(self):
            call = json.loads(output.getvalue())
            return json.dumps(
                {
                    "request_id": call["request_id"],
                    "result": {"structuredContent": {"parentThreadId": "caller"}},
                }
            )

    result = await StdioHostTransport(Reader(), output).call(
        "orchestrator_capabilities", {}
    )
    assert result == {"parentThreadId": "caller"}
    assert json.loads(output.getvalue())["marker"] == "ORACULO_HOST_CALL_V1"


async def test_native_stdio_bridge_rejects_unrelated_ack():
    import io

    from oraculo.task_transport import StdioHostTransport

    with pytest.raises(StateError, match="native_bridge_ack_mismatch"):
        await StdioHostTransport(
            io.StringIO('{"request_id":"wrong","result":{}}\n'), io.StringIO()
        ).call("tool", {})


def test_native_stdio_pty_accepts_large_ack_without_canonical_truncation():
    import os
    import pty
    import subprocess

    master, slave = pty.openpty()
    code = (
        "import asyncio\nfrom oraculo.task_transport import StdioHostTransport\n"
        "async def main():\n"
        '    value = await StdioHostTransport().call("example", {})\n'
        '    print(len(value["data"]))\n'
        "asyncio.run(main())\n"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", code],
        stdin=slave,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    os.close(slave)
    try:
        request = json.loads(process.stdout.readline())
        raw = (
            json.dumps(
                {"request_id": request["request_id"], "result": {"data": "x" * 12000}}
            )
            + "\n"
        ).encode()
        while raw:
            raw = raw[os.write(master, raw) :]
        out, err = process.communicate(timeout=10)
        assert process.returncode == 0, err
        assert out.strip() == "12000"
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        os.close(master)


async def test_native_stdio_bridge_ack_timeout_defaults_and_overrides(monkeypatch):
    import io
    import os

    from oraculo.task_transport import StdioHostTransport

    monkeypatch.delenv("ORACULO_BRIDGE_ACK_TIMEOUT", raising=False)
    assert StdioHostTransport(io.StringIO(), io.StringIO()).ack_timeout == 600.0
    monkeypatch.setenv("ORACULO_BRIDGE_ACK_TIMEOUT", "45")
    assert StdioHostTransport(io.StringIO(), io.StringIO()).ack_timeout == 45.0
    assert (
        StdioHostTransport(io.StringIO(), io.StringIO(), ack_timeout=7).ack_timeout == 7
    )
    for invalid in ("0", "-1", "soon"):
        monkeypatch.setenv("ORACULO_BRIDGE_ACK_TIMEOUT", invalid)
        with pytest.raises(StateError, match="invalid_bridge_ack_timeout"):
            StdioHostTransport(io.StringIO(), io.StringIO())
    monkeypatch.delenv("ORACULO_BRIDGE_ACK_TIMEOUT", raising=False)
    # A silent host within the wait is reported as uncertain, never as success.
    read, write = os.pipe()
    reader = os.fdopen(read, "rb", buffering=0)
    try:
        with pytest.raises(StateError, match="host_response_uncertain"):
            await StdioHostTransport(reader, io.StringIO(), ack_timeout=0.2).call(
                "tool", {}
            )
    finally:
        reader.close()
        os.close(write)
