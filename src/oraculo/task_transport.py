"""Caller-bound host transport and the existing socket memory adapter.

A generic daemon HTTP session must not impersonate a parent. This transport runs
only where the host supplies its caller-bound ACP bridge, or an explicitly
configured equivalent executable. No credentials or Keychain are read here.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os

from oraculo.cli import make_envelope, request
from oraculo.protocol import canonical_json
from oraculo.state import StateError


class CommandTransport:
    def __init__(self, command: list[str] | None = None):
        if command is None:
            node = os.environ.get("T3_ACP_MCP_NODE")
            if not node:
                raise StateError(
                    "caller_bound_transport_unavailable: configure a host bridge; no global hook installed"
                )
            entry = os.environ.get("T3_ACP_MCP_ENTRYPOINT")
            command = [node, *([entry] if entry else []), "acp-mcp-call"]
        if (
            not isinstance(command, list)
            or not command
            or any(not isinstance(arg, str) or not arg for arg in command)
        ):
            raise StateError("invalid_caller_bound_bridge_command")
        self.command = command

    async def call(self, name: str, arguments: dict) -> dict:
        process = await asyncio.create_subprocess_exec(
            *self.command,
            name,
            canonical_json(arguments),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "ELECTRON_RUN_AS_NODE": "1"},
        )
        try:
            async with asyncio.timeout(60):
                out, _ = await process.communicate()
        except TimeoutError:
            process.kill()
            await process.wait()
            raise StateError("host_response_uncertain") from None
        if process.returncode:
            raise StateError("host_response_uncertain")
        result = json.loads(out)
        if result.get("isError"):
            raise StateError("host_response_uncertain")
        if "structuredContent" in result:
            return result["structuredContent"]
        if "content" in result:
            return json.loads(
                "\n".join(
                    x.get("text", "")
                    for x in result["content"]
                    if x.get("type") == "text"
                )
            )
        return result


class SocketMemory:
    def __init__(self, config):
        self.config = config

    async def _call(
        self, action: str, project: str, actor: dict, payload: dict
    ) -> dict:
        envelope = make_envelope("memory.cycle." + action, project, payload)
        envelope["origin"] = {k: actor[k] for k in ("thread_id", "run_id")}
        response = await request(self.config, envelope)
        if not response.get("ok"):
            raise StateError("memory_adapter_unavailable")
        return response["data"]

    async def begin(self, project: str, actor: dict, key: str, query: str) -> dict:
        cycle = await self._call(
            "begin", project, actor, {"key": key, "query": query[:8000]}
        )
        if cycle["read_state"] == "unavailable":
            cycle = await self._call(
                "retry", project, actor, {"cycle_id": cycle["id"], "phase": "read"}
            )
        if cycle["read_state"] not in {"found", "empty"}:
            raise StateError("memory_read_unavailable")
        reference = cycle["artifact"]["artifact"]
        content, offset = [], 0
        while True:
            page = await self._call(
                "read",
                project,
                actor,
                {"cycle_id": cycle["id"], "artifact": reference, "offset": offset},
            )
            chunk = page["content"]
            if (
                page["offset"] != offset
                or page["next_offset"] != offset + len(chunk)
                or page["sha256"] != reference
            ):
                raise StateError("memory_page_mismatch")
            content.append(chunk)
            offset += len(chunk)
            if offset > 16 * 1024 * 1024 or (not chunk and not page["eof"]):
                raise StateError("memory_page_limit")
            if page["eof"]:
                if offset != page["total_characters"]:
                    raise StateError("memory_page_mismatch")
                break
        raw = "".join(content)
        if hashlib.sha256(raw.encode()).hexdigest() != reference:
            raise StateError("memory_digest_mismatch")
        return {"cycle": cycle, "context": json.loads(raw), "artifact": reference}

    async def finish(
        self, project: str, actor: dict, cycle_id: str, status: str, report: dict
    ) -> dict:
        cycle = await self._call(
            "finish",
            project,
            actor,
            {"cycle_id": cycle_id, "status": status, "evidence": report},
        )
        if cycle["write_state"] in {"failed", "pending"}:
            cycle = await self._call(
                "retry", project, actor, {"cycle_id": cycle_id, "phase": "write"}
            )
        return cycle


class StdioHostTransport:
    """Cooperative native-tool bridge for harnesses without an ACP executable.

    Run the adapter in an interactive tool session. For each emitted call the
    origin agent invokes its native T3 tool, then writes one JSON acknowledgement
    to stdin. This is a request/response transport, not a task or PR poller.
    The local user/agent is the trust boundary, as for the existing Unix socket.
    """

    # An agent answering frames by hand may need minutes to transcribe a large
    # argument into its native tool call; the daemon relay waits longer still.
    DEFAULT_ACK_TIMEOUT = 600.0

    def __init__(self, reader=None, writer=None, ack_timeout: float | None = None):
        import sys

        self.reader = reader or sys.stdin
        self.writer = writer or sys.stdout
        self.terminal = None
        if ack_timeout is None:
            raw = os.environ.get("ORACULO_BRIDGE_ACK_TIMEOUT")
            try:
                ack_timeout = float(raw) if raw else self.DEFAULT_ACK_TIMEOUT
            except ValueError:
                raise StateError("invalid_bridge_ack_timeout") from None
        if not ack_timeout > 0:
            raise StateError("invalid_bridge_ack_timeout")
        self.ack_timeout = ack_timeout
        # PTY canonical mode truncates long JSON acknowledgements. This adapter
        # owns its interactive terminal while active; restore it at process exit.
        if getattr(self.reader, "isatty", lambda: False)():
            import atexit
            import termios
            import tty

            fd = self.reader.fileno()
            previous = termios.tcgetattr(fd)
            tty.setraw(fd)
            self.terminal = (fd, previous)
            atexit.register(self.close)

    def close(self):
        if self.terminal:
            import termios

            fd, previous = self.terminal
            self.terminal = None
            try:
                termios.tcsetattr(fd, termios.TCSANOW, previous)
            except (OSError, termios.error):
                pass

    async def call(self, name: str, arguments: dict) -> dict:
        import uuid

        request_id = uuid.uuid4().hex
        from oraculo.stdio import read_line, write_async

        await write_async(
            canonical_json(
                {
                    "marker": "ORACULO_HOST_CALL_V1",
                    "request_id": request_id,
                    "tool": name,
                    "arguments": arguments,
                }
            )
            + "\n",
            self.writer,
        )
        try:
            raw = await read_line(self.reader, timeout=self.ack_timeout)
        except TimeoutError:
            raise StateError("host_response_uncertain") from None
        try:
            ack = json.loads(raw)
        except (TypeError, ValueError):
            raise StateError("native_bridge_ack_invalid") from None
        if ack.get("request_id") != request_id or not isinstance(
            ack.get("result"), dict
        ):
            raise StateError("native_bridge_ack_mismatch")
        result = ack["result"]
        if result.get("isError"):
            raise StateError("host_response_uncertain")
        if "structuredContent" in result:
            result = result["structuredContent"]
        elif "content" in result:
            result = json.loads(
                "\n".join(
                    c.get("text", "")
                    for c in result["content"]
                    if c.get("type") == "text"
                )
            )
        if not isinstance(result, dict):
            raise StateError("native_bridge_result_invalid")
        return result
