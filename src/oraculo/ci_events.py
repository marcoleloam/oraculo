"""Authenticated, durable CI observations. Uses the existing inbox/outbox and CAS.

Transport authentication is verified before parsing an event. No payload boolean
can assert authentication. The observer is a service-installed dependency; a
public request names a job, never an event or authentication result.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Literal

from pydantic import Field

from oraculo.config import StrictModel
from oraculo.protocol import content_hash
from oraculo.state import DedupeConflict, State, StateError


class CIBinding(StrictModel):
    repository: str = Field(min_length=1, max_length=300)
    pr: int = Field(ge=1, strict=True)
    head: str = Field(pattern=r"^[a-f0-9]{40}$")
    base: str = Field(pattern=r"^[a-f0-9]{40}$")
    run: str = Field(min_length=1, max_length=200)
    attempt: int = Field(ge=1, strict=True)


class CIEvent(CIBinding):
    version: Literal[1] = 1
    event_id: str = Field(min_length=1, max_length=200)
    sequence: int = Field(ge=1, strict=True)
    occurred_at: float = Field(ge=0, allow_inf_nan=False)
    conclusion: Literal["pending", "passed", "failed", "cancelled"]
    diagnostic: str = Field(default="", max_length=12000)


class SignedCIObserver:
    """Verify bytes delivered by a trusted host adapter using its pinned MAC key.

    reader(binding) returns (body_bytes, signature_hex). The key resolver and
    identity are installed by the service, never taken from the event or caller.
    No webhook server, paid request, or polling loop is created here.
    """

    def __init__(self, reader, key_resolver, key_reference, identity):
        self.reader, self.resolve = reader, key_resolver
        self.reference, self.identity = key_reference, identity

    async def read(self, binding):
        body, signature = await self.reader(binding.model_dump(mode="json"))
        if (
            not isinstance(body, bytes)
            or len(body) > 32768
            or not isinstance(signature, str)
        ):
            raise StateError("ci_authentication_failed")
        key = self.resolve(self.reference)
        if (
            not isinstance(key, bytes)
            or len(key) < 32
            or not hmac.compare_digest(
                hmac.new(key, body, hashlib.sha256).hexdigest(), signature
            )
        ):
            raise StateError("ci_authentication_failed")
        try:
            event = CIEvent.model_validate(json.loads(body))
        except ValueError:
            raise StateError("ci_schema_invalid") from None
        return event


class CIEvents:
    def __init__(self, state: State):
        self.state = state

    @staticmethod
    def key(project, binding):
        return "ci:v1:current:" + content_hash(
            [project, binding.repository, binding.pr]
        )

    async def observe(self, project, binding: CIBinding, observer: SignedCIObserver):
        if not isinstance(observer, SignedCIObserver):
            raise StateError("ci_trusted_observer_unavailable")
        if self.state.connection.in_transaction:
            raise StateError("ci_io_inside_transaction")
        event = await observer.read(binding)
        raw = event.model_dump(mode="json")
        identity = "ci:v1:event:" + content_hash(
            [
                project,
                observer.identity,
                event.repository,
                event.pr,
                event.head,
                event.base,
                event.run,
                event.attempt,
                event.event_id,
            ]
        )
        cursor = self.key(project, binding)
        with self.state.transaction():
            previous = self.state.get_meta(identity)
            if previous:
                if previous["event_hash"] != content_hash(raw):
                    raise DedupeConflict("ci_event_identity_reused")
                return dict(previous, replay=True)
            current = self.state.get_meta(cursor)
            matched = all(
                raw[k] == v for k, v in binding.model_dump(mode="json").items()
            )
            stale = current and (
                event.sequence <= current["event"]["sequence"]
                or (
                    event.head == current["event"]["head"]
                    and event.run == current["event"]["run"]
                    and event.attempt < current["event"]["attempt"]
                )
            )
            disposition = (
                "stale" if stale else "accepted" if matched else "binding_mismatch"
            )
            receipt = dict(
                event=raw,
                event_hash=content_hash(raw),
                disposition=disposition,
                source=observer.identity,
                observed_at=time.time(),
            )
            # Inbox and outbox intent commit together before scheduler admission.
            self.state.require_orchestration_schema()
            self.state.request_begin(identity, receipt)
            self.state.set_meta(identity, receipt)
            if disposition == "accepted":
                self.state.set_meta(cursor, receipt)
            self.state.enqueue_outbox(
                identity,
                {
                    "kind": "ci_observed",
                    "project_id": project,
                    "event_id": event.event_id,
                    "disposition": disposition,
                },
            )
            self.state.request_finish(identity, receipt)
            return receipt

    def current(self, project, binding: CIBinding):
        r = self.state.get_meta(self.key(project, binding))
        if not r or r["disposition"] != "accepted":
            return None
        if any(
            r["event"].get(k) != v for k, v in binding.model_dump(mode="json").items()
        ):
            return None
        return r

    def passed(self, project, raw_binding):
        try:
            binding = CIBinding.model_validate(raw_binding)
        except ValueError:
            return False
        current = self.current(project, binding)
        return current is not None and current["event"]["conclusion"] == "passed"
