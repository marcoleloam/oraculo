from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

TERMINAL_STATUSES = frozenset(
    {"completed", "interrupted", "failed", "cancelled", "rolled_back"}
)
REQUIRED_TOOLS = frozenset(
    {
        "t3_project_list",
        "t3_thread_list",
        "t3_thread_launch",
        "t3_thread_read",
        "t3_thread_send",
        "t3_thread_wait",
        "t3_thread_interrupt",
        "orchestrator_capabilities",
    }
)


class ContractError(ValueError):
    def __init__(
        self, message: str, *, tool: str | None = None, direction: str | None = None
    ) -> None:
        super().__init__(message)
        self.tool = tool
        self.direction = direction


class RemoteModel(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)


class Run(RemoteModel):
    runId: str
    status: str


class Thread(RemoteModel):
    threadId: str
    activeRunId: str | None
    latestRunId: str | None
    status: str
    pendingRequestCount: int = Field(ge=0)


class TimelineItem(RemoteModel):
    itemId: str
    position: int
    runId: str | None
    text: str | None
    textTruncated: bool
    nextTextOffset: int | None = None


class ThreadRead(RemoteModel):
    thread: Thread
    recentRuns: list[Run]
    items: list[TimelineItem]
    hasMore: bool
    nextPosition: int | None


class SettlementThread(Thread):
    """Stricter optional state contract; legacy execution reads stay compatible."""

    threadId: str = Field(min_length=1)
    projectId: str = Field(min_length=1)
    parentThreadId: str | None
    settled: bool


class SettlementRead(ThreadRead):
    thread: SettlementThread


class SettlementListThread(RemoteModel):
    threadId: str = Field(min_length=1)
    parentThreadId: str | None
    status: str


class SettlementListPage(RemoteModel):
    projectId: str = Field(min_length=1)
    threads: list[SettlementListThread]
    nextCursor: int | None = Field(ge=0)


def settlement_child_clear(snapshot: SettlementRead) -> bool:
    """A failed/interrupted child is unresolved, even though its run is terminal."""
    thread = snapshot.thread
    if thread.activeRunId is not None or thread.pendingRequestCount != 0:
        return False
    if thread.status == "idle" and thread.latestRunId is None:
        return not snapshot.recentRuns
    if thread.status != "completed" or not thread.latestRunId:
        return False
    latest = [run for run in snapshot.recentRuns if run.runId == thread.latestRunId]
    return len(latest) == 1 and latest[0].status == "completed"


def execution_terminal(snapshot: dict[str, Any], expected_run_id: str) -> bool:
    parsed = ThreadRead.model_validate(snapshot)
    return (
        parsed.thread.activeRunId is None
        and parsed.thread.status in TERMINAL_STATUSES
        and parsed.thread.latestRunId == expected_run_id
        and parsed.thread.pendingRequestCount == 0
        and not snapshot.get("hasPendingChildRuns", False)
        and any(
            run.runId == expected_run_id and run.status in TERMINAL_STATUSES
            for run in parsed.recentRuns
        )
    )


def dispatch_retry_safe(snapshot: dict[str, Any], dispatch_run_id: str) -> bool:
    """Prove the prior dispatch ended and the coordinator is currently idle.

    A later terminal run does not invalidate the dispatch receipt. Both identities
    still need terminal evidence; an absent or contradictory run stays blocked.
    This is deliberately separate from execution completion, which must match
    the latest run of the executor.
    """
    parsed = ThreadRead.model_validate(snapshot)
    latest_run_id = parsed.thread.latestRunId
    if (
        not dispatch_run_id
        or not latest_run_id
        or parsed.thread.activeRunId is not None
        or parsed.thread.status not in TERMINAL_STATUSES
        or parsed.thread.pendingRequestCount != 0
        or snapshot.get("hasPendingChildRuns", False)
    ):
        return False
    for run_id in {dispatch_run_id, latest_run_id}:
        statuses = [run.status for run in parsed.recentRuns if run.runId == run_id]
        if not statuses or any(status not in TERMINAL_STATUSES for status in statuses):
            return False
    return True


def utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le", errors="surrogatepass")) // 2


def join_utf16(parts: list[str]) -> str:
    return b"".join(
        part.encode("utf-16-le", errors="surrogatepass") for part in parts
    ).decode("utf-16-le")
