from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

MAX_ENVELOPE_BYTES = 1048576
CONTROL_MARKER = "ORACULO_CONTROL_V1"


class ProtocolError(ValueError):
    pass


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
        "status.get",
        "grant.acquire",
        "grant.claim",
        "execution.report",
        "operation.prepare",
        "operation.report",
        "memory.propose",
        "memory.read",
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
