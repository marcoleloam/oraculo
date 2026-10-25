import asyncio
import contextlib
import json
from types import SimpleNamespace

import pytest

from oraculo.cli import make_envelope, request
from oraculo.protocol import MAX_ENVELOPE_BYTES, MAX_RESPONSE_BYTES, parse_envelope


@pytest.mark.parametrize("size", [MAX_ENVELOPE_BYTES + 100, MAX_RESPONSE_BYTES])
async def test_aggregate_response_can_exceed_request_limit(tmp_path, size):
    envelope = make_envelope("status.get", "*", {})
    prefix = json.dumps({"ok": True, "request_id": envelope["request_id"], "data": ""})
    response = prefix[:-2].encode() + b"x" * (size - len(prefix) - 1) + b'"}\n'
    assert len(response) == size
    await exchange(tmp_path, envelope, response, expected_size=size)


@pytest.mark.parametrize("extra", [1, 100])
async def test_oversized_response_is_rejected(tmp_path, extra):
    response = b"x" * (MAX_RESPONSE_BYTES + extra - 1) + b"\n"
    with pytest.raises(ValueError, match="^invalid_response$"):
        await exchange(tmp_path, make_envelope("status.get", "*", {}), response)


def test_request_limit_is_unchanged():
    with pytest.raises(ValueError, match="envelope_too_large"):
        parse_envelope(b"x" * (MAX_ENVELOPE_BYTES + 1))


async def exchange(tmp_path, envelope, response, expected_size=None):
    # macOS Unix socket names are limited; pytest's nested paths can exceed that.
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory(prefix="ora-rpc-", dir="/tmp") as directory:
        path = Path(directory) / "socket"
        completed = asyncio.Event()

        async def reply(reader, writer):
            try:
                assert json.loads(await reader.readline()) == envelope
                writer.write(response)
                with contextlib.suppress(ConnectionError):
                    await writer.drain()
            finally:
                writer.close()
                with contextlib.suppress(ConnectionError):
                    await writer.wait_closed()
                completed.set()

        server = await asyncio.start_unix_server(reply, path=str(path))
        async with server:
            try:
                result = await request(SimpleNamespace(socket_path=path), envelope)
                assert result["ok"] is True
                assert result["request_id"] == envelope["request_id"]
                assert len(json.dumps(result).encode()) + 1 == expected_size
            finally:
                await asyncio.wait_for(completed.wait(), 5)
