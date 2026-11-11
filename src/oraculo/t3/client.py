from __future__ import annotations

import json
from contextlib import AsyncExitStack
from datetime import datetime
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
    SettlementListPage,
    SettlementRead,
    ThreadRead,
    join_utf16,
    settlement_child_clear,
    utf16_length,
)


class T3Error(RuntimeError):
    pass


class TransportError(T3Error):
    pass


class MutationUncertain(TransportError):
    pass


# A thread in one of these statuses holds, or is about to hold, execution.
LIVE_THREAD_STATUSES = ("preparing", "queued", "starting", "running", "waiting")
# Ended, and not with success.
UNSUCCESSFUL_THREAD_STATUSES = ("interrupted", "failed", "cancelled", "rolled_back")
# Every status the host knows except ``completed`` and ``idle``. The host
# reports ``idle`` for a thread that never ran, which the catalog rule already
# treats as clear. The filter cannot negate: a status this list does not name
# is not discovered.
UNCLEAR_THREAD_STATUSES = LIVE_THREAD_STATUSES + UNSUCCESSFUL_THREAD_STATUSES
# Rows per page of the filtered list: about 1.4 KB each on a real host.
_UNCLEAR_PAGE = 6
_UNCLEAR_PAGES = 200
_ANCESTOR_READS = 100
_CHAIN_DEPTH = 64


def _instant(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else None


READ_TOOLS = frozenset(
    {
        "t3_project_list",
        "t3_thread_list",
        "t3_thread_read",
        "t3_thread_wait",
        "orchestrator_capabilities",
        "list_thread_pull_requests",
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

    def validate_call(self, name: str, arguments: dict[str, Any]) -> None:
        """Check availability and input before reserving an irreversible intent."""
        if name not in self.tools:
            raise ContractError(f"Unavailable T3 tool: {name}")
        tool = self.tools[name]
        try:
            validator_for(tool["inputSchema"])(tool["inputSchema"]).validate(arguments)
        except SchemaValidationError as exc:
            raise ContractError(
                f"T3 input schema mismatch: {name}", tool=name, direction="input"
            ) from exc

    async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.validate_call(name, arguments)
        tool = self.tools[name]
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

    async def list_thread_pull_requests(self, thread_id: str) -> dict[str, Any]:
        return await self.call("list_thread_pull_requests", {"threadId": thread_id})

    async def link_pull_request(
        self, thread_id: str, pr: dict[str, Any]
    ) -> dict[str, Any]:
        # Do not permit a supplied PR object to override the authorized thread.
        identity = {
            key: pr[key] for key in ("host", "repository", "number", "url") if key in pr
        }
        if not identity.get("url") and not (
            identity.get("host")
            and identity.get("repository")
            and type(identity.get("number")) is int
            and identity["number"] > 0
        ):
            raise ContractError("Pull request requires URL or host/repository/number")
        return await self.call("link_pull_request", {**identity, "threadId": thread_id})

    async def settle_thread(self, thread_id: str) -> dict[str, Any]:
        """Return only the request receipt; confirmation requires a later read."""
        return await self.call(
            "t3_thread_organize", {"action": "settle", "threadId": thread_id}
        )

    def validate_settle(self, thread_id: str) -> None:
        self.validate_call(
            "t3_thread_organize", {"action": "settle", "threadId": thread_id}
        )

    async def _settlement_read(self, thread_id: str) -> SettlementRead:
        # Minimize timeline text; settlement needs durable state, not prose.
        payload = await self.call(
            "t3_thread_read",
            {
                "threadId": thread_id,
                "view": "activity",
                "limit": 1,
                "runLimit": 50,
                "maxCharsPerItem": 1,
            },
        )
        try:
            snapshot = SettlementRead.model_validate(payload)
        except ValidationError as exc:
            raise ContractError("Invalid T3 settlement read") from exc
        if snapshot.thread.threadId != thread_id:
            raise ContractError("Settlement read identity mismatch")
        return snapshot

    async def settlement_target(self, thread_id: str) -> dict[str, Any]:
        """One fresh, validated read of the target: its state and run chain."""
        snapshot = await self._settlement_read(thread_id)
        return {
            "thread": snapshot.thread.model_dump(mode="json"),
            "recentRuns": [run.model_dump(mode="json") for run in snapshot.recentRuns],
        }

    async def unclear_descendants(
        self,
        thread_id: str,
        project_id: str,
        parent_thread_id: str | None,
        created_at: str | None = None,
    ) -> dict[str, Any]:
        """Descendants of ``thread_id`` that are live or ended without success.

        Instead of the whole project catalog, the host is asked only for
        threads (subagents included) whose status is neither ``completed``
        nor ``idle``, newest first, in small pages. A descendant is created
        after its ancestor, so a row last updated before the target was
        created cannot be one; once such a row appears in a list that is
        ordered newest first, the remaining pages are not read. Without a
        usable creation time or order, every page of the filtered list is
        read.

        Each remaining row is traced through its parents up to a top-level
        thread, the target, or the target's own parent (an ancestor cannot be
        a descendant). A parent the list does not show costs one small read.

        What this proves: at the time of the reads, no thread of the project
        that the host reports as live, interrupted, failed, cancelled or
        rolled back descends from the target. Like the catalog proof it is
        project-scoped and not atomic. Unlike it, it trusts the host's status
        filter and timestamps, and it does not look at completed or idle
        threads at all: it names no successful descendant and cannot notice
        one whose row says ``completed`` while a run is active.
        """
        floor = _instant(created_at)
        rows: dict[str, Any] = {}
        kinds: dict[str, str] = {}
        seen_ids: set[str] = set()
        cursor = None
        seen_cursors: set[int] = set()
        ordered, previous, closed, pages = floor is not None, None, False, 0
        for _ in range(_UNCLEAR_PAGES):
            payload = await self.call(
                "t3_thread_list",
                {
                    "projectId": project_id,
                    "includeSubagents": True,
                    "statuses": list(UNCLEAR_THREAD_STATUSES),
                    "limit": _UNCLEAR_PAGE,
                    **({"cursor": cursor} if cursor is not None else {}),
                },
            )
            pages += 1
            try:
                page = SettlementListPage.model_validate(payload)
            except ValidationError as exc:
                raise ContractError("Invalid T3 settlement thread list") from exc
            if page.projectId != project_id:
                raise ContractError("Settlement list project mismatch")
            for row, raw in zip(page.threads, payload["threads"], strict=True):
                # A row seen twice means the list shifted between pages and
                # another row may have been skipped. Fail closed.
                if row.threadId in seen_ids:
                    raise ContractError("Duplicate settlement thread identity")
                seen_ids.add(row.threadId)
                updated = _instant(raw.get("updatedAt"))
                if updated is None or (previous is not None and updated > previous):
                    ordered = False
                previous = updated if updated is not None else previous
                if floor is not None and updated is not None and updated < floor:
                    closed = True
                    continue
                # A host that ignores the filter returns every thread. Judge
                # each row by its own fields; an unknown status is not clear.
                if raw.get("activeRunId") or raw.get("pendingRequestCount"):
                    kind = "live"
                elif row.status in UNSUCCESSFUL_THREAD_STATUSES:
                    kind = "unsuccessful"
                elif row.status in ("completed", "idle"):
                    continue
                else:
                    kind = "live"
                rows[row.threadId], kinds[row.threadId] = row, kind
            cursor = page.nextCursor
            if cursor is None or (closed and ordered):
                break
            if cursor in seen_cursors:
                raise ContractError("Repeated settlement thread list cursor")
            seen_cursors.add(cursor)
        else:
            raise ContractError("Settlement project page limit exceeded")
        parents: dict[str, str | None] = {
            row.threadId: row.parentThreadId for row in rows.values()
        }
        parents[thread_id] = parent_thread_id
        reads = 0
        found: dict[str, list[str]] = {"live": [], "unsuccessful": []}
        for row in rows.values():
            if row.threadId == thread_id:
                continue
            current, seen = row.parentThreadId, {row.threadId}
            while current is not None and current not in (thread_id, parent_thread_id):
                if current in seen or len(seen) > _CHAIN_DEPTH:
                    raise ContractError("Cyclic settlement parent links")
                seen.add(current)
                if current not in parents:
                    reads += 1
                    if reads > _ANCESTOR_READS:
                        raise ContractError("Settlement descendant limit exceeded")
                    ancestor = await self._settlement_read(current)
                    if ancestor.thread.projectId != project_id:
                        raise ContractError("Settlement descendant project mismatch")
                    parents[current] = ancestor.thread.parentThreadId
                current = parents[current]
            if current is not None and current == thread_id:
                found[kinds[row.threadId]].append(row.threadId)
        return {
            "target_live": kinds.get(thread_id) == "live",
            "live": sorted(found["live"]),
            "unsuccessful": sorted(found["unsuccessful"]),
            "rows": len(rows),
            "pages": pages,
            "ancestor_reads": reads,
            "window": "since_target_creation" if closed and ordered else "all_listed",
        }

    async def settlement_snapshot(
        self,
        thread_id: str,
        *,
        terminal_only: bool = False,
        complete_runs: bool = False,
        live_only: bool = False,
        root: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Read bounded descendant evidence, without watching or polling.

        The public list API is project-scoped. This proves only descendants
        visible in that project and is not an atomic lock against new work.

        ``live_only`` replaces the whole project catalog by the filtered list
        of ``unclear_descendants``. When no live or unsuccessful descendant
        is found, the snapshot carries no descendant rows: successful
        descendants are not named. A caller that needs their identities or
        run chains (a review, a delivery decision) must ask for the catalog.
        Any live or unsuccessful descendant, or a live target, falls back to
        the catalog. ``root`` is a ``settlement_target`` read of this target
        made by the caller in the same operation.
        """
        if live_only:
            # ``root`` comes from settlement_target, already validated there.
            target = root or await self.settlement_target(thread_id)
            if target["thread"]["threadId"] != thread_id:
                raise ContractError("Settlement read identity mismatch")
            found = await self.unclear_descendants(
                thread_id,
                target["thread"]["projectId"],
                target["thread"]["parentThreadId"],
                target["thread"].get("createdAt"),
            )
            if not (found["target_live"] or found["live"] or found["unsuccessful"]):
                return {
                    "thread": target["thread"],
                    "recentRuns": target["recentRuns"],
                    "hasPendingChildRuns": False,
                    "childrenObserved": True,
                    "childrenScope": "project_unclear_statuses",
                    "childrenProof": "status_filter",
                    "childrenProofWindow": found["window"],
                    "unclearThreadsObserved": found["rows"],
                    "observedChildCount": 0,
                    "childrenSuccessful": True,
                    "childrenTerminal": True,
                    "descendants": [],
                }
            # A descendant is live or ended without success. Only the complete
            # catalog can describe it; the target is read again below, exactly
            # as without this mode.
        root = await self._settlement_read(thread_id)
        project_id = root.thread.projectId
        rows: dict[str, Any] = {}
        cursor = None
        seen_cursors: set[int] = set()
        for _ in range(10):
            payload = await self.call(
                "t3_thread_list",
                {
                    "projectId": project_id,
                    "includeSubagents": True,
                    "limit": 100,
                    **({"cursor": cursor} if cursor is not None else {}),
                },
            )
            try:
                page = SettlementListPage.model_validate(payload)
            except ValidationError as exc:
                raise ContractError("Invalid T3 settlement thread list") from exc
            if page.projectId != project_id:
                raise ContractError("Settlement list project mismatch")
            if len(rows) + len(page.threads) > 1000:
                raise ContractError("Settlement project thread limit exceeded")
            for row in page.threads:
                if row.threadId in rows:
                    raise ContractError("Duplicate settlement thread identity")
                rows[row.threadId] = row
            cursor = page.nextCursor
            if cursor is None:
                break
            if cursor in seen_cursors:
                raise ContractError("Repeated settlement thread list cursor")
            seen_cursors.add(cursor)
        else:
            raise ContractError("Settlement project page limit exceeded")
        if thread_id not in rows:
            raise ContractError("Settlement thread absent from project list")
        if rows[thread_id].parentThreadId != root.thread.parentThreadId:
            raise ContractError("Settlement parent changed during read")
        children: dict[str, list[str]] = {}
        for row in rows.values():
            if row.parentThreadId is not None:
                children.setdefault(row.parentThreadId, []).append(row.threadId)
        pending = list(children.get(thread_id, []))
        visited = {thread_id}
        has_pending = False
        children_successful = True
        children_terminal = True
        descendants = []
        while pending:
            child_id = pending.pop()
            if child_id in visited:
                raise ContractError("Cyclic settlement parent links")
            visited.add(child_id)
            if len(visited) > 101:
                raise ContractError("Settlement descendant limit exceeded")
            child = await self._settlement_read(child_id)
            if child.thread.projectId != project_id:
                raise ContractError("Settlement descendant project mismatch")
            if child.thread.parentThreadId != rows[child_id].parentThreadId:
                raise ContractError("Settlement descendant parent changed during read")
            has_pending = has_pending or not settlement_child_clear(
                child, terminal_only=terminal_only
            )
            children_successful = children_successful and settlement_child_clear(child)
            terminal = settlement_child_clear(child, terminal_only=True)
            children_terminal = children_terminal and terminal
            descendants.append(
                {
                    "thread": child.thread.model_dump(mode="json"),
                    "recentRuns": [
                        run.model_dump(mode="json") for run in child.recentRuns
                    ],
                    "terminal": terminal,
                    "successful": settlement_child_clear(child),
                }
            )
            if complete_runs and settlement_child_clear(child, terminal_only=True):
                from oraculo.run_continuity import terminal_chain

                if child.thread.latestRunId is not None:
                    terminal_chain(child.model_dump(mode="json"))
                elif getattr(child.thread, "runCount", None) != 0:
                    raise ContractError("Settlement descendant history incomplete")
            pending.extend(children.get(child_id, []))
        # Refresh the target after descendant reads so newly active work blocks
        # the caller's policy, rather than relying on the initial target state.
        current = await self._settlement_read(thread_id)
        if (
            current.thread.projectId != project_id
            or current.thread.parentThreadId != root.thread.parentThreadId
        ):
            raise ContractError("Settlement target scope changed during read")
        return {
            "thread": current.thread.model_dump(mode="json"),
            "recentRuns": [run.model_dump(mode="json") for run in current.recentRuns],
            "hasPendingChildRuns": has_pending,
            "childrenObserved": True,
            "childrenScope": "project",
            "observedChildCount": len(visited) - 1,
            "childrenSuccessful": children_successful,
            "childrenTerminal": children_terminal,
            "descendants": sorted(descendants, key=lambda d: d["thread"]["threadId"]),
        }

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
