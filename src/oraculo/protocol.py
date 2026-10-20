from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

MAX_ENVELOPE_BYTES = 1048576
CONTROL_MARKER = "ORACULO_CONTROL_V1"


class ProtocolError(ValueError):
    pass


def validate_memory_submission(payload: dict[str, Any]) -> None:
    """Require an explicit operation before memory work consumes a grant."""
    if not isinstance(payload, dict) or payload.get("kind") != "memory":
        return
    operation = payload.get("memory_operation")
    if not isinstance(operation, str) or operation not in {"ingest", "query", "lint"}:
        raise ProtocolError(
            "memory_operation_required: use 'memory ingest PROJECT --source FILE', "
            "'memory query PROJECT --query TEXT' or 'memory lint PROJECT'; "
            "structured requests must declare memory_operation"
        )
    if operation == "query" and (
        not isinstance(payload.get("query"), str) or not payload["query"].strip()
    ):
        raise ProtocolError(
            "memory_query_required: use 'memory query PROJECT --query TEXT'"
        )
    source = payload.get("source_path")
    evidence = payload.get("execution_evidence")
    if operation == "ingest" and not (
        (isinstance(source, str) and source.strip())
        or (isinstance(evidence, dict) and evidence)
    ):
        raise ProtocolError(
            "memory_source_required: use 'memory ingest PROJECT --source FILE' "
            "or supply nonempty execution_evidence in a structured request"
        )


class Origin(BaseModel):
    model_config = ConfigDict(extra="forbid")
    thread_id: str | None = None
    run_id: str | None = None
    message_id: str | None = None


class Envelope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1]
    request_id: str = Field(min_length=1, max_length=200)
    kind: Literal[
        "objective.submit",
        "objective.cancel",
        "plan.submit",
        "plan.extend",
        "plan.configure",
        "task.retry",
        "status.get",
        "capacity.get",
        "capacity.set",
        "settlement.bind",
        "settlement.status",
        "grant.acquire",
        "grant.claim",
        "execution.report",
        "operation.prepare",
        "operation.report",
        "memory.propose",
        "memory.read",
        "memory.cycle.begin",
        "memory.cycle.read",
        "memory.cycle.finish",
        "memory.cycle.status",
        "memory.cycle.retry",
        "memory.cycle.repair",
        "review.open",
        "review.close",
    ]
    project_id: str = Field(min_length=1, max_length=200)
    origin: Origin
    payload: dict[str, Any]

    @property
    def payload_hash(self) -> str:
        return content_hash(self.model_dump(mode="json"))


class Response(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str
    ok: bool
    state: str
    data: dict[str, Any] | None = None
    error: dict[str, Any] | None = None


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def content_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def parse_envelope(raw: bytes) -> Envelope:
    if len(raw) > MAX_ENVELOPE_BYTES:
        raise ProtocolError("envelope_too_large")
    return Envelope.model_validate_json(raw)


def is_control_event(message: dict[str, Any]) -> bool:
    return (
        message.get("marker") == CONTROL_MARKER or message.get("kind") == "status_event"
    )
