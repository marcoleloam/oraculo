"""Real PTYs/pipes/files and slow readers; no host API is simulated as real proof."""

import asyncio
import fcntl
import json
import os
import pty
import secrets
import select
import subprocess
import sys
import termios
import time

import pytest

from oraculo.host_ack import make_ack
from oraculo.state import StateError
from oraculo.stdio import write_async, write_text


@pytest.mark.parametrize("mode", ["shared", "separate", "pipe", "file", "pipes"])
def test_large_frames_ack_and_final_result_exact_with_slow_reader(tmp_path, mode):
    master, slave = pty.openpty()
    out_master, out_slave = pty.openpty()
    before = termios.tcgetattr(slave)
    fd_flags = fcntl.fcntl(slave, fcntl.F_GETFL)
    # Explicit external NONBLOCK reproduces the observed symptom independently
    # of the suspected connect_read_pipe trigger.
    fcntl.fcntl(slave, fcntl.F_SETFL, fd_flags | os.O_NONBLOCK)
    target = open(tmp_path / "output", "w+b")
    output = (
        slave
        if mode == "shared"
        else out_slave
        if mode == "separate"
        else target
        if mode == "file"
        else subprocess.PIPE
    )
    code = """import asyncio,sys
from oraculo.task_transport import StdioHostTransport
from oraculo.stdio import print_json
async def main():
    bridge=StdioHostTransport()
    try:
        for i in range(2):
            result=await bridge.call('test', {'large':'á'*150000,'sequence':i})
            assert result['ack']=='ü'*100000
        print_json({'final':'Ω'*180000})
    finally:
        bridge.close()
asyncio.run(main())
"""
    process = subprocess.Popen(
        [sys.executable, "-c", code],
        stdin=subprocess.PIPE if mode == "pipes" else slave,
        stdout=output,
        stderr=subprocess.PIPE,
    )
    read_fd = (
        master
        if mode == "shared"
        else out_master
        if mode == "separate"
        else process.stdout.fileno()
        if mode != "file"
        else None
    )
    input_fd = process.stdin.fileno() if mode == "pipes" else master
    buffer = b""
    file_offset = 0

    def line():
        nonlocal buffer, file_offset
        deadline = time.monotonic() + 15
        while b"\n" not in buffer:
            assert time.monotonic() < deadline, "bounded output wait expired"
            if mode == "file":
                with open(target.name, "rb") as source:
                    source.seek(file_offset)
                    block = source.read(2048)
                    file_offset += len(block)
            else:
                readable, _, _ = select.select([read_fd], [], [], 0.2)
                block = os.read(read_fd, 2048) if readable else b""
            buffer += block
            # Controlled slow consumer forces a frame larger than PTY buffers.
            time.sleep(0.0005)
        result, buffer = buffer.split(b"\n", 1)
        return result

    try:
        for i in range(2):
            frame = json.loads(line())
            assert frame["arguments"] == {"large": "á" * 150000, "sequence": i}
            raw = (
                json.dumps(
                    make_ack(
                        frame,
                        json.dumps(
                            {
                                "ack": "ü" * 100000,
                                "incompressible": secrets.token_hex(100000),
                            },
                            ensure_ascii=False,
                        ).encode(),
                    ),
                    ensure_ascii=False,
                )
                + "\n"
            ).encode()
            offset = 0
            while offset < len(raw):
                offset += os.write(input_fd, raw[offset:])
        # Pretty printed final result is multiple lines, still exact JSON.
        chunks = [line()]
        while chunks[-1].strip() != b"}":
            chunks.append(line())
        assert json.loads(b"\n".join(chunks)) == {"final": "Ω" * 180000}
        process.wait(timeout=5)
        assert process.returncode == 0, process.stderr.read().decode()
        restored = termios.tcgetattr(slave)
        # Darwin sets PENDIN when canonical processing is restored. Compare
        # requested settings excluding that kernel-maintained pending-input bit.
        restored[3] &= ~getattr(termios, "PENDIN", 0)
        before[3] &= ~getattr(termios, "PENDIN", 0)
        assert restored == before
        assert fcntl.fcntl(slave, fcntl.F_GETFL) & (os.O_NONBLOCK | os.O_ACCMODE) == (
            fd_flags | os.O_NONBLOCK
        ) & (os.O_NONBLOCK | os.O_ACCMODE)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        target.close()
        for fd in [master, slave, out_master, out_slave]:
            os.close(fd)


def test_writer_timeout_restores_flags_and_closed_pipe():
    read, write = os.pipe()
    stream = os.fdopen(write, "w", buffering=1)
    before = fcntl.fcntl(write, fcntl.F_GETFL)
    try:
        with pytest.raises(StateError, match="timeout_incomplete"):
            write_text("x" * 1000000, stream, timeout=0.05)
        assert fcntl.fcntl(write, fcntl.F_GETFL) & (
            os.O_NONBLOCK | os.O_APPEND | os.O_ASYNC | os.O_ACCMODE
        ) == before & (os.O_NONBLOCK | os.O_APPEND | os.O_ASYNC | os.O_ACCMODE)
        os.close(read)
        with pytest.raises(BrokenPipeError):
            write_text("output", stream)
    finally:
        stream.close()


async def test_writer_cancellation_stops_worker_and_restores_flags():
    read, write = os.pipe()
    stream = os.fdopen(write, "w")
    before = fcntl.fcntl(write, fcntl.F_GETFL)
    try:
        task = asyncio.create_task(write_async("x" * 1000000, stream))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        assert fcntl.fcntl(write, fcntl.F_GETFL) & (
            os.O_NONBLOCK | os.O_APPEND | os.O_ASYNC | os.O_ACCMODE
        ) == before & (os.O_NONBLOCK | os.O_APPEND | os.O_ASYNC | os.O_ACCMODE)
    finally:
        stream.close()
        os.close(read)


async def test_connect_read_pipe_changes_shared_pty_flags():
    # Laboratory observation of the prior reader's side effect, not an assertion
    # that the production incident necessarily had exactly these descriptors.
    master, slave = pty.openpty()
    duplicate = os.dup(slave)
    reader = os.fdopen(duplicate, "rb", buffering=0)
    previous = fcntl.fcntl(slave, fcntl.F_GETFL)
    transport = None
    try:
        loop = asyncio.get_running_loop()
        transport, _ = await loop.connect_read_pipe(asyncio.Protocol, reader)
        assert fcntl.fcntl(slave, fcntl.F_GETFL) & os.O_NONBLOCK
        assert not previous & os.O_NONBLOCK
    finally:
        if transport:
            transport.close()
        os.close(master)
        os.close(slave)
