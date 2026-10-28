"""Bounded exact writes and reads without asyncio changing shared PTY flags."""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import select
import sys
import threading
import time

from oraculo.state import StateError

MAX_STDIO_BYTES = 16 * 1024 * 1024
# Darwin named pipes (mkfifo) can lose kqueue/poll wakeups: a blocking writer
# whose single write() exceeds the 8 KiB FIFO buffer fills it and sleeps, and a
# read knote registered before those bytes arrived is never activated. select()
# still reports them, so readiness is also rechecked by level on this interval.
READY_RECHECK_SECONDS = 0.1


def readable_now(fd: int) -> bool:
    """Level-triggered, non-blocking readability check that never sets flags."""
    try:
        return bool(select.select([fd], [], [], 0)[0])
    except ValueError:  # Descriptor above FD_SETSIZE.
        poller = select.poll()
        poller.register(fd, select.POLLIN)
        return bool(poller.poll(0))


def write_text(text: str, stream=None, *, timeout: float = 60, cancelled=None) -> None:
    stream = stream or sys.stdout
    raw = text.encode("utf-8")
    if len(raw) > MAX_STDIO_BYTES:
        raise StateError("stdio_output_limit")
    try:
        fd = stream.fileno()
    except (AttributeError, OSError):
        stream.write(text)
        stream.flush()
        return
    original = fcntl.fcntl(fd, fcntl.F_GETFL)
    deadline = time.monotonic() + timeout
    offset = 0
    try:
        # This may affect stdin on a shared PTY. No buffered/read-pipe reader is
        # active during the write; restore flags before waiting for the ACK.
        fcntl.fcntl(fd, fcntl.F_SETFL, original | os.O_NONBLOCK)
        while offset < len(raw):
            if cancelled is not None and cancelled.is_set():
                raise StateError("stdio_write_cancelled_incomplete")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise StateError(
                    "stdio_write_timeout_incomplete"
                    if offset
                    else "stdio_write_timeout_unemitted"
                )
            try:
                count = os.write(fd, raw[offset : offset + 65536])
                if not count:
                    raise StateError("stdio_write_closed")
                offset += count
            except BlockingIOError:
                select.select(
                    [],
                    [fd],
                    [],
                    min(remaining, 0.1) if cancelled is not None else remaining,
                )
            except InterruptedError:
                continue
    finally:
        fcntl.fcntl(fd, fcntl.F_SETFL, original)


async def write_async(text: str, stream=None, *, timeout: float = 60):
    cancelled = threading.Event()
    pending = asyncio.create_task(
        asyncio.to_thread(
            write_text, text, stream, timeout=timeout, cancelled=cancelled
        )
    )
    try:
        await asyncio.shield(pending)
    except asyncio.CancelledError:
        cancelled.set()
        try:
            await asyncio.shield(pending)
        except StateError:
            pass
        raise


def print_json(value: dict, stream=None) -> None:
    write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", stream)


async def read_line(
    reader, *, limit: int = 4 * 1024 * 1024, timeout: float = 60
) -> bytes | str:
    try:
        fd = reader.fileno()
    except (AttributeError, OSError):
        raw = reader.readline()
        if len(raw.encode() if isinstance(raw, str) else raw) > limit:
            raise StateError("native_bridge_ack_limit")
        return raw
    # Readiness registration does not mutate O_NONBLOCK, unlike connect_read_pipe.
    # A wakeup is only a hint: bytes are read after a level check confirms them,
    # so a blocking descriptor is never read while empty.
    loop = asyncio.get_running_loop()
    result = bytearray()
    async with asyncio.timeout(timeout):
        while True:
            if not readable_now(fd):
                ready = loop.create_future()

                def wake():
                    if not ready.done():
                        ready.set_result(None)

                try:
                    loop.add_reader(fd, wake)
                except PermissionError:
                    # Regular files (epoll) cannot be registered.
                    await asyncio.sleep(READY_RECHECK_SECONDS)
                else:
                    try:
                        await asyncio.wait([ready], timeout=READY_RECHECK_SECONDS)
                    finally:
                        loop.remove_reader(fd)
                if not readable_now(fd):
                    continue
            try:
                byte = os.read(fd, min(65536, limit + 1 - len(result)))
            except BlockingIOError:
                continue
            if not byte:
                raise StateError("native_bridge_ack_eof")
            result.extend(byte)
            if len(result) > limit:
                raise StateError("native_bridge_ack_limit")
            if b"\n" in byte:
                if not result.endswith(b"\n") or result.count(b"\n") != 1:
                    raise StateError("native_bridge_unsolicited_ack")
                return bytes(result)
