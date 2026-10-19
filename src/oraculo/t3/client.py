from __future__ import annotations

import json
from contextlib import AsyncExitStack
from typing import Any

import httpx2
from jsonschema import ValidationError as SchemaValidationError
from jsonschema.validators import validator_for
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import PaginatedRequestParams
from pydantic import ValidationError

from oraculo.t3.auth import AuthRequired, build_auth, validate_endpoint
from oraculo.t3.contracts import (
    REQUIRED_TOOLS,
    ContractError,
    ThreadRead,
    join_utf16,
    utf16_length,
)


class T3Error(RuntimeError):
    pass


class TransportError(T3Error):
    pass


class MutationUncertain(TransportError):
    pass


READ_TOOLS = frozenset(
    {
        "t3_project_list",
        "t3_thread_list",
        "t3_thread_read",
        "t3_thread_wait",
        "orchestrator_capabilities",
    }
)


def _auth_failure(exc: BaseException) -> bool:
    pending = [exc]
    seen: set[int] = set()
    while pending:
        error = pending.pop()
        if id(error) in seen:
            continue
        seen.add(id(error))
        if isinstance(error, AuthRequired) or type(error).__name__ in {
            "OAuthTokenError",
            "OAuthFlowError",
            "OAuthRegistrationError",
        }:
            return True
        if isinstance(error, BaseExceptionGroup):
            pending.extend(error.exceptions)
        if error.__cause__ is not None:
            pending.append(error.__cause__)
    return False


def _dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return value.model_dump(mode="json", by_alias=True)


class T3Client:
    def __init__(self, endpoint: str, auth: Any = None, *, session: Any = None) -> None:
        validate_endpoint(endpoint)
        self.endpoint = endpoint
        self.auth = auth
        self.session = session
        self.stack = AsyncExitStack()
        self.tools: dict[str, dict[str, Any]] = {}

    async def __aenter__(self) -> T3Client:
        try:
            if self.session is None:
                http = await self.stack.enter_async_context(
                    httpx2.AsyncClient(
                        auth=self.auth or build_auth(self.endpoint), timeout=60
                    )
                )
                read_stream, write_stream = await self.stack.enter_async_context(
                    streamable_http_client(self.endpoint, http_client=http)
                )
                self.session = await self.stack.enter_async_context(
                    ClientSession(read_stream, write_stream)
                )
                await self.session.initialize()
            await self.validate_tools()
            return self
        except BaseException as exc:
            await self.stack.aclose()
            if _auth_failure(exc):
                raise AuthRequired("T3 OAuth requires oraculo auth login") from exc
            raise

    async def __aexit__(self, *args: Any) -> None:
        await self.stack.aclose()

    async def validate_tools(self) -> None:
        cursor = None
        seen: set[str] = set()
        while True:
            result = _dict(
                await self.session.list_tools(
                    params=PaginatedRequestParams(cursor=cursor) if cursor else None
                )
            )
            for entry in result["tools"]:
                entry = _dict(entry)
                name = entry["name"].rsplit("__", 1)[-1]
                if name in self.tools:
                    raise ContractError(f"Ambiguous tool name: {name}")
                if not isinstance(entry.get("inputSchema"), dict):
                    raise ContractError(f"Missing input schema: {name}")
                self.tools[name] = entry
            cursor = result.get("nextCursor")
            if cursor is None:
                break
            if cursor in seen:
                raise ContractError("Repeated tools/list cursor")
            seen.add(cursor)
        missing = REQUIRED_TOOLS - self.tools.keys()
        if missing:
            raise ContractError(f"T3 tools missing: {', '.join(sorted(missing))}")

    async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name not in self.tools:
            raise ContractError(f"Unavailable T3 tool: {name}")
        tool = self.tools[name]
        try:
            validator_for(tool["inputSchema"])(tool["inputSchema"]).validate(arguments)
        except SchemaValidationError as exc:
            raise ContractError(
                f"T3 input schema mismatch: {name}", tool=name, direction="input"
            ) from exc
        try:
            result = _dict(
                await self.session.call_tool(tool["name"], arguments=arguments)
            )
        except AuthRequired:
            raise
        except Exception as exc:
            if _auth_failure(exc):
                raise AuthRequired("T3 OAuth requires oraculo auth login") from exc
            error = TransportError if name in READ_TOOLS else MutationUncertain
            raise error(
                f"T3 call failed: {name}; reconcile before retrying mutations"
            ) from exc
        if result.get("isError"):
            error = T3Error if name in READ_TOOLS else MutationUncertain
            raise error(f"T3 tool reported an error: {name}")
        payload = result.get("structuredContent")
        if payload is None:
            texts = [
                item.get("text", "")
                for item in result.get("content", [])
                if item.get("type") == "text"
            ]
            try:
                payload = json.loads("\n".join(texts))
            except (ValueError, TypeError) as exc:
                raise ContractError(
                    f"T3 response is not structured JSON: {name}"
                ) from exc
        if not isinstance(payload, dict):
            raise ContractError(f"T3 response must be an object: {name}")
        if tool.get("outputSchema"):
            try:
                validator_for(tool["outputSchema"])(tool["outputSchema"]).validate(
                    payload
                )
            except SchemaValidationError as exc:
                raise ContractError(
                    f"T3 output schema mismatch: {name}", tool=name, direction="output"
                ) from exc
        return payload

    async def capabilities(self) -> dict[str, Any]:
        result = await self.call("orchestrator_capabilities", {})
        if not isinstance(result.get("providers"), list):
            raise ContractError("Capabilities missing providers")
        return result

    async def _pages(
        self, name: str, key: str, arguments: dict[str, Any]
    ) -> list[dict[str, Any]]:
        cursor = None
        seen: set[int] = set()
        items: list[dict[str, Any]] = []
        for _ in range(10000):
            page = await self.call(
                name,
                {
                    **arguments,
                    "limit": 100,
                    **({"cursor": cursor} if cursor is not None else {}),
                },
            )
            if not isinstance(page.get(key), list) or "nextCursor" not in page:
                raise ContractError(f"Invalid page: {name}")
            items.extend(page[key])
            cursor = page["nextCursor"]
            if cursor is None:
                return items
            if not isinstance(cursor, int) or cursor in seen:
                raise ContractError(f"Invalid/repeated cursor: {name}")
            seen.add(cursor)
        raise ContractError("Pagination exceeded maximum page count")

    async def list_projects(self) -> list[dict[str, Any]]:
        rows = await self._pages("t3_project_list", "projects", {})
        return list({row["id"]: row for row in rows}.values())

    async def list_threads(
        self, project_id: str, *, title_contains: str | None = None
    ) -> list[dict[str, Any]]:
        arguments: dict[str, Any] = {"projectId": project_id, "includeSubagents": True}
        if title_contains is not None:
            arguments["titleContains"] = title_contains
        rows = await self._pages("t3_thread_list", "threads", arguments)
        return list({row["threadId"]: row for row in rows}.values())

    async def read_thread(
        self, thread_id: str, *, after_position: int | None = None
    ) -> dict[str, Any]:
        arguments: dict[str, Any] = {
            "threadId": thread_id,
            "view": "messages",
            "limit": 100,
            "runLimit": 50,
            "maxCharsPerItem": 16000,
        }
        if after_position is not None:
            arguments["afterPosition"] = after_position
        result = await self.call("t3_thread_read", arguments)
        try:
            ThreadRead.model_validate(result)
        except ValidationError as exc:
            raise ContractError("Invalid T3 thread read") from exc
        if result["thread"]["threadId"] != thread_id:
            raise ContractError("Thread read identity mismatch")
        for item in result["items"]:
            parts = [item["text"] or ""]
            offset = item.get("nextTextOffset")
            if item["textTruncated"] and offset is None:
                raise ContractError("Truncated item has no continuation offset")
            seen: set[int] = set()
            while offset is not None:
                if offset in seen or offset != sum(
                    utf16_length(part) for part in parts
                ):
                    raise ContractError("Invalid UTF-16 continuation offset")
                seen.add(offset)
                if len(seen) > 1024:
                    raise ContractError("Timeline item exceeds continuation limit")
                chunk = await self.call(
                    "t3_thread_read",
                    {
                        "threadId": thread_id,
                        "itemId": item["itemId"],
                        "textOffset": offset,
                        "maxCharsPerItem": 16000,
                    },
                )
                matches = [
                    entry
                    for entry in chunk.get("items", [])
                    if entry.get("itemId") == item["itemId"]
                ]
                if len(matches) != 1:
                    raise ContractError("Missing timeline continuation")
                continuation = matches[0]
                if continuation.get("runId") != item["runId"] or continuation.get(
                    "updatedAt"
                ) != item.get("updatedAt"):
                    raise ContractError(
                        "Timeline changed during text read; retry from saved cursor"
                    )
                parts.append(continuation["text"] or "")
                offset = continuation.get("nextTextOffset")
                if continuation.get("textTruncated") and offset is None:
                    raise ContractError("Truncated continuation has no offset")
            item["text"] = join_utf16(parts) if item["text"] is not None else None
            item["textTruncated"] = False
            item["nextTextOffset"] = None
        if result["hasMore"] and (
            result["nextPosition"] is None or result["nextPosition"] == after_position
        ):
            raise ContractError("Timeline cursor did not advance")
        return result

    read = read_thread

    async def wait(
        self, thread_id: str, run_id: str, *, timeout_ms: int = 30000
    ) -> dict[str, Any]:
        if not 0 < timeout_ms <= 60000:
            raise ValueError("wait timeout must be between 1 and 60000 ms")
        return await self.call(
            "t3_thread_wait",
            {"threadId": thread_id, "runId": run_id, "timeoutMs": timeout_ms},
        )

    async def launch(
        self,
        *,
        project_id: str,
        title: str,
        message: str,
        model_selection: dict[str, Any],
        workspace_strategy: dict[str, Any],
        runtime_mode: str = "full-access",
    ) -> dict[str, Any]:
        return await self.call(
            "t3_thread_launch",
            {
                "projectId": project_id,
                "title": title,
                "message": message,
                "modelSelection": model_selection,
                "workspaceStrategy": workspace_strategy,
                "runtimeMode": runtime_mode,
                "interactionMode": "default",
            },
        )

    async def send(
        self, thread_id: str, message: str, client_request_id: str, mode: str = "auto"
    ) -> dict[str, Any]:
        if not client_request_id:
            raise ValueError("send requires stable client_request_id")
        return await self.call(
            "t3_thread_send",
            {
                "threadId": thread_id,
                "message": message,
                "clientRequestId": client_request_id,
                "mode": mode,
            },
        )

    async def interrupt(
        self,
        thread_id: str,
        run_id: str | None,
        client_request_id: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        if not client_request_id:
            raise ValueError("interrupt requires stable client_request_id")
        return await self.call(
            "t3_thread_interrupt",
            {
                "threadId": thread_id,
                "runId": run_id,
                "clientRequestId": client_request_id,
                "reason": reason,
            },
        )
