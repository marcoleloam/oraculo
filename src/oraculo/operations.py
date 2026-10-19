from __future__ import annotations

import inspect
import json
import time
from collections.abc import Callable
from typing import Any

from oraculo.protocol import canonical_json, content_hash
from oraculo.state import DedupeConflict, State, StateError


class Operations:
    def __init__(self, state: State) -> None:
        self.state = state

    def get(self, operation_id: str) -> dict[str, Any] | None:
        return self.state._one("SELECT * FROM operations WHERE id=?", (operation_id,))

    def prepare(
        self,
        operation_id: str,
        attempt_id: str | None,
        kind: str,
        expected_state: dict[str, Any],
        *,
        generation: int | None = None,
        idempotency_key: str | None = None,
        checks_passed: bool = False,
    ) -> dict[str, Any]:
        digest = content_hash(
            {
                "attempt_id": attempt_id,
                "kind": kind,
                "expected_state": expected_state,
                "idempotency_key": idempotency_key,
            }
        )
        with self.state.transaction():
            previous = self.get(operation_id)
            if previous:
                if previous["payload_hash"] != digest:
                    raise DedupeConflict("operation identity reused")
                return previous
            if attempt_id:
                self.state.validate_grant(attempt_id, generation)
            elif kind != "coordinator_launch":
                raise StateError("execution operation requires grant")
            if kind in {"merge", "deploy"}:
                if (
                    not checks_passed
                    or not expected_state.get("commit")
                    or not expected_state.get("target")
                ):
                    raise StateError(
                        "delivery requires passing checks, expected commit and target"
                    )
                if kind == "deploy" and not expected_state.get("artifact"):
                    raise StateError("deploy requires an expected artifact")
            self.state.connection.execute(
                "INSERT INTO operations(id,attempt_id,kind,idempotency_key,expected_state,state,payload_hash,created_at) VALUES(?,?,?,?,?,'prepared',?,?)",
                (
                    operation_id,
                    attempt_id,
                    kind,
                    idempotency_key,
                    canonical_json(expected_state),
                    digest,
                    time.time(),
                ),
            )
            return self.get(operation_id)

    def mark_dispatched(self, operation_id: str) -> bool:
        with self.state.transaction():
            operation = self.get(operation_id)
            if not operation or operation["state"] != "prepared":
                return False
            if operation["attempt_id"]:
                attempt = self.state.get_attempt(operation["attempt_id"])
                self.state.validate_grant(attempt["id"], attempt["generation"])
            self.state.connection.execute(
                "UPDATE operations SET state='dispatched' WHERE id=?", (operation_id,)
            )
            return True

    async def dispatch(
        self, operation_id: str, effect: Callable[[], Any]
    ) -> dict[str, Any]:
        if not self.mark_dispatched(operation_id):
            return self.get(operation_id)
        try:
            observed = effect()
            if inspect.isawaitable(observed):
                observed = await observed
        except BaseException:
            self.mark_uncertain(operation_id)
            raise
        return self.report(operation_id, observed)

    def mark_uncertain(self, operation_id: str) -> None:
        with self.state.transaction():
            operation = self.get(operation_id)
            self.state.connection.execute(
                "UPDATE operations SET state='uncertain' WHERE id=? AND state!='confirmed'",
                (operation_id,),
            )
            if operation and operation["attempt_id"]:
                self.state.mark_uncertain(
                    operation["attempt_id"], "external effect uncertain"
                )

    def report(
        self,
        operation_id: str,
        observed_state: dict[str, Any],
        *,
        verified: bool = False,
    ) -> dict[str, Any]:
        with self.state.transaction():
            operation = self.get(operation_id)
            if not operation:
                raise StateError("unknown operation")
            if operation["state"] == "confirmed":
                return operation
            expected = json.loads(operation["expected_state"])
            matches = all(
                observed_state.get(key) == value for key, value in expected.items()
            )
            status = (
                "confirmed"
                if verified
                and matches
                and (
                    observed_state.get("healthy") is True
                    if operation["kind"] == "deploy"
                    else observed_state.get("healthy", True)
                )
                else "uncertain"
            )
            if observed_state.get("failed") is True:
                status = "failed"
            self.state.connection.execute(
                "UPDATE operations SET observed_state=?,state=? WHERE id=?",
                (canonical_json(observed_state), status, operation_id),
            )
            return self.get(operation_id)

    async def reconcile(
        self, operation_id: str, verify_destination: Callable[[dict[str, Any]], Any]
    ) -> dict[str, Any]:
        operation = self.get(operation_id)
        if not operation:
            raise StateError("unknown operation")
        observed = verify_destination(json.loads(operation["expected_state"]))
        if inspect.isawaitable(observed):
            observed = await observed
        return self.report(operation_id, observed, verified=True)
