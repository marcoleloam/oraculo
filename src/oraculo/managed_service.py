"""Daemon-owned managed state with a caller-bound, correlated socket relay.

The local operator is the trust boundary. ACKs attest neither cryptographic host
identity nor enforcement outside cooperative entrypoints.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import fcntl
import json
import os
import threading
import time
import uuid

from oraculo.protocol import MAX_ENVELOPE_BYTES, canonical_json
from oraculo.state import StateError

PROTOCOL_VERSION = 1
# A host frame occupies only its own scope. These bounds must exceed the
# bridge's ACK wait (600 s default) so a slow but honest acknowledgement is
# not reported as uncertain twice. Other scopes keep running.
HOST_ACK_TIMEOUT = 900
HOST_FRAME_TIMEOUT = 1200
# Background calls have no manual bridge to wait for. A failed call ends this
# batch's host traffic; pending durable returns retain their original keys.
BACKGROUND_HOST_TIMEOUT = 60
# Retained so callers can still recognize the old queue bound. Same-scope
# contention now fails immediately; this is not a cross-task wait.
CALLER_QUEUE_TIMEOUT = 60
MAX_QUEUED_CALLERS = 32
_VIEW_FIELDS = ("tool", "host_request_id", "timeout_seconds", "memory_action")


class _LoopCursor:
    """Cursor whose fetches run on the connection's owner thread."""

    def __init__(self, real, hop):
        self._real = real
        self._hop = hop

    def fetchone(self):
        return self._hop(self._real.fetchone)

    def fetchall(self):
        return self._hop(self._real.fetchall)

    def __iter__(self):
        return iter(self._hop(lambda: list(self._real)))

    def __getattr__(self, name):
        attr = getattr(self._real, name)
        if not callable(attr):
            return self._hop(lambda: getattr(self._real, name))

        def wrapped(*args, **kwargs):
            return self._hop(lambda: attr(*args, **kwargs))

        return wrapped


class _LoopConnection:
    """Ledger connection pinned to the daemon thread.

    Sync publisher and lifecycle callbacks run on the executor thread and still
    read this ledger. SQLite refuses that unless the call is handed back here.
    The daemon loop is awaiting that executor, so the handoff cannot deadlock.
    """

    _CURSOR_METHODS = frozenset({"execute", "executemany", "executescript", "cursor"})

    def __init__(self, real, loop, owner):
        self._real = real
        self._loop = loop
        self._owner = owner

    def _hop(self, fn):
        if threading.current_thread() is self._owner:
            return fn()
        future = concurrent.futures.Future()

        def run():
            try:
                future.set_result(fn())
            except BaseException as exc:
                future.set_exception(exc)

        try:
            self._loop.call_soon_threadsafe(run)
        except RuntimeError:
            future.cancel()
            raise
        return future.result()

    def __getattr__(self, name):
        attr = getattr(self._real, name)
        if not callable(attr):
            return self._hop(lambda: getattr(self._real, name))

        def wrapped(*args, **kwargs):
            result = self._hop(lambda: attr(*args, **kwargs))
            if name in self._CURSOR_METHODS:
                return _LoopCursor(result, self._hop)
            return result

        return wrapped

    def atomic(self, fn):
        """Run fn inside one IMMEDIATE transaction on the owner thread.

        Nested calls join the open transaction and must not commit it. The
        body runs to completion before any other hop, and must not await.
        """

        def body():
            nested = self._real.in_transaction
            if not nested:
                self._real.execute("BEGIN IMMEDIATE")
            try:
                result = fn()
            except BaseException:
                if not nested:
                    self._real.rollback()
                raise
            else:
                if not nested:
                    self._real.commit()
                return result

        return self._hop(body)


def public_scope(scope):
    if not scope:
        return None
    kind = scope[0]
    if kind == "intent":
        return {"kind": "intent", "origin": scope[1], "key": scope[2]}
    if kind == "task":
        return {"kind": "task", "task_id": scope[1]}
    return {"kind": str(kind)}


def operation_scope(action, payload, actor=None):
    """Scope that must not run twice at once.

    ``start`` is keyed by the origin thread and the idempotency key, and that
    identity exists only after the caller-bound host read. Every other action
    names the durable task directly.
    """
    body = payload if isinstance(payload, dict) else {}
    if action == "start":
        if not actor:
            return None
        key = body.get("key")
        if not isinstance(key, str):
            key = ""
        return ("intent", actor["thread_id"], key)
    task_id = body.get("task_id")
    if isinstance(task_id, str) and task_id:
        return ("task", task_id)
    return None


class ScopeBusy(StateError):
    """The same task or start intent already has an operation in progress."""

    def __init__(self, operation_id, scope=None, *, public=None):
        super().__init__("managed_scope_busy")
        self.operation_id = operation_id
        self.retryable = True
        self.scope = public if public is not None else public_scope(scope)


class OperationLease:
    """Worker-side claim for the scope of one admitted operation."""

    def __init__(self, facade, operation_id):
        self.facade = facade
        self.operation_id = operation_id

    def claim(self, scope):
        self.facade.acquire_scope(self.operation_id, scope)


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
            self.store = TaskStore(path, schema_max=max(5, service.state.schema_max))
            self.store.authority_owned = True
            from oraculo.rollout import verifier_binding

            self.store.rollout_binding = lambda env: verifier_binding(
                service.config.rollout_targets.get(env), service.config_path
            )
            untracked = []
            for row in self.store.db.execute("SELECT id FROM managed_tasks"):
                task = self.store.get(row[0])
                if (
                    (task.get("launch_attempted") or task.get("child"))
                    and task.get("work_status")
                    not in {"succeeded", "failed", "stopped", "cancelled"}
                    and not service.state._one(
                        "SELECT id FROM managed_reservations WHERE id=?", (row[0],)
                    )
                ):
                    untracked.append(row[0])
            service.state.set_meta("managed_legacy_untracked", untracked)
            self._pin_ledger()
        except BaseException:
            os.close(self.fd)
            raise
        self._lock = threading.Lock()
        self._operations = {}
        self._scopes = {}
        self._forced_busy = False
        # Not the default executor. Memory and other loop work use that pool;
        # filling it with managed git/gh/ACK waits would stall memory.cycle.*.
        self._workers = concurrent.futures.ThreadPoolExecutor(
            max_workers=MAX_QUEUED_CALLERS + 4,
            thread_name_prefix="oraculo-managed",
        )
        self.owner = None
        self.reader = None
        self.background_worker = None
        self.waiters = []

    @property
    def busy(self):
        with self._lock:
            return self._forced_busy or bool(self._operations)

    @busy.setter
    def busy(self, value):
        # Tests use this to model an owner the table does not track.
        with self._lock:
            self._forced_busy = bool(value)

    def _pin_ledger(self):
        """Bind the facade ledger to the daemon thread once a loop exists.

        Service startup can open the ledger before the loop is running. Sync
        callbacks on the executor thread reach this same object later.
        """
        if isinstance(self.store.db, _LoopConnection):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        owner = threading.current_thread()
        ledger = _LoopConnection(self.store.db, loop, owner)
        self.store.db = ledger
        if self.store.state is not None:
            self.store.state.connection = ledger
        # Sync callbacks also read admission (capacity, reservations) on this
        # connection. Hand those calls back; do not share it across threads.
        admission = self.service.state
        if not isinstance(admission.connection, _LoopConnection):
            admission.connection = _LoopConnection(admission.connection, loop, owner)

    def _emit(self, event, fields):
        try:
            self.service.log.info(canonical_json(dict(fields, event=event)))
        except Exception:
            return

    def _log_fields(self, op):
        return {
            "operation_id": op["operation_id"],
            "action": op.get("action"),
            "task_id": op.get("task_id"),
            "scope": public_scope(op.get("scope")),
            "mode": op["mode"],
            "phase": op.get("phase"),
            "wait_seconds": op.get("wait_seconds", 0),
        }

    def _new_operation(self, operation_id, mode, action, task_id, reader):
        now = time.monotonic()
        return {
            "operation_id": operation_id,
            "mode": mode,
            "action": action,
            "task_id": task_id,
            "scope": None,
            "phase": "adapter",
            "started_at": time.time(),
            "started_monotonic": now,
            "phase_monotonic": now,
            "wait_seconds": 0,
            "reader": reader,
            "owner": None,
        }

    def admit(self, mode, operation_id, *, action=None, task_id=None, reader=None):
        """Track one operation. Caller admission is capped; background is not."""
        started = None
        rejected = None
        with self._lock:
            if operation_id in self._operations:
                return
            if mode == "caller":
                callers = sum(
                    1 for op in self._operations.values() if op["mode"] == "caller"
                )
                if callers >= MAX_QUEUED_CALLERS:
                    rejected = {
                        "action": action,
                        "mode": mode,
                        "task_id": task_id,
                        "wait_seconds": 0,
                        "reason": "managed_adapter_busy_retry_same_key",
                    }
            if rejected is None:
                op = self._new_operation(
                    operation_id, mode, action, task_id, reader
                )
                if mode == "caller":
                    task = asyncio.current_task()
                    op["owner"] = task
                    self.owner = task
                if reader is not None:
                    self.reader = reader
                self._operations[operation_id] = op
                started = self._log_fields(op)
        if rejected is not None:
            self._emit("managed_operation_rejected", rejected)
            raise StateError("managed_adapter_busy_retry_same_key")
        if started is not None:
            self._emit("managed_operation_started", started)

    def acquire_scope(self, operation_id, scope):
        """Take one scope, or raise ScopeBusy naming the operation that holds it.

        The same operation claiming the scope it already holds is a no-op, so
        the daemon can reserve a task scope before the worker repeats the claim.
        """
        if scope is None:
            return
        with self._lock:
            op = self._operations.get(operation_id)
            if op is None:
                raise StateError("managed_operation_missing")
            if op.get("scope") == scope:
                return
            holder = self._scopes.get(scope)
            if holder is not None and holder != operation_id:
                raise ScopeBusy(holder, scope)
            current = op.get("scope")
            if current is not None and self._scopes.get(current) == operation_id:
                self._scopes.pop(current, None)
            self._scopes[scope] = operation_id
            op["scope"] = scope
            if scope[0] == "task":
                op["task_id"] = scope[1]

    def try_background(self, operation_id, task_id):
        """Occupy one task for reconciliation, or return False when it is held."""
        scope = ("task", task_id)
        started = None
        with self._lock:
            if self._scopes.get(scope) or operation_id in self._operations:
                return False
            op = self._new_operation(
                operation_id, "background", "reconcile", task_id, None
            )
            op["scope"] = scope
            self._operations[operation_id] = op
            self._scopes[scope] = operation_id
            started = self._log_fields(op)
        self._emit("managed_operation_started", started)
        return True

    def phase(self, phase, operation_id=None, **details):
        with self._lock:
            if operation_id is None:
                if len(self._operations) != 1:
                    return
                operation_id = next(iter(self._operations))
            op = self._operations.get(operation_id)
            if op is None:
                return
            op["phase"] = phase
            op["phase_monotonic"] = time.monotonic()
            op.update(details)

    def finish(self, operation_id):
        fields = None
        with self._lock:
            op = self._operations.pop(operation_id, None)
            if op is None:
                return
            scope = op.get("scope")
            if scope is not None and self._scopes.get(scope) == operation_id:
                self._scopes.pop(scope, None)
            if self.owner is op.get("owner"):
                self.owner = next(
                    (
                        item.get("owner")
                        for item in self._operations.values()
                        if item.get("owner") is not None
                    ),
                    None,
                )
            if self.reader is op.get("reader"):
                self.reader = next(
                    (
                        item.get("reader")
                        for item in self._operations.values()
                        if item.get("reader") is not None
                    ),
                    None,
                )
            fields = self._log_fields(op)
            fields["duration_seconds"] = round(
                time.monotonic() - op["started_monotonic"], 3
            )
        self._emit("managed_operation_finished", fields)

    def _view(self, op):
        now = time.monotonic()
        reader = op.get("reader")
        owner = op.get("owner")
        view = {
            "mode": op["mode"],
            "operation_id": op["operation_id"],
            "action": op.get("action"),
            "task_id": op.get("task_id"),
            "scope": public_scope(op.get("scope")),
            "phase": op.get("phase"),
            "age_seconds": round(now - op["started_monotonic"], 3),
            "phase_age_seconds": round(now - op["phase_monotonic"], 3),
            "wait_seconds": op.get("wait_seconds", 0),
            "state": "owner_finished"
            if owner is not None and owner.done()
            else "active",
            "client_eof": reader.at_eof() if reader is not None else None,
        }
        for key in _VIEW_FIELDS:
            if key in op:
                view[key] = op[key]
        return view

    def diagnostic(self):
        with self._lock:
            forced = self._forced_busy
            operations = [dict(op) for op in self._operations.values()]
        if not operations:
            if forced:
                return dict(
                    state="owner_unknown",
                    busy=True,
                    recovery="inspect_do_not_clear",
                    queued_callers=0,
                    operations=[],
                )
            return dict(state="idle", busy=False, queued_callers=0, operations=[])
        views = [
            self._view(op)
            for op in sorted(operations, key=lambda item: item["started_monotonic"])
        ]
        return dict(
            dict(views[0]),
            busy=True,
            recovery="wait_then_retry_same_intent",
            queued_callers=0,
            operations=views,
        )

    def memory(self):
        from oraculo.task_transport import SocketMemory

        facade = self

        class Memory(SocketMemory):
            def __init__(self, config):
                super().__init__(config)
                # Reconcile and the caller each stamp this before use. The
                # factory itself stays zero-arg so existing adapters still fit.
                self.operation_id = None

            async def _call(self, action, project, actor, payload):
                facade.phase(
                    "memory",
                    operation_id=self.operation_id,
                    memory_action=action,
                )
                try:
                    return await super()._call(action, project, actor, payload)
                finally:
                    facade.phase(
                        "adapter",
                        operation_id=self.operation_id,
                        memory_action=None,
                    )

        return Memory(self.service.config)

    def close(self):
        self.store.close()
        os.close(self.fd)
        self._workers.shutdown(wait=False, cancel_futures=True)

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
        from oraculo.pr_lifecycle import RepositoryPRLifecycle
        from oraculo.publication import RepositoryPublisher

        if envelope.payload.get("protocol_version") != PROTOCOL_VERSION:
            raise StateError("managed_protocol_incompatible")
        self._pin_ledger()
        action = envelope.payload.get("action")
        body = envelope.payload.get("payload")
        if not isinstance(body, dict):
            body = {}
        raw_task = body.get("task_id")
        operation = envelope.request_id
        self.admit(
            "caller",
            operation,
            action=action,
            task_id=raw_task if isinstance(raw_task, str) and raw_task else None,
            reader=reader,
        )
        worker = None
        facade = self
        main_loop = asyncio.get_running_loop()
        try:
            if reader.at_eof():
                raise StateError("managed_host_ack_eof")
            scope = operation_scope(action, body)
            if scope is not None:
                self.acquire_scope(operation, scope)

            class Relay:
                failed = False

                async def call(self, name, arguments):
                    # command() runs on a worker loop. Socket IO stays on this one.
                    if asyncio.get_running_loop() is main_loop:
                        return await self._on_main(name, arguments)
                    future = asyncio.run_coroutine_threadsafe(
                        self._on_main(name, arguments), main_loop
                    )
                    return await asyncio.wrap_future(future)

                async def _on_main(self, name, arguments):
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
                        operation_id=operation,
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
                        "adapter",
                        operation_id=operation,
                        tool=None,
                        host_request_id=None,
                        timeout_seconds=None,
                    )
                    return ack["result"]

            def run_command():
                # Same ledger and admission objects the daemon and in-process
                # callbacks already hold. SQLite calls hop back to this loop;
                # git, gh and the publisher stay on this worker so the loop can
                # serve other clients. Do not close those shared connections.
                memory = self.memory()
                memory.operation_id = operation
                adapter = ManagedTasks(
                    self.service.config,
                    self.store,
                    Relay(),
                    memory,
                    RepositoryPublisher(),
                    RepositoryPRLifecycle(),
                    admission=self.service.state,
                    scopes=OperationLease(facade, operation),
                )
                adapter.config_path = self.service.config_path
                adapter.decisions = self.service.decisions
                return asyncio.run(
                    adapter.command(
                        envelope.payload["action"], envelope.payload["payload"]
                    )
                )

            # Own the executor future for the whole call. Cancelling this
            # connection releases only this operation's scope. The dedicated
            # pool keeps the daemon loop and its memory/read threads free.
            worker = main_loop.run_in_executor(self._workers, run_command)
            try:
                result = await asyncio.shield(worker)
            except asyncio.CancelledError:
                # connection() closes the socket only after execute returns, and
                # the worker's host read is a different task. Abort here so that
                # read ends, then wait until the worker has left the ledger.
                transport = writer.transport
                if transport is not None:
                    transport.abort()
                try:
                    await worker
                except Exception:
                    pass
                raise
            return dict(
                self.with_admission(result), source="service", protocol_version=1
            )
        finally:
            if worker is None or worker.done():
                if worker is not None and not worker.cancelled():
                    worker.exception()
                self.finish(operation)
            else:

                def _release(fut, loop=main_loop, op=operation):
                    if not fut.cancelled():
                        fut.exception()
                    try:
                        loop.call_soon_threadsafe(lambda: self.finish(op))
                    except RuntimeError:
                        pass

                worker.add_done_callback(_release)


async def command(config, action, payload, transport):
    from oraculo.cli import make_envelope, request

    if action in {"diagnose-delivery", "recover-delivery"}:
        capabilities = await request(
            config, make_envelope("managed.capabilities", "*", {})
        )
        if (
            not capabilities.get("ok")
            or capabilities.get("data", {}).get("protocol_version") != PROTOCOL_VERSION
            or capabilities.get("data", {}).get("legacy_delivery_recovery") is not True
        ):
            raise StateError("legacy_delivery_service_upgrade_required")
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
                    error = frame.get("error") or {}
                    message = error.get("message", "managed_operation_failed")
                    if message == "managed_scope_busy":
                        exc = ScopeBusy(
                            error.get("operation_id"), public=error.get("scope")
                        )
                    else:
                        exc = StateError(message)
                    if error.get("retryable") or message in {
                        "managed_adapter_busy_retry_same_key",
                        "managed_scope_busy",
                        "memory_cycle_timeout",
                    }:
                        exc.retryable = True
                    if error.get("operation_id"):
                        exc.operation_id = error.get("operation_id")
                    if "scope" in error:
                        exc.scope = error.get("scope")
                    if "adapter" in error:
                        exc.adapter = error.get("adapter")
                    raise exc
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
