"""Named-pipe stdin with ACKs larger than the FIFO buffer (real kernel FIFOs).

Reproduces the origin arrangement: stdin is a mkfifo whose write end is held
open by another descriptor, stdout is a regular file, and each ACK is written
by a separate process with one blocking write. On Darwin the FIFO buffer is
8 KiB and such a writer never activated the reader's kqueue knote.
"""

import asyncio
import fcntl
import hashlib
import json
import os
import sys
import time

import pytest
from test_managed_tasks import report
from test_report_prevalidation import flow  # noqa: F401

from oraculo.protocol import canonical_json
from oraculo.state import StateError
from oraculo.stdio import read_line

pytestmark = pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs mkfifo")

WRITER = """import os, sys
fd = os.open(sys.argv[1], os.O_WRONLY)
data = open(sys.argv[2], "rb").read()
written = 0
while written < len(data):
    written += os.write(fd, data[written:])
"""
# Operational workaround for an unfixed reader: never sleep inside write().
NONBLOCKING_WRITER = """import os, select, sys
fd = os.open(sys.argv[1], os.O_WRONLY | os.O_NONBLOCK)
data = open(sys.argv[2], "rb").read()
written = 0
while written < len(data):
    try:
        written += os.write(fd, data[written:])
    except BlockingIOError:
        select.select([], [fd], [], 1)
"""


def open_held_fifo(path):
    """Blocking read end plus a held write end, as `mkfifo` + `sleep` does."""
    os.mkfifo(path)
    read_fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    hold_fd = os.open(path, os.O_WRONLY)
    fcntl.fcntl(
        read_fd, fcntl.F_SETFL, fcntl.fcntl(read_fd, fcntl.F_GETFL) & ~os.O_NONBLOCK
    )
    return read_fd, hold_fd


async def blocking_write(path, data, tmp_path, *, timeout=15, code=WRITER):
    source = tmp_path / f"ack-{time.monotonic_ns()}.bin"
    source.write_bytes(data)
    writer = await asyncio.create_subprocess_exec(
        sys.executable, "-c", code, str(path), str(source)
    )
    try:
        async with asyncio.timeout(timeout):
            assert await writer.wait() == 0
    except TimeoutError:
        pytest.fail(f"reader never drained a {len(data)}-byte ACK from the FIFO")
    finally:
        if writer.returncode is None:
            writer.kill()
            await writer.wait()


@pytest.mark.parametrize(
    "size,code",
    [
        *((size, WRITER) for size in (4096, 8192, 8193, 11907, 16385, 65537, 300000)),
        (74679, NONBLOCKING_WRITER),
    ],
)
async def test_read_line_wakes_for_single_blocking_write_beyond_fifo_buffer(
    tmp_path, size, code
):
    path = tmp_path / "in.fifo"
    read_fd, hold_fd = open_held_fifo(path)
    reader = os.fdopen(read_fd, "rb", buffering=0)
    line = os.urandom(size // 2).hex().encode()[: size - 1] + b"\n"
    try:
        flags = fcntl.fcntl(read_fd, fcntl.F_GETFL)
        pending = asyncio.create_task(read_line(reader, timeout=10))
        await asyncio.sleep(0.2)  # The knote is registered before bytes arrive.
        started = time.monotonic()
        await blocking_write(path, line, tmp_path, code=code)
        assert await pending == line
        assert time.monotonic() - started < 5
        # Readiness handling must not change the shared descriptor's flags.
        assert fcntl.fcntl(read_fd, fcntl.F_GETFL) == flags
    finally:
        reader.close()
        os.close(hold_fd)


async def test_read_line_on_silent_fifo_still_times_out(tmp_path):
    read_fd, hold_fd = open_held_fifo(tmp_path / "in.fifo")
    reader = os.fdopen(read_fd, "rb", buffering=0)
    try:
        with pytest.raises(TimeoutError):
            await read_line(reader, timeout=0.5)
    finally:
        reader.close()
        os.close(hold_fd)


async def test_read_line_on_closed_fifo_reports_eof(tmp_path):
    read_fd, hold_fd = open_held_fifo(tmp_path / "in.fifo")
    reader = os.fdopen(read_fd, "rb", buffering=0)
    os.close(hold_fd)
    try:
        with pytest.raises(StateError, match="native_bridge_ack_eof"):
            await read_line(reader, timeout=5)
    finally:
        reader.close()


def padded(tool, arguments, response, pad_hex):
    # Deterministic per call so repeated reads stay mutually consistent, and
    # hex so zlib cannot shrink the relayed ACK below the FIFO buffer.
    seed = canonical_json({"tool": tool, "arguments": arguments}).encode()
    return dict(
        response, oraculoTestPadding=hashlib.shake_256(seed).hexdigest(pad_hex // 2)
    )


async def run_cli_fifo(flow, tmp_path, action, payload, *, pad_hex, minimum):  # noqa: F811
    """Drive the real CLI exactly as the origin does: FIFO in, file out."""
    work = tmp_path / f"{action}-{time.monotonic_ns()}"
    work.mkdir()
    config = work / "cli.toml"
    config.write_text(
        "\n".join(
            f"{k} = {json.dumps(str(getattr(flow.service.config, k)))}"
            for k in ("state_dir", "projects_root", "vault_root")
        )
    )
    (work / "payload.json").write_text(json.dumps(payload))
    fifo = work / "in.fifo"
    read_fd, hold_fd = open_held_fifo(fifo)
    out = open(work / "out.txt", "wb")
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "oraculo",
        "--config",
        str(config),
        "task",
        action,
        "--stdio-host",
        "--file",
        str(work / "payload.json"),
        stdin=read_fd,
        stdout=out,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "ORACULO_BRIDGE_ACK_TIMEOUT": "30"},
    )
    os.close(read_fd)
    out.close()
    sizes, offset, buffer = [], 0, b""
    try:
        async with asyncio.timeout(120):
            while True:
                exited = False
                while b"\n" not in buffer:
                    with open(work / "out.txt", "rb") as source:
                        source.seek(offset)
                        block = source.read()
                    offset += len(block)
                    buffer += block
                    if not block:
                        if exited:
                            pytest.fail((await process.stderr.read()).decode())
                        exited = process.returncode is not None
                        await asyncio.sleep(0.02)
                line, buffer = buffer.split(b"\n", 1)
                if line == b"{":
                    code = await process.wait()
                    with open(work / "out.txt", "rb") as source:
                        source.seek(offset)
                        rest = source.read()
                    result = json.loads(line + b"\n" + buffer + rest)
                    return code, result, sizes, (await process.stderr.read()).decode()
                frame = json.loads(line)
                assert frame["marker"] == "ORACULO_HOST_CALL_V1", frame
                response = padded(
                    frame["tool"],
                    frame["arguments"],
                    await flow.host.call(frame["tool"], frame["arguments"]),
                    pad_hex,
                )
                frame_path, result_path = work / "frame.json", work / "result.json"
                frame_path.write_text(canonical_json(frame))
                result_path.write_text(canonical_json(response))
                helper = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-m",
                    "oraculo",
                    "bridge",
                    "ack",
                    "--frame",
                    str(frame_path),
                    "--from-file",
                    str(result_path),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                ack, error = await helper.communicate()
                assert helper.returncode == 0, error
                assert len(ack) > minimum
                sizes.append(len(ack))
                await blocking_write(fifo, ack, work)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
        os.close(hold_fd)


@pytest.mark.parametrize(
    "pad_hex,minimum", [(14000, 8192), (26000, 16384), (110000, 65536)]
)
async def test_cli_start_finish_resume_over_fifo_with_large_acks(
    flow,  # noqa: F811
    tmp_path,
    pad_hex,
    minimum,
):
    root = flow.service.config.projects_root
    code, task, sizes, stderr = await run_cli_fifo(
        flow,
        tmp_path,
        "start",
        dict(
            kind="work",
            key=f"fifo-{minimum}",
            text="Bounded work",
            cwd=str(root / "alpha"),
        ),
        pad_hex=pad_hex,
        minimum=minimum,
    )
    assert code == 0 and task["phase"] == "executing", (task, stderr)
    assert sizes
    flow.host.caller = task["child"]["childThreadId"]
    code, result, sizes, stderr = await run_cli_fifo(
        flow,
        tmp_path,
        "finish",
        dict(task_id=task["id"], report=report(task)),
        pad_hex=pad_hex,
        minimum=minimum,
    )
    assert code == 0 and result["phase"] == "validation_submitted", (result, stderr)
    assert sizes
    flow.host.complete(flow.host.caller)
    flow.host.caller = "origin"
    code, result, sizes, stderr = await run_cli_fifo(
        flow,
        tmp_path,
        "resume",
        dict(task_id=task["id"]),
        pad_hex=pad_hex,
        minimum=minimum,
    )
    assert code == 0, (result, stderr)
    assert result["memory"]["write_state"] == "saved", result
    assert result.get("blocked_reason") is None, result
    assert "t3_thread_list" in {name for name, _ in flow.host.calls[-len(sizes) :]}
