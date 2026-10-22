"""Daemon-owned managed state with a caller-bound, correlated socket relay.

The local operator is the trust boundary. ACKs attest neither cryptographic host
identity nor enforcement outside cooperative entrypoints.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import time
import uuid

from oraculo.protocol import MAX_ENVELOPE_BYTES, canonical_json
from oraculo.state import StateError

PROTOCOL_VERSION = 1
# The daemon serializes managed operations while a caller-bound bridge answers
# its frames. These bounds must exceed the bridge's own ACK wait (600 s default)
# so a slow but honest acknowledgement is not reported as uncertain twice.
HOST_ACK_TIMEOUT = 900
HOST_FRAME_TIMEOUT = 1200
# Background calls have no manual bridge to wait for. A failed call ends this
# batch's host traffic; pending durable returns retain their original keys.
BACKGROUND_HOST_TIMEOUT = 60


async def close_writer(writer):
    writer.close()
    try:
        async with asyncio.timeout(5):
            await writer.wait_closed()
    except (OSError, TimeoutError):
        pass


class ManagedService:
    def __init__(self, service):
        from oraculo.task_store import TaskStore

        self.service = service
        path = service.config.state_dir / "managed-tasks.sqlite3"
        self.fd = os.open(
            path.with_suffix(".lock"), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
        )
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.store = TaskStore(path)
            self.store.authority_owned = True
            untracked = []
            for row in self.store.db.execute("SELECT id FROM managed_tasks"):
                task = self.store.get(row[0])
                if (
                    (task.get("launch_attempted") or task.get("child"))
                    and task.get("work_status")
                    not in {"succeeded", "failed", "stopped"}
                    and not service.state._one(
                        "SELECT id FROM managed_reservations WHERE id=?", (row[0],)
                    )
                ):
                    untracked.append(row[0])
            service.state.set_meta("managed_legacy_untracked", untracked)
        except BaseException:
            os.close(self.fd)
            raise
        self.busy = False
        self.activity = None
        self.owner = None
        self.reader = None
        self.background_worker = None

    def begin(self, mode, operation_id, *, action=None, task_id=None, reader=None):
        if self.busy:
            raise StateError("managed_adapter_busy_retry_same_key")
        self.busy = True
        self.owner = asyncio.current_task()
        self.reader = reader
        self.activity = dict(
            mode=mode,
            operation_id=operation_id,
            action=action,
            task_id=task_id,
            started_at=time.time(),
            started_monotonic=time.monotonic(),
            phase="adapter",
            phase_monotonic=time.monotonic(),
        )

    def phase(self, phase, **details):
        self.activity = dict(
            self.activity,
            phase=phase,
            phase_monotonic=time.monotonic(),
            **details,
        )

    def end(self):
        self.busy = False
        self.activity = self.owner = self.reader = None

    def diagnostic(self):
        if not self.busy:
            return dict(state="idle", busy=False)
        if self.activity is None or self.owner is None:
            return dict(
                state="owner_unknown", busy=True, recovery="inspect_do_not_clear"
            )
        activity = dict(self.activity)
        activity["age_seconds"] = round(
            time.monotonic() - activity.pop("started_monotonic"), 3
        )
        activity["phase_age_seconds"] = round(
            time.monotonic() - activity.pop("phase_monotonic"), 3
        )
        return dict(
            activity,
            busy=True,
            state="owner_finished" if self.owner.done() else "active",
            client_eof=self.reader.at_eof() if self.reader else None,
            recovery="wait_then_retry_same_intent",
        )

    def memory(self):
        from oraculo.task_transport import SocketMemory

        facade = self

        class Memory(SocketMemory):
            async def _call(self, action, project, actor, payload):
                facade.phase("memory", memory_action=action)
                try:
                    return await super()._call(action, project, actor, payload)
                finally:
                    facade.phase("adapter", memory_action=None)

        return Memory(self.service.config)

    def close(self):
        self.store.close()
        os.close(self.fd)

    def with_admission(self, task):
        if "id" not in task:
            return task
        row = self.service.state._one(
            "SELECT * FROM managed_reservations WHERE id=?", (task["id"],)
        )
        untracked = task["id"] in (
            self.service.state.get_meta("managed_legacy_untracked") or []
        )
        current = (
            dict(row)
            if row
            else {"state": "untracked" if untracked else "not_reserved"}
        )
        current["source"] = "service"
        return dict(task, admission_at_launch=task.get("admission"), admission=current)

    def describe(self, action, payload):
        if action == "history":
            return dict(
                self.store.history(payload["task_id"], payload.get("offset", 0)),
                source="service",
                protocol_version=1,
                adapter=self.diagnostic(),
            )
        if action == "status":
            return dict(
                self.with_admission(self.store.describe(payload["task_id"])),
                source="service",
                protocol_version=1,
                adapter=self.diagnostic(),
            )
        return dict(
            tasks=[
                self.with_admission(self.store.describe(r[0]))
                for r in self.store.db.execute(
                    "SELECT id FROM managed_tasks ORDER BY rowid DESC"
                )
            ],
            source="service",
            protocol_version=1,
            adapter=self.diagnostic(),
        )

    async def execute(self, envelope, reader, writer):
        from oraculo.managed_tasks import ManagedTasks
        from oraculo.pr_lifecycle import GitHubPRLifecycle
        from oraculo.publication import GitHubPublisher

        if self.busy:
            raise StateError("managed_adapter_busy_retry_same_key")
        if envelope.payload.get("protocol_version") != PROTOCOL_VERSION:
            raise StateError("managed_protocol_incompatible")
        operation = envelope.request_id
        self.begin(
            "caller",
            operation,
            action=envelope.payload.get("action"),
            task_id=envelope.payload.get("payload", {}).get("task_id"),
            reader=reader,
        )
        facade = self

        class Relay:
            failed = False

            async def call(self, name, arguments):
                if self.failed:
                    raise StateError("managed_host_channel_failed")
                call_id = uuid.uuid4().hex
                frame = dict(
                    marker="ORACULO_SERVICE_HOST_CALL_V1",
                    operation_id=operation,
                    request_id=call_id,
                    tool=name,
                    arguments=arguments,
                )
                raw = canonical_json(frame).encode() + b"\n"
                if len(raw) > MAX_ENVELOPE_BYTES:
                    raise StateError("managed_host_frame_limit")
                facade.phase(
                    "host_ack",
                    tool=name,
                    host_request_id=call_id,
                    timeout_seconds=HOST_ACK_TIMEOUT,
                )
                try:
                    # Includes backpressure, not just a silent reader. EOF is
                    # uncertainty, never evidence that the host did nothing.
                    async with asyncio.timeout(HOST_ACK_TIMEOUT):
                        writer.write(raw)
                        await writer.drain()
                        raw_ack = await reader.readline()
                    if not raw_ack:
                        raise StateError("managed_host_ack_eof")
                    if len(raw_ack) > MAX_ENVELOPE_BYTES:
                        raise StateError("managed_host_ack_limit")
                    ack = json.loads(raw_ack)
                except (OSError, TimeoutError, ValueError, StateError) as exc:
                    self.failed = True
                    if isinstance(exc, StateError):
                        raise
                    reason = (
                        "timeout"
                        if isinstance(exc, TimeoutError)
                        else "disconnected"
                        if isinstance(exc, OSError)
                        else "invalid"
                    )
                    raise StateError("managed_host_ack_" + reason) from None
                if (
                    not isinstance(ack, dict)
                    or ack.get("operation_id") != operation
                    or ack.get("request_id") != call_id
                    or (
                        not isinstance(ack.get("result"), dict)
                        and ack.get("error") != "host_response_uncertain"
                    )
                ):
                    self.failed = True
                    raise StateError("managed_host_ack_mismatch")
                if ack.get("error"):
                    self.failed = True
                    raise StateError("host_response_uncertain")
                facade.phase(
                    "adapter", tool=None, host_request_id=None, timeout_seconds=None
                )
                return ack["result"]

        try:
            adapter = ManagedTasks(
                self.service.config,
                self.store,
                Relay(),
                self.memory(),
                GitHubPublisher(),
                GitHubPRLifecycle(),
                admission=self.service.state,
            )
            adapter.config_path = self.service.config_path
            result = await adapter.command(
                envelope.payload["action"], envelope.payload["payload"]
            )
            return dict(
                self.with_admission(result), source="service", protocol_version=1
            )
        finally:
            self.end()


async def command(config, action, payload, transport):
    from oraculo.cli import make_envelope, request

    if action in {"list", "status", "history"}:
        result = await request(
            config,
            make_envelope("managed.read", "*", dict(action=action, payload=payload)),
        )
        if not result.get("ok"):
            raise StateError(
                result.get("error", {}).get("message", "managed_read_failed")
            )
        return result["data"]
    envelope = make_envelope(
        "managed.execute",
        "*",
        dict(protocol_version=PROTOCOL_VERSION, action=action, payload=payload),
    )
    operation = envelope["request_id"]
    reader, writer = await asyncio.open_unix_connection(
        str(config.socket_path), limit=MAX_ENVELOPE_BYTES + 1
    )
    try:
        writer.write(canonical_json(envelope).encode() + b"\n")
        await writer.drain()
        while True:
            try:
                async with asyncio.timeout(HOST_FRAME_TIMEOUT):
                    raw = await reader.readline()
                if not raw or len(raw) > MAX_ENVELOPE_BYTES:
                    raise ValueError("invalid_response")
                frame = json.loads(raw)
                if not isinstance(frame, dict):
                    raise ValueError("invalid_response")
            except (OSError, ValueError, TimeoutError):
                raise StateError(
                    "managed_response_uncertain_resume_same_intent"
                ) from None
            if frame.get("marker") != "ORACULO_SERVICE_HOST_CALL_V1":
                if not frame.get("ok"):
                    raise StateError(
                        frame.get("error", {}).get(
                            "message", "managed_operation_failed"
                        )
                    )
                return frame["data"]
            if frame.get("operation_id") != operation:
                raise StateError("managed_operation_mismatch")
            acknowledgement = dict(
                operation_id=operation, request_id=frame["request_id"]
            )
            try:
                acknowledgement["result"] = await transport.call(
                    frame["tool"], frame["arguments"]
                )
            except (OSError, ValueError, TimeoutError, StateError):
                acknowledgement["error"] = "host_response_uncertain"
            ack = canonical_json(acknowledgement).encode() + b"\n"
            if len(ack) > MAX_ENVELOPE_BYTES:
                raise StateError("managed_ack_limit")
            writer.write(ack)
            await writer.drain()
    finally:
        await close_writer(writer)
