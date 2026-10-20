from __future__ import annotations

from typing import Any, Protocol

from oraculo.state import State, StateError
from oraculo.t3.contracts import ThreadRead, execution_terminal


def _terminal_status(
    snapshot: dict[str, Any], thread_id: str, run_id: str
) -> str | None:
    """Require an exact identity and unambiguous terminal evidence."""
    if snapshot["thread"]["threadId"] != thread_id or not execution_terminal(
        snapshot, run_id
    ):
        return None
    statuses = {
        run["status"] for run in snapshot["recentRuns"] if run["runId"] == run_id
    }
    return next(iter(statuses)) if len(statuses) == 1 else None


def _historical_stop(
    snapshot: dict[str, Any], thread_id: str, run_id: str
) -> str | None:
    """Recover only an interrupted original run, never a later run's success."""
    latest = snapshot["thread"]["latestRunId"]
    if (
        not latest
        or latest == run_id
        or not _terminal_status(snapshot, thread_id, latest)
    ):
        return None
    statuses = {
        run["status"] for run in snapshot["recentRuns"] if run["runId"] == run_id
    }
    if len(statuses) == 1 and statuses <= {"cancelled", "interrupted"}:
        return next(iter(statuses))
    return None


class Remote(Protocol):
    async def read_thread(
        self, thread_id: str, *, after_position: int | None = None
    ) -> dict[str, Any]: ...
    async def list_threads(
        self, project_id: str, *, title_contains: str | None = None
    ) -> list[dict[str, Any]]: ...
    async def interrupt(
        self,
        thread_id: str,
        run_id: str,
        client_request_id: str,
        reason: str | None = None,
    ) -> dict[str, Any]: ...


class ReconciliationResults(list[dict[str, Any]]):
    """Per-lease results, with the original single-executor mapping interface.

    Serial callers may still index by a string or compare with a dict. Multiple
    results deliberately have no implicit first-result mapping view.
    """

    def _legacy(self) -> dict[str, Any]:
        if not self:
            return {"state": "idle"}
        if len(self) == 1:
            return self[0]
        raise TypeError("multiple leases: iterate reconciliation results")

    def __getitem__(self, key):
        if isinstance(key, str):
            return self._legacy()[key]
        return super().__getitem__(key)

    def __eq__(self, other):
        if isinstance(other, dict):
            return len(self) <= 1 and self._legacy() == other
        return super().__eq__(other)

    def __ne__(self, other):
        return not self == other


class Reconciler:
    def __init__(self, state: State, remote: Remote) -> None:
        self.state = state
        self.remote = remote

    async def reconcile_launch(self, operation_id: str) -> dict[str, Any] | None:
        import json

        from oraculo.operations import Operations

        operations = Operations(self.state)
        operation = operations.get(operation_id)
        if not operation:
            return None
        expected = json.loads(operation["expected_state"])
        candidates = await self.remote.list_threads(
            expected["project_id"], title_contains=expected["marker"]
        )
        matches = []
        for candidate in candidates:
            if (
                candidate.get("projectId", expected["project_id"])
                != expected["project_id"]
            ):
                continue
            snapshot = await self.remote.read_thread(candidate["threadId"])
            thread = snapshot["thread"]
            if thread.get("projectId", expected["project_id"]) != expected[
                "project_id"
            ] or thread.get("branch") != expected.get("branch"):
                continue
            seen_positions: set[int] = set()
            while True:
                text = " ".join(
                    item.get("text") or "" for item in snapshot.get("items", [])
                )
                if expected["marker"] in text:
                    matches.append(candidate)
                    break
                cursor = snapshot.get("nextPosition")
                if (
                    not snapshot.get("hasMore")
                    or cursor is None
                    or cursor in seen_positions
                ):
                    break
                seen_positions.add(cursor)
                snapshot = await self.remote.read_thread(
                    candidate["threadId"], after_position=cursor
                )
        if len(matches) != 1:
            operations.mark_uncertain(operation_id)
            return None
        observed = dict(expected, thread_id=matches[0]["threadId"])
        operations.report(operation_id, observed, verified=True)
        return matches[0]

    async def reconcile(self) -> list[dict[str, Any]]:
        results = ReconciliationResults()
        for lease in self.state.leases():
            results.append(await self._reconcile_lease(lease))
        return results

    async def _reconcile_lease(self, lease: dict[str, Any]) -> dict[str, Any]:
        if self.state.lease(lease["attempt_id"]) != lease:
            return {"state": "uncertain", "reason": "lease_changed"}
        attempt = self.state.get_attempt(lease["attempt_id"])
        job = self.state.get_job(attempt["job_id"])
        objective = self.state.get_objective(job["objective_id"])
        if not attempt["thread_id"] or not attempt["run_id"]:
            self.state.mark_uncertain(attempt["id"], "awaiting execution identity")
            return {"state": "uncertain", "reason": "missing_execution_identity"}
        try:
            threads = await self.remote.list_threads(objective["project_id"])
            ids = {attempt["thread_id"]}
            while True:
                found = {
                    row["threadId"]
                    for row in threads
                    if row.get(
                        "parentThreadId",
                        row.get("parent_thread_id", row.get("subagentParentThreadId")),
                    )
                    in ids
                }
                if found.issubset(ids):
                    break
                ids |= found
            snapshots = {
                thread_id: await self.remote.read_thread(thread_id) for thread_id in ids
            }
            for snapshot in snapshots.values():
                ThreadRead.model_validate(snapshot)
            if any(
                snapshot["thread"]["threadId"] != thread_id
                for thread_id, snapshot in snapshots.items()
            ):
                raise ValueError("execution thread identity mismatch")
            if any(
                snapshot["thread"].get("projectId", objective["project_id"])
                != objective["project_id"]
                for snapshot in snapshots.values()
            ):
                raise ValueError("execution project identity mismatch")
            # Remote reads yield control: cancellation or another reconciliation
            # may have changed the lease in the meantime.
            if self.state.lease(attempt["id"]) != lease:
                return {"state": "uncertain", "reason": "lease_changed"}
            objective = self.state.get_objective(job["objective_id"])
            if objective["status"] == "cancel_requested":
                for thread_id, snapshot in snapshots.items():
                    active = snapshot["thread"].get("activeRunId")
                    if active:
                        await self.remote.interrupt(
                            thread_id,
                            active,
                            f"cancel:{attempt['id']}:{thread_id}:{active}",
                            "objective cancellation",
                        )
            root = snapshots[attempt["thread_id"]]
            remote_status = _terminal_status(
                root, attempt["thread_id"], attempt["run_id"]
            ) or _historical_stop(root, attempt["thread_id"], attempt["run_id"])
            children_terminal = all(
                _terminal_status(snapshot, thread_id, snapshot["thread"]["latestRunId"])
                for thread_id, snapshot in snapshots.items()
                if thread_id != attempt["thread_id"]
            )
            if not remote_status or not children_terminal:
                return {
                    "state": "cancel_requested"
                    if objective["status"] == "cancel_requested"
                    else "running"
                }
            report = self.state.get_meta(f"execution_report:{attempt['id']}", {})
            matching_report = (
                report.get("run_id") == attempt["run_id"]
                and report.get("generation") == attempt["generation"]
            )
            if objective["status"] == "cancel_requested":
                status = "stopped"
            elif remote_status in {"failed", "rolled_back"}:
                status = "failed"
            elif remote_status in {"interrupted", "cancelled"}:
                status = "stopped"
            elif (
                matching_report
                and report.get("status") in {"succeeded", "failed", "stopped"}
                and report.get("evidence")
            ):
                status = report["status"]
            else:
                self.state.mark_uncertain(
                    attempt["id"], "terminal run without verified execution report"
                )
                return {"state": "uncertain", "reason": "missing_execution_report"}
            if status == "succeeded":
                from oraculo.plans import Plans

                plans = Plans(self.state)
                if plans.get(objective["id"]):
                    try:
                        plans.validate_report(attempt, report)
                    except StateError as error:
                        self.state.mark_uncertain(attempt["id"], str(error))
                        return {
                            "state": "uncertain",
                            "reason": "invalid_execution_report",
                        }
            evidence = report
            if root["thread"]["latestRunId"] != attempt["run_id"]:
                evidence = {
                    "reconciliation": "historical_stop",
                    "thread_id": attempt["thread_id"],
                    "run_id": attempt["run_id"],
                    "generation": attempt["generation"],
                    "remote_status": remote_status,
                    "latest_run_id": root["thread"]["latestRunId"],
                    "tree_terminal": True,
                }
            released = self.state.record_execution(
                attempt["id"],
                attempt["generation"],
                attempt["run_id"],
                status,
                pending_children=False,
                tree_confirmed=True,
                evidence=evidence,
            )
            return {
                "state": self.state.get_job(attempt["job_id"])["state"]
                if released
                else "uncertain",
                "released": released,
            }
        except (OSError, TimeoutError, ConnectionError, ValueError) as error:
            self.state.mark_uncertain(attempt["id"], type(error).__name__)
            return {"state": "uncertain", "reason": type(error).__name__}

    async def tick(self) -> list[dict[str, Any]]:
        return await self.reconcile()
