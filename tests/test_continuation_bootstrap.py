"""Exact 01efe package/socket/SQLite/Git/vault; simulated caller-bound T3.

Set ORACULO_CONTINUATION_LEGACY_SITE to an isolated installed 01efe9c site-packages.
The complete Python source inventory is pinned; no candidate modules enter it.
"""

import asyncio
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_managed_service import Caller
from test_managed_tasks import git, initialize, report
from test_run_continuity import attach_chain

from oraculo import continuation_bootstrap as bootstrap
from oraculo import report_bootstrap as shared
from oraculo.cli import make_envelope, request
from oraculo.config import Config
from oraculo.host_ack import make_ack
from oraculo.managed_service import command
from oraculo.protocol import canonical_json, content_hash
from oraculo.state import StateError
from oraculo.task_transport import SocketMemory

LEGACY_SHA256 = "09df7208c87ec4df10566545497350955f6727f1a97527b3b55a9b4a4bdd6d83"


@pytest.fixture(scope="session")
def legacy_site(tmp_path_factory):
    selected = os.environ.get("ORACULO_CONTINUATION_LEGACY_SITE")
    if not selected:
        pytest.skip(
            "explicit exact legacy package required; see CONTINUATION_BOOTSTRAP.md"
        )
    source = Path(selected)
    files = {
        str(p.relative_to(source)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted((source / "oraculo").rglob("*.py"))
    }
    assert content_hash(files) == LEGACY_SHA256, "not the audited exact legacy package"
    target = tmp_path_factory.mktemp("legacy-site")
    shutil.copytree(
        source / "oraculo",
        target / "oraculo",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    return target


@pytest.fixture
async def legacy(tmp_path, legacy_site):
    with tempfile.TemporaryDirectory(prefix="orb-", dir="/tmp") as short:
        path = tmp_path / "config.toml"
        path.write_text(
            "\n".join(
                f"{k} = {json.dumps(str(v))}"
                for k, v in dict(
                    projects_root=tmp_path / "projects",
                    vault_root=tmp_path / "vault",
                    state_dir=short,
                ).items()
            )
            + '\nmax_executors = 7\n[providers.codex]\ninstance_id="codex"\nbilling_verified=true\nmodels=["current"]\n'
        )
        config = Config.load(path)
        initialize(config.projects_root / "alpha")
        host = Caller(config.projects_root)
        env = dict(
            os.environ,
            PYTHONPATH=str(legacy_site),
            PYTHONDONTWRITEBYTECODE="1",
            ORACULO_RELEASE_ID=bootstrap.KNOWN_RELEASE,
        )

        async def spawn():
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-B",
                str(Path(__file__).parent / "helpers/bootstrap_daemon.py"),
                str(path),
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            async with asyncio.timeout(10):
                line = await process.stdout.readline()
                assert line and json.loads(line)["ready"], (
                    line,
                    await process.stderr.read(),
                )
                assert (
                    Path(json.loads(line)["service_module"])
                    == legacy_site / "oraculo/service.py"
                )
            return process

        process = await spawn()

        async def control(action):
            process.stdin.write((json.dumps(dict(action=action)) + "\n").encode())
            await process.stdin.drain()
            async with asyncio.timeout(5):
                return json.loads(await process.stdout.readline())

        async def invoke(action, **payload):
            return await command(config, action, payload, host)

        def task_read(task_id):
            with sqlite3.connect(config.state_dir / "managed-tasks.sqlite3") as db:
                return json.loads(
                    db.execute(
                        "SELECT payload FROM managed_tasks WHERE id=?", (task_id,)
                    ).fetchone()[0]
                )

        def snapshot(task_id):
            with sqlite3.connect(config.db_path) as db:
                state = [
                    list(db.execute(sql))
                    for sql in [
                        "SELECT * FROM managed_reservations",
                        "SELECT * FROM memory_cycles",
                        "SELECT * FROM audit_events WHERE kind='managed.continuation_bootstrap_released'",
                    ]
                ]
            return dict(
                task=task_read(task_id),
                state=state,
                vault=git(config.vault_root, "rev-parse", "HEAD"),
            )

        flow = SimpleNamespace(
            config=config,
            path=path,
            host=host,
            process=process,
            spawn=spawn,
            control=control,
            invoke=invoke,
            task_read=task_read,
            snapshot=snapshot,
            service=SimpleNamespace(config=config),
        )
        try:
            task = await invoke(
                "start",
                kind="work",
                key="native-not-run",
                text="Test fixture",
                cwd=str(config.projects_root / "alpha"),
            )
            assert task["phase"] == "executing", task
            original = report(task)
            flow.task = task
            runs, thread, native = attach_chain(flow, task, active=True)
            host.caller = task["child"]["childThreadId"]
            submitted = await invoke("finish", task_id=task["id"], report=original)
            assert submitted["phase"] == "validation_submitted"
            host.complete(host.caller)
            runs[-1].update(status="completed", completedAt="2026-10-21T18:03:30Z")
            host.caller = "origin"
            blocked = await invoke("resume", task_id=task["id"])
            assert (
                blocked["blocked_reason"]
                == "terminal_execution_or_descendants_unproven"
            ), blocked
            runtime = (await request(config, make_envelope("status.get", "*", {})))[
                "data"
            ]["runtime"]
            payload = dict(
                task_id=task["id"],
                expected_report_sha256=content_hash(original),
                reason="Complete the original submitted result after proven continuation",
                expected_service=runtime,
            )
            flow.task, flow.original, flow.payload = task, original, payload
            flow.runs, flow.thread, flow.native = runs, thread, native
            yield flow
        finally:
            process = flow.process
            if process.returncode is None:
                process.stdin.write(b'{"action":"stop"}\n')
                await process.stdin.drain()
                try:
                    async with asyncio.timeout(8):
                        await process.wait()
                except TimeoutError:
                    process.kill()
                    await process.wait()
                    pytest.fail("legacy fixture failed to stop")
            assert process.returncode == 0, (await process.stderr.read()).decode()


async def recover(flow, payload=None):
    return await bootstrap.recover(
        flow.config, flow.path, payload or flow.payload, flow.host
    )


async def test_exact_daemon_preserves_report_run_history_and_replays(legacy):
    before = legacy.snapshot(legacy.task["id"])
    result = await recover(legacy)
    assert result["recovery"] == "confirmed" and result["work_status"] == "succeeded"
    assert result["memory"]["write_state"] == "saved"
    assert result["memory"]["write_receipt"]["state"] == "confirmed"
    assert result["admission"]["state"] == "not_reserved"
    after = legacy.snapshot(legacy.task["id"])
    stored = after["task"]
    assert stored["submitted_report"] == stored["report"] == legacy.original
    assert stored["child"] == before["task"]["child"]
    assert stored["events"][: len(before["task"]["events"])] == before["task"]["events"]
    assert (
        json.loads(stored["continuation_bootstrap"]["original_payload"])
        == before["task"]
    )
    assert stored["completion_evidence"]["runs"] == legacy.runs
    assert not after["state"][0] and len(after["state"][2]) == 1
    assert len(legacy.host.created) == 1
    again = await recover(legacy)
    assert again["memory"]["write_receipt"] == result["memory"]["write_receipt"]
    assert legacy.snapshot(legacy.task["id"]) == after


def edit_task(flow, change):
    with sqlite3.connect(flow.config.state_dir / "managed-tasks.sqlite3") as db:
        task = flow.task_read(flow.task["id"])
        change(task)
        db.execute(
            "UPDATE managed_tasks SET payload=? WHERE id=?",
            (canonical_json(task), task["id"]),
        )


def edit_cycle(flow, change):
    with sqlite3.connect(flow.config.db_path) as db:
        cycle = json.loads(
            db.execute("SELECT payload FROM memory_cycles").fetchone()[0]
        )
        change(cycle)
        db.execute("UPDATE memory_cycles SET payload=?", (canonical_json(cycle),))


@pytest.mark.parametrize(
    "defect",
    [
        "active",
        "queued",
        "missing",
        "truncated",
        "duplicate",
        "ordinal",
        "overlap",
        "old_report",
        "wrong_native",
        "wrong_latest",
        "question",
        "parent",
        "descendant",
        "dirty",
        "commit",
        "origin",
        "executor",
        "digest",
        "report",
        "provenance",
        "memory_pending",
        "memory_other",
        "release",
        "schema",
        "plan",
        "no_report",
        "reservation",
        "unreserved",
        "publication",
        "transport",
        "one_run",
    ],
)
async def test_ineligible_proofs_leave_state_untouched(legacy, defect):
    flow = legacy
    if defect == "active":
        flow.thread["activeRunId"] = "new"
    elif defect == "queued":
        flow.thread["pendingRequestCount"] = 1
    elif defect == "missing":
        flow.runs.pop(1)
    elif defect == "truncated":
        flow.thread["runCount"] = 4
    elif defect == "duplicate":
        flow.runs[1]["runId"] = flow.runs[0]["runId"]
    elif defect == "ordinal":
        flow.runs[1]["ordinal"] = 1
    elif defect == "overlap":
        flow.runs[-1]["startedAt"] = flow.runs[0]["startedAt"]
    elif defect == "old_report":
        edit_task(
            flow,
            lambda t: [
                e.update(at=0)
                for e in t["events"]
                if e["phase"] == "validation_submitted"
            ],
        )
    elif defect == "wrong_native":
        flow.native["childRunId"] = "other"
    elif defect == "wrong_latest":
        flow.native["latestTerminalRunId"] = flow.runs[1]["runId"]
    elif defect == "question":
        flow.native["hasPendingQuestion"] = True
    elif defect == "parent":
        flow.thread["parentThreadId"] = "other"
    elif defect == "descendant":
        flow.host.threads["descendant"] = dict(
            flow.thread,
            threadId="descendant",
            parentThreadId=flow.task["child"]["childThreadId"],
            activeRunId="d",
            latestRunId="d",
            status="running",
        )
    elif defect == "dirty":
        Path(flow.task["child_workspace"]["path"], "unfinished").write_text("pending")
    elif defect == "commit":
        git(
            flow.task["child_workspace"]["path"],
            "commit",
            "--allow-empty",
            "-m",
            "Changed",
        )
    elif defect in {"origin", "executor"}:
        if defect == "origin":
            flow.host.threads["other"] = dict(
                flow.host.threads["origin"], threadId="other"
            )
        else:
            flow.thread["activeRunId"] = "executor-active"
        flow.host.caller = (
            "other" if defect == "origin" else flow.task["child"]["childThreadId"]
        )
    elif defect == "digest":
        flow.payload["expected_report_sha256"] = "f" * 64
    elif defect in {"report", "plan"}:

        def change(t):
            if defect == "report":
                t["submitted_report"]["checks"][0]["status"] = "not_run"
            else:
                t["submitted_report"].update(
                    deliverable_kind="plan", approval_state="pending"
                )
            digest = content_hash(t["submitted_report"])
            flow.payload["expected_report_sha256"] = digest
            for e in t["events"]:
                if e["phase"] == "validation_submitted":
                    e["proof"]["report_sha256"] = digest

        edit_task(flow, change)
    elif defect == "provenance":
        edit_task(
            flow,
            lambda t: [
                e["proof"].update(submission={"thread_id": "other", "run_id": "other"})
                for e in t["events"]
                if e["phase"] == "validation_submitted"
            ],
        )
    elif defect == "memory_pending":
        edit_cycle(flow, lambda c: c.update(write_state="pending"))
    elif defect == "memory_other":
        edit_cycle(flow, lambda c: c["identity"].update(thread_id="other"))
    elif defect == "release":
        flow.payload["expected_service"]["release_id"] = "0" * 40
    elif defect == "schema":
        with sqlite3.connect(flow.config.db_path) as db:
            db.execute("UPDATE schema_migrations SET version=5 WHERE version=4")
    elif defect == "no_report":
        edit_task(flow, lambda t: t.pop("submitted_report"))
    elif defect in {"reservation", "unreserved"}:
        with sqlite3.connect(flow.config.db_path) as db:
            db.execute(
                "UPDATE managed_reservations SET provider='other'"
                if defect == "reservation"
                else "DELETE FROM managed_reservations"
            )
    elif defect == "publication":
        edit_task(flow, lambda t: t.update(publication_attempted=True))
    elif defect == "transport":
        edit_task(flow, lambda t: t.update(transport_mode="managed_thread"))
    else:
        flow.runs[:] = flow.runs[:1]
        flow.thread.update(runCount=1, latestRunId=flow.runs[0]["runId"])
        flow.native.update(latestTerminalRunId=flow.runs[0]["runId"])
    before = flow.snapshot(flow.task["id"])
    with pytest.raises((StateError, ValueError)):
        await recover(flow)
    assert flow.snapshot(flow.task["id"]) == before


async def test_memory_lost_ack_legacy_resume_and_idempotent_retry(legacy, monkeypatch):
    actual = SocketMemory.finish

    async def lost(*args, **kwargs):
        await actual(*args, **kwargs)
        raise TimeoutError("memory response lost")

    with monkeypatch.context() as patch:
        patch.setattr(SocketMemory, "finish", lost)
        with pytest.raises(TimeoutError):
            await recover(legacy)
    task = legacy.task_read(legacy.task["id"])
    assert task.get("report") is None and task["submitted_report"] == legacy.original
    memory = await shared.cycle_status(legacy.config, task)
    assert memory["write_state"] == "saved"
    blocked = await legacy.invoke("resume", task_id=task["id"])
    assert blocked["blocked_reason"] == "terminal_execution_or_descendants_unproven"
    assert blocked["admission"]["state"] == "executing"
    result = await recover(legacy)
    assert result["memory"]["write_receipt"] == memory["write_receipt"]


@pytest.mark.parametrize(
    "change",
    [
        "active",
        "fourth",
        "descendant",
        "dirty",
        "identity",
        "caller-run",
        "task",
        "report",
        "cycle",
        "unconfirmed",
        "reservation",
        "service",
    ],
)
async def test_changes_during_memory_never_release(legacy, monkeypatch, change):
    actual = SocketMemory.finish

    async def changed(*args, **kwargs):
        result = await actual(*args, **kwargs)
        if change == "active":
            legacy.thread["activeRunId"] = "fourth"
        elif change == "fourth":
            legacy.runs.append(
                dict(
                    runId="fourth",
                    ordinal=4,
                    status="completed",
                    startedAt="2026-10-21T18:04:00Z",
                    completedAt="2026-10-21T18:04:30Z",
                )
            )
            legacy.thread.update(latestRunId="fourth", runCount=4)
            legacy.native.update(latestTerminalRunId="fourth")
        elif change == "descendant":
            legacy.host.threads["descendant"] = dict(
                legacy.thread,
                threadId="descendant",
                parentThreadId=legacy.task["child"]["childThreadId"],
                activeRunId="d",
                latestRunId="d",
                status="running",
            )
        elif change == "dirty":
            Path(legacy.task["child_workspace"]["path"], "unfinished").write_text(
                "pending"
            )
        elif change == "identity":
            legacy.thread["parentThreadId"] = "other"
        elif change == "caller-run":
            legacy.host.threads["origin"]["activeRunId"] = "new"
        elif change == "task":
            edit_task(legacy, lambda t: t["request"].update(text="new intent"))
        elif change == "report":
            edit_task(
                legacy, lambda t: t["submitted_report"].update(summary="new result")
            )
        elif change == "cycle":
            edit_cycle(legacy, lambda c: c["read_receipt"].update(sha256="different"))
        elif change == "unconfirmed":
            edit_cycle(legacy, lambda c: c["write_receipt"].update(state="sent"))
        elif change == "reservation":
            with sqlite3.connect(legacy.config.db_path) as db:
                db.execute("UPDATE managed_reservations SET provider='other'")
        else:
            legacy.path.write_text(legacy.path.read_text() + "\n# config changed\n")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(SocketMemory, "finish", changed)
        with pytest.raises(StateError):
            await recover(legacy)
    assert legacy.snapshot(legacy.task["id"])["state"][0]
    assert not legacy.snapshot(legacy.task["id"])["state"][2]


async def test_new_terminal_run_between_retries_cannot_reuse_intent(
    legacy, monkeypatch
):
    async def lost(*args, **kwargs):
        raise TimeoutError("before memory")

    with monkeypatch.context() as patch:
        patch.setattr(SocketMemory, "finish", lost)
        with pytest.raises(TimeoutError):
            await recover(legacy)
    legacy.runs.append(
        dict(
            runId="fourth",
            ordinal=4,
            status="completed",
            startedAt="2026-10-21T18:04:00Z",
            completedAt="2026-10-21T18:04:30Z",
        )
    )
    legacy.thread.update(latestRunId="fourth", runCount=4)
    legacy.native.update(latestTerminalRunId="fourth")
    before = legacy.snapshot(legacy.task["id"])
    with pytest.raises(StateError):
        await recover(legacy)
    assert legacy.snapshot(legacy.task["id"]) == before


@pytest.mark.parametrize("crash_point", ["after_release", "after_completion"])
async def test_release_and_outbox_ack_loss_replay(legacy, monkeypatch, crash_point):
    from oraculo.task_store import TaskStore

    actual = TaskStore.update

    def interrupted(store, task_id, phase, proof, **changes):
        if "continuation_confirmed" not in proof:
            return actual(store, task_id, phase, proof, **changes)
        if crash_point == "after_completion":
            actual(store, task_id, phase, proof, **changes)
            # Commit as a separate process might have done before its reply died.
            store.db.commit()
        raise RuntimeError("lost completion")

    with monkeypatch.context() as patch:
        patch.setattr(TaskStore, "update", interrupted)
        with pytest.raises(RuntimeError):
            await recover(legacy)
    after = legacy.snapshot(legacy.task["id"])
    assert not after["state"][0] and len(after["state"][2]) == 1
    legacy.host.fail_send_once = True
    result = await recover(legacy)
    assert result["recovery"] == "confirmed" and result["pending_returns"] > 0
    memory = result["memory"]["write_receipt"]
    result = await recover(legacy)
    assert (
        result["pending_returns"] == 0 and result["memory"]["write_receipt"] == memory
    )
    assert len(legacy.snapshot(legacy.task["id"])["state"][2]) == 1


async def cli(flow, tmp_path, *, crash_after_memory=False):
    payload = tmp_path / "bootstrap-request.json"
    payload.write_text(json.dumps(flow.payload))
    script = "from oraculo.cli import main; raise SystemExit(main())"
    if crash_after_memory:
        script = """import os
from oraculo.task_transport import SocketMemory
original = SocketMemory.finish
async def crash(*args, **kwargs):
    await original(*args, **kwargs)
    os._exit(73)
SocketMemory.finish = crash
from oraculo.cli import main
raise SystemExit(main())
"""
    argv = [sys.executable, "-c", script]
    if not crash_after_memory and os.environ.get("ORACULO_CONTINUATION_CLI"):
        # Wheel acceptance must execute its installed console script, without
        # source-tree PYTHONPATH or an editable installation.
        assert (
            Path(bootstrap.__file__)
            .resolve()
            .is_relative_to(Path(sys.prefix).resolve())
        )
        argv = [os.environ["ORACULO_CONTINUATION_CLI"]]
    process = await asyncio.create_subprocess_exec(
        *argv,
        "--config",
        str(flow.path),
        "task",
        "recover-continuation-bootstrap",
        "--stdio-host",
        "--file",
        str(payload),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    result = None
    try:
        async with asyncio.timeout(20):
            while line := await process.stdout.readline():
                if line == b"{\n":
                    result = json.loads(line + await process.stdout.read())
                    break
                frame = json.loads(line)
                if frame.get("marker") != "ORACULO_HOST_CALL_V1":
                    result = frame
                    continue
                response = await flow.host.call(frame["tool"], frame["arguments"])
                process.stdin.write(
                    (
                        canonical_json(make_ack(frame, json.dumps(response).encode()))
                        + "\n"
                    ).encode()
                )
                await process.stdin.drain()
            code = await process.wait()
            return code, result, (await process.stderr.read()).decode()
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def test_cli_process_crash_after_memory_then_retry(legacy, tmp_path):
    code, _, error = await cli(legacy, tmp_path, crash_after_memory=True)
    assert code == 73, error
    task = legacy.task_read(legacy.task["id"])
    assert task.get("report") is None and task["submitted_report"] == legacy.original
    memory = await shared.cycle_status(legacy.config, task)
    assert memory["write_state"] == "saved"
    assert legacy.snapshot(task["id"])["state"][0]
    code, result, error = await cli(legacy, tmp_path)
    assert code == 0 and result["recovery"] == "confirmed", (result, error)
    assert result["memory"]["write_receipt"] == memory["write_receipt"]
    after = legacy.snapshot(task["id"])
    code, result, error = await cli(legacy, tmp_path)
    assert code == 0 and result["recovery"] == "confirmed", (result, error)
    assert legacy.snapshot(task["id"]) == after


async def test_origin_controls_serial_barrier_without_bypassing_maintenance(
    legacy, monkeypatch
):
    actual = SocketMemory.finish

    async def inspect(*args, **kwargs):
        with pytest.raises(StateError, match="managed_adapter_busy_retry_same_key"):
            await legacy.invoke(
                "progress", task_id=legacy.task["id"], message="must be excluded"
            )
        with pytest.raises(BlockingIOError):
            await recover(legacy)
        status = await request(legacy.config, make_envelope("status.get", "*", {}))
        assert status["data"]["capacity"]["occupied"] == 1
        return await actual(*args, **kwargs)

    monkeypatch.setattr(SocketMemory, "finish", inspect)
    assert (await recover(legacy))["recovery"] == "confirmed"


async def test_restarted_exact_service_cannot_adopt_prior_intent(legacy, monkeypatch):
    async def lost(*args, **kwargs):
        raise TimeoutError("intent persisted")

    with monkeypatch.context() as patch:
        patch.setattr(SocketMemory, "finish", lost)
        with pytest.raises(TimeoutError):
            await recover(legacy)
    legacy.process.stdin.write(b'{"action":"stop"}\n')
    await legacy.process.stdin.drain()
    assert await legacy.process.wait() == 0
    legacy.process = await legacy.spawn()
    legacy.payload["expected_service"] = (
        await request(legacy.config, make_envelope("status.get", "*", {}))
    )["data"]["runtime"]
    before = legacy.snapshot(legacy.task["id"])
    with pytest.raises(StateError, match="intent_mismatch"):
        await recover(legacy)
    assert legacy.snapshot(legacy.task["id"]) == before


@pytest.mark.parametrize("negative", ["FAIL", "failed-check", "interrupted"])
async def test_negative_evidence_never_becomes_success(legacy, negative):
    def change(t):
        t["submitted_report"]["verdict"] = "FAIL"
        if negative == "failed-check":
            t["submitted_report"].pop("verdict")
            t["submitted_report"]["checks"][0]["status"] = "failed"
        digest = content_hash(t["submitted_report"])
        legacy.payload["expected_report_sha256"] = digest
        for e in t["events"]:
            if e["phase"] == "validation_submitted":
                e["proof"]["report_sha256"] = digest

    edit_task(legacy, change)
    if negative == "interrupted":
        legacy.runs[0]["status"] = "interrupted"
        legacy.native["status"] = "interrupted"
    original = legacy.task_read(legacy.task["id"])["submitted_report"]
    result = await recover(legacy)
    assert result["recovery"] == "confirmed"
    assert result["work_status"] == (
        "stopped" if negative == "interrupted" else "failed"
    )
    assert result["blocked_reason"] == "work_not_successful"
    assert legacy.task_read(legacy.task["id"])["report"] == original
    assert not legacy.snapshot(legacy.task["id"])["state"][0]
    assert (await recover(legacy))["memory"]["write_receipt"] == result["memory"][
        "write_receipt"
    ]


@pytest.mark.parametrize(
    "change", ["missing-audit", "completion-evidence", "history", "intent-reason"]
)
async def test_completed_replay_revalidates_durable_proof(legacy, change):
    await recover(legacy)
    if change == "missing-audit":
        with sqlite3.connect(legacy.config.db_path) as db:
            db.execute("DELETE FROM audit_events WHERE kind=?", (bootstrap.AUDIT_KIND,))
    elif change == "completion-evidence":
        edit_task(
            legacy, lambda t: t["completion_evidence"].update(report_sha256="other")
        )
    elif change == "history":
        edit_task(legacy, lambda t: t["events"][0].update(at=0))
    else:
        legacy.payload["reason"] = "other"
    before = legacy.snapshot(legacy.task["id"])
    with pytest.raises(StateError):
        await recover(legacy)
    assert legacy.snapshot(legacy.task["id"]) == before


async def test_other_reservation_untouched_and_no_maintenance_bypass(legacy):
    other = await legacy.invoke(
        "start",
        kind="work",
        key="other",
        text="Other work",
        cwd=str(legacy.config.projects_root / "alpha"),
    )
    assert other["phase"] == "executing"
    with sqlite3.connect(legacy.config.db_path) as db:
        before = db.execute(
            "SELECT * FROM managed_reservations WHERE id=?", (other["id"],)
        ).fetchone()
    await recover(legacy)
    with sqlite3.connect(legacy.config.db_path) as db:
        assert db.execute("SELECT * FROM managed_reservations").fetchall() == [before]
    status = await request(legacy.config, make_envelope("status.get", "*", {}))
    assert status["data"]["capacity"]["occupied"] == 1
    assert status["data"]["maintenance"]["admission_closed"] is False


async def test_terminal_descendant_change_after_memory_is_not_hidden_by_count(
    legacy, monkeypatch
):
    descendant = dict(
        legacy.thread,
        threadId="descendant",
        parentThreadId=legacy.task["child"]["childThreadId"],
        activeRunId=None,
        latestRunId="descendant-run",
        status="completed",
        runCount=1,
    )
    legacy.host.threads["descendant"] = descendant
    actual = SocketMemory.finish

    async def changed(*args, **kwargs):
        result = await actual(*args, **kwargs)
        descendant["latestRunId"] = "different-terminal-run"
        return result

    monkeypatch.setattr(SocketMemory, "finish", changed)
    with pytest.raises(StateError, match="host_changed"):
        await recover(legacy)
    assert legacy.snapshot(legacy.task["id"])["state"][0]


async def test_actual_cli_negative_exit_is_not_recovery_failure(legacy, tmp_path):
    def change(t):
        t["submitted_report"]["verdict"] = "FAIL"
        digest = content_hash(t["submitted_report"])
        legacy.payload["expected_report_sha256"] = digest
        for e in t["events"]:
            if e["phase"] == "validation_submitted":
                e["proof"]["report_sha256"] = digest

    edit_task(legacy, change)
    code, result, error = await cli(legacy, tmp_path)
    assert code == 1 and result["recovery"] == "confirmed", error
    assert result["work_status"] == "failed"


async def test_schema_change_after_memory_fails_final_cas(legacy, monkeypatch):
    actual = shared.cycle_status
    calls = 0

    async def changed(*args, **kwargs):
        nonlocal calls
        result = await actual(*args, **kwargs)
        calls += 1
        if calls == 4:  # After the last socket proof, immediately before CAS.
            with sqlite3.connect(legacy.config.db_path) as db:
                db.execute("UPDATE schema_migrations SET version=5 WHERE version=4")
        return result

    monkeypatch.setattr(shared, "cycle_status", changed)
    with pytest.raises(StateError, match="legacy_schema_required"):
        await recover(legacy)
    assert legacy.snapshot(legacy.task["id"])["state"][0]


async def test_exact_background_owner_is_not_force_cleared(legacy):
    legacy.host.fail_send_once = True
    await legacy.invoke("progress", task_id=legacy.task["id"], message="Pending return")
    assert (await legacy.control("background"))["busy"] is True
    with pytest.raises(StateError, match="barrier_unavailable"):
        await recover(legacy)
    # This exact release has the waiter cancellation fix. No orphan shortcut.
    assert (await legacy.control("cancel-background"))["busy"] is True
    with pytest.raises(StateError, match="barrier_unavailable"):
        await recover(legacy)
    await legacy.control("release-background")
    for _ in range(100):
        status = await request(legacy.config, make_envelope("status.get", "*", {}))
        if not status["data"]["managed_adapter"]["busy"]:
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("background did not settle")
    assert (await recover(legacy))["recovery"] == "confirmed"


async def test_official_maintenance_requires_recovery_then_zero_occupancy(legacy):
    denied = await request(
        legacy.config,
        make_envelope("maintenance.begin", "*", {"operation_id": "fixture-upgrade"}),
    )
    assert denied["ok"] is False
    await recover(legacy)
    allowed = await request(
        legacy.config,
        make_envelope("maintenance.begin", "*", {"operation_id": "fixture-upgrade"}),
    )
    assert allowed["ok"] is True, allowed
    assert allowed["data"]["admission_closed"] is True
    assert allowed["data"]["quiescent"] is True
    before = legacy.snapshot(legacy.task["id"])
    with pytest.raises(StateError, match="bootstrap_maintenance_active"):
        await recover(legacy)
    assert legacy.snapshot(legacy.task["id"]) == before
