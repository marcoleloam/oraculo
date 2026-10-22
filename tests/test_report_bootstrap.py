"""Exact legacy package/socket/SQLite/Git/vault, simulated caller-bound host.

Set ORACULO_BOOTSTRAP_LEGACY_SITE to an isolated installed 3d857301 site-packages.
The complete Python source inventory is pinned; no candidate modules enter it.
"""

import asyncio
import copy
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

from oraculo import report_bootstrap as bootstrap
from oraculo.cli import make_envelope, request
from oraculo.config import Config
from oraculo.host_ack import make_ack
from oraculo.managed_service import command
from oraculo.protocol import canonical_json, content_hash
from oraculo.state import StateError
from oraculo.task_transport import SocketMemory

LEGACY_SHA256 = "7e55111d5d42bb982db6c23d95ef6df0823cc99d0f11f144e5a09f43607a8616"


@pytest.fixture(scope="session")
def legacy_site(tmp_path_factory):
    selected = os.environ.get("ORACULO_BOOTSTRAP_LEGACY_SITE")
    if not selected:
        pytest.skip("explicit exact legacy package required; see REPORT_BOOTSTRAP.md")
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
def report_variant(request):
    return getattr(request, "param", ("not_run", False))


@pytest.fixture
async def legacy(tmp_path, legacy_site, report_variant):
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
                        "SELECT * FROM audit_events WHERE kind='managed.report_bootstrap_released'",
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
            if len(report_variant) > 3 and report_variant[3]:
                original["commit"] = original["commit"][:7]
            if len(report_variant) > 2 and report_variant[2]:
                original["checks"][0]["detail"] = {
                    "note": "legacy observation",
                    "count": 0,
                }
                original["checks"][0]["result"] = "Retain existing result"
            original["checks"] += [
                dict(command="external check one", status=report_variant[0]),
                dict(
                    command="external check two",
                    status="not_run",
                    result="Not authorized in this fixture",
                ),
            ]
            host.caller = task["child"]["childThreadId"]
            submitted = await invoke("finish", task_id=task["id"], report=original)
            assert submitted["phase"] == "validation_submitted"
            host.complete(host.caller)
            host.caller = "origin"
            blocked = await invoke("resume", task_id=task["id"])
            assert blocked["blocked_reason"] == "malformed_validation_check"
            if report_variant[1]:
                refused = await invoke(
                    "continue",
                    task_id=task["id"],
                    key="repair-report",
                    text="Repair report",
                )
                assert (
                    refused["blocked_reason"]
                    == "terminal_task_requires_explicit_new_revision"
                )
            runtime = (await request(config, make_envelope("status.get", "*", {})))[
                "data"
            ]["runtime"]
            payload = dict(
                task_id=task["id"],
                expected_report_sha256=content_hash(original),
                report=bootstrap.replacement_for(task_read(task["id"]), original),
                reason="Move unexecuted checks verbatim to limitations",
                expected_service=runtime,
            )
            flow.task, flow.original, flow.payload = task, original, payload
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


@pytest.mark.parametrize(
    "report_variant",
    [
        ("not_run", False),
        ("blocked", False),
        ("blocked", True),
        ("not_run", True),
        ("blocked", True, True),
        ("blocked", True, True, True),
    ],
    indirect=True,
)
async def test_exact_daemon_bootstrap_e2e(legacy, report_variant):
    before = legacy.snapshot(legacy.task["id"])
    legacy.host.calls.clear()
    result = await recover(legacy)
    # A child without descendants must fit the manual bridge budget; duplicated
    # observations previously exhausted the real origin's ten-minute window.
    assert len(legacy.host.calls) <= 18
    assert result["recovery"] == "confirmed" and result["work_status"] == "succeeded"
    final = legacy.task_read(legacy.task["id"])
    assert final["memory"]["write_state"] == "saved"
    assert final["memory"]["write_receipt"]["state"] == "confirmed"
    assert final["memory"]["write_receipt"]["operation_id"] == final["memory"]["id"]
    assert final["report"] == legacy.payload["report"]
    assert final["report_recovery"]["original_report"] == legacy.original
    assert json.loads(final["report_bootstrap"]["original_payload"]) == before["task"]
    assert len(final["report"]["checks"]) == 1
    if len(report_variant) > 2 and report_variant[2]:
        expected = copy.deepcopy(legacy.original["checks"][0])
        del expected["detail"]
        assert final["report"]["checks"][0] == expected
        assert (
            "Original check with legacy detail: "
            + canonical_json(legacy.original["checks"][0])
            in final["report"]["limitations"]
        )
    assert any(report_variant[0] in item for item in final["report"]["limitations"])
    assert any("not_run" in item for item in final["report"]["limitations"])
    if len(report_variant) > 3 and report_variant[3]:
        assert final["report"]["commit"] == git(
            legacy.task["child_workspace"]["path"], "rev-parse", "HEAD"
        )
        assert (
            "Original abbreviated commit: " + canonical_json(legacy.original["commit"])
            in final["report"]["limitations"]
        )
    with sqlite3.connect(legacy.config.db_path) as db:
        assert (
            db.execute("SELECT count(*) FROM managed_reservations").fetchone()[0] == 0
        )
        cycle = json.loads(
            db.execute("SELECT payload FROM memory_cycles").fetchone()[0]
        )
        assert cycle["report"]["evidence"] == final["report"]
        assert (
            db.execute(
                "SELECT count(*) FROM audit_events WHERE kind=?",
                (bootstrap.AUDIT_KIND,),
            ).fetchone()[0]
            == 1
        )
    # Retry after the response was discarded has no second write/event/release.
    after = legacy.snapshot(legacy.task["id"])
    await recover(legacy)
    assert legacy.snapshot(legacy.task["id"]) == after
    assert len(legacy.host.created) == 1
    status = (await request(legacy.config, make_envelope("status.get", "*", {})))[
        "data"
    ]
    assert status["runtime"] == legacy.payload["expected_service"]
    assert status["maintenance"]["quiescent"] is True


@pytest.mark.parametrize(
    "guard",
    [
        "origin",
        "inactive",
        "project",
        "child",
        "descendant",
        "latest",
        "unknown",
        "dirty",
        "commit",
        "digest",
        "reason",
        "release",
        "pid",
        "config",
        "false-verdict",
        "failed-check",
        "invented-pass",
        "lost-limitations",
    ],
)
async def test_rejects_before_effects(legacy, guard):
    payload = copy.deepcopy(legacy.payload)
    child = legacy.task["child"]["childThreadId"]
    if guard == "origin":
        legacy.host.caller = child
        legacy.host.threads[child]["activeRunId"] = "continued"
    elif guard == "inactive":
        legacy.host.threads["origin"]["activeRunId"] = None
    elif guard == "project":
        legacy.host.threads["origin"]["projectId"] = "foreign"
    elif guard == "child":
        legacy.host.threads[child]["activeRunId"] = "continued"
    elif guard == "descendant":
        legacy.host.complete(child, pending=True)
    elif guard == "latest":
        legacy.host.threads[child]["latestRunId"] = "different"
    elif guard == "unknown":
        legacy.host.complete(child, status="unknown")
    elif guard == "dirty":
        Path(legacy.task["child_workspace"]["path"], "unfinished").write_text("dirty")
    elif guard == "commit":
        payload["report"]["commit"] = "f" * 40
    elif guard == "digest":
        payload["expected_report_sha256"] = "f" * 64
    elif guard == "reason":
        payload["reason"] = " "
    elif guard in {"release", "pid", "config"}:
        payload["expected_service"][
            {"release": "release_id", "pid": "pid", "config": "config_sha256"}[guard]
        ] = 1 if guard == "pid" else "wrong"
    elif guard == "false-verdict":
        payload["report"]["verdict"] = False
    elif guard == "failed-check":
        payload["report"]["checks"][0]["status"] = "failed"
    elif guard == "invented-pass":
        payload["report"]["checks"].append(
            dict(command="external check one", status="passed")
        )
    elif guard == "lost-limitations":
        payload["report"]["limitations"] = []
    before = legacy.snapshot(legacy.task["id"])
    with pytest.raises(StateError):
        await recover(legacy, payload)
    assert legacy.snapshot(legacy.task["id"]) == before


@pytest.mark.parametrize("report_variant", [("blocked", True, True)], indirect=True)
async def test_detail_metadata_cannot_be_dropped_from_recovery(legacy):
    before = legacy.snapshot(legacy.task["id"])
    payload = copy.deepcopy(legacy.payload)
    payload["report"]["limitations"] = [
        item
        for item in payload["report"]["limitations"]
        if not item.startswith("Original check with legacy detail: ")
    ]
    with pytest.raises(StateError, match="bootstrap_lossless_replacement_required"):
        await recover(legacy, payload)
    assert legacy.snapshot(legacy.task["id"]) == before


def test_detail_repair_does_not_allow_other_passed_check_keys():
    original = {
        "commit": "1" * 40,
        "checks": [
            {"command": "test", "status": "passed", "detail": "retained", "unknown": 1},
            {"command": "external", "status": "blocked"},
        ],
    }
    with pytest.raises(StateError, match="malformed_validation_check"):
        bootstrap.replacement_for({"request": {}}, original)


@pytest.mark.parametrize("mode", ["dirty", "not-head", "missing", "ambiguous"])
async def test_abbreviated_commit_requires_unique_clean_head(legacy, monkeypatch, mode):
    original = copy.deepcopy(legacy.original)
    original["commit"] = original["commit"][:7]
    path = legacy.task["child_workspace"]["path"]
    if mode == "dirty":
        Path(path, "unfinished").write_text("pending")
    elif mode == "not-head":
        git(path, "commit", "--allow-empty", "-m", "Later commit")
    elif mode == "missing":
        original["commit"] = "fffffff"
    else:

        def ambiguous(cwd, *args):
            assert args == (
                "rev-parse",
                "--verify",
                "--end-of-options",
                original["commit"] + "^{commit}",
            )
            raise StateError("workspace_git_metadata_unavailable")

        monkeypatch.setattr(bootstrap, "git", ambiguous)
    before = legacy.snapshot(legacy.task["id"])
    with pytest.raises(StateError):
        bootstrap.replacement_for(legacy.task, original)
    assert legacy.snapshot(legacy.task["id"]) == before


async def test_memory_lost_response_retry_keeps_original_until_confirmed(
    legacy, monkeypatch
):
    actual = SocketMemory.finish

    async def lost(*args, **kwargs):
        await actual(*args, **kwargs)
        raise TimeoutError("response lost after service commit")

    with monkeypatch.context() as patch:
        patch.setattr(SocketMemory, "finish", lost)
        with pytest.raises(TimeoutError):
            await recover(legacy)
    task = legacy.task_read(legacy.task["id"])
    assert task["submitted_report"] == legacy.original and task.get("report") is None
    assert task["report_bootstrap"]["original_report"] == legacy.original
    memory = await bootstrap.cycle_status(legacy.config, task)
    assert memory["write_state"] == "saved"
    # The old origin resume still refuses malformed checks and retains occupancy.
    result = await legacy.invoke("resume", task_id=task["id"])
    assert result["blocked_reason"] == "malformed_validation_check"
    assert result["admission"]["state"] == "executing"
    await recover(legacy)
    assert (await bootstrap.cycle_status(legacy.config, task))[
        "write_receipt"
    ] == memory["write_receipt"]


async def test_host_change_during_memory_retains_reservation(legacy, monkeypatch):
    actual = SocketMemory.finish

    async def changed(*args, **kwargs):
        result = await actual(*args, **kwargs)
        legacy.host.threads[legacy.task["child"]["childThreadId"]]["activeRunId"] = (
            "new"
        )
        return result

    with monkeypatch.context() as patch:
        patch.setattr(SocketMemory, "finish", changed)
        with pytest.raises(StateError):
            await recover(legacy)
    assert legacy.snapshot(legacy.task["id"])["state"][0]
    legacy.host.threads[legacy.task["child"]["childThreadId"]]["activeRunId"] = None
    await recover(legacy)


async def test_cancelled_legacy_background_only_flushes_native_outbox(legacy):
    # Leave a real pending return so the actual legacy worker waits in its host call.
    legacy.host.fail_send_once = True
    await legacy.invoke(
        "progress", task_id=legacy.task["id"], message="Pending notification"
    )
    assert (await legacy.control("background"))["busy"] is True
    with pytest.raises(StateError, match="barrier_unavailable"):
        await recover(legacy)
    cancelled = await legacy.control("cancel-background")
    assert cancelled["busy"] is False  # Reproduce actual old defect, not a fix.
    result = await recover(legacy)
    assert result["recovery"] == "confirmed"
    after = legacy.snapshot(legacy.task["id"])
    await legacy.control("release-background")
    await asyncio.sleep(0.05)
    assert legacy.snapshot(legacy.task["id"]) == after


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
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        script,
        "--config",
        str(flow.path),
        "task",
        "recover-report-bootstrap",
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
    memory = await bootstrap.cycle_status(legacy.config, task)
    assert memory["write_state"] == "saved"
    assert legacy.snapshot(task["id"])["state"][0]
    code, result, error = await cli(legacy, tmp_path)
    assert code == 0 and result["recovery"] == "confirmed", (result, error)
    assert result["memory"]["write_receipt"] == memory["write_receipt"]
    after = legacy.snapshot(task["id"])
    code, result, error = await cli(legacy, tmp_path)
    assert code == 0 and result["recovery"] == "confirmed", (result, error)
    assert legacy.snapshot(task["id"]) == after


@pytest.mark.parametrize(
    "change",
    ["report", "task", "cycle", "reservation", "caller-run", "service", "unconfirmed"],
)
async def test_concurrent_change_after_memory_fails_closed(legacy, monkeypatch, change):
    actual = SocketMemory.finish

    async def changed(*args, **kwargs):
        result = await actual(*args, **kwargs)
        if change in {"report", "task"}:
            with sqlite3.connect(
                legacy.config.state_dir / "managed-tasks.sqlite3"
            ) as db:
                task = legacy.task_read(legacy.task["id"])
                if change == "report":
                    task["submitted_report"]["summary"] = "concurrent replacement"
                else:
                    task["request"]["text"] = "different intent"
                db.execute(
                    "UPDATE managed_tasks SET payload=? WHERE id=?",
                    (canonical_json(task), task["id"]),
                )
        elif change in {"cycle", "unconfirmed"}:
            with sqlite3.connect(legacy.config.db_path) as db:
                cycle = json.loads(
                    db.execute("SELECT payload FROM memory_cycles").fetchone()[0]
                )
                if change == "cycle":
                    cycle["read_receipt"]["sha256"] = "different"
                else:
                    cycle["write_receipt"]["state"] = "sent"
                db.execute(
                    "UPDATE memory_cycles SET payload=?", (canonical_json(cycle),)
                )
        elif change == "reservation":
            with sqlite3.connect(legacy.config.db_path) as db:
                db.execute("UPDATE managed_reservations SET provider='other'")
        elif change == "caller-run":
            legacy.host.threads["origin"]["activeRunId"] = "different"
        elif change == "service":
            legacy.path.write_text(legacy.path.read_text() + "\n# changed config\n")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(SocketMemory, "finish", changed)
        with pytest.raises(StateError):
            await recover(legacy)
    assert legacy.snapshot(legacy.task["id"])["state"][0]
    assert legacy.task_read(legacy.task["id"]).get("report_recovery") is None


@pytest.mark.parametrize(
    "fault", ["timeout", "release-crash", "intent-crash", "lost-outbox"]
)
async def test_replay_boundaries(legacy, monkeypatch, fault):
    from oraculo.task_store import TaskStore

    actual = TaskStore.update
    finish = SocketMemory.finish

    def broken(self, task_id, phase, proof, **changes):
        if fault == "intent-crash" and "bootstrap_intent" in proof:
            actual(self, task_id, phase, proof, **changes)
            raise StateError("injected_rollback")
        if fault == "release-crash" and "bootstrap_confirmed" in proof:
            raise StateError("injected_after_release")
        return actual(self, task_id, phase, proof, **changes)

    async def slow(*args, **kwargs):
        await finish(*args, **kwargs)
        await asyncio.sleep(1)

    with monkeypatch.context() as patch:
        patch.setattr(TaskStore, "update", broken)
        if fault == "timeout":
            patch.setattr(bootstrap, "BARRIER_SECONDS", 0.4)
            patch.setattr(SocketMemory, "finish", slow)
        if fault == "lost-outbox":
            legacy.host.fail_send_once = True
            result = await recover(legacy)
            assert result["pending_returns"] > 0
        else:
            with pytest.raises((StateError, TimeoutError)):
                await recover(legacy)
    task = legacy.task_read(legacy.task["id"])
    if fault == "intent-crash":
        assert task.get("report_bootstrap") is None
        assert (await bootstrap.cycle_status(legacy.config, task))[
            "write_state"
        ] == "not_requested"
    elif fault == "release-crash":
        assert task["submitted_report"] == legacy.original
        assert not legacy.snapshot(task["id"])["state"][0]
    final = await recover(legacy)
    assert final["recovery"] == "confirmed" and final["pending_returns"] == 0


@pytest.mark.parametrize(
    "mode",
    ["managed-thread", "negative-original", "valid-original", "changed-service-retry"],
)
async def test_ineligible_task_never_recovered(legacy, mode, monkeypatch):
    if mode == "changed-service-retry":
        actual = SocketMemory.finish

        async def lost(*args, **kwargs):
            await actual(*args, **kwargs)
            raise TimeoutError()

        with monkeypatch.context() as patch:
            patch.setattr(SocketMemory, "finish", lost)
            with pytest.raises(TimeoutError):
                await recover(legacy)
        # Simulates a newly responding service identity after a restart; journal
        # is pinned even if the caller updates its expected_service payload.
        legacy.payload["expected_service"]["pid"] += 1

        async def changed_service(config, path, expected):
            return expected

        monkeypatch.setattr(bootstrap, "live_service", changed_service)
    else:
        task = legacy.task_read(legacy.task["id"])
        if mode == "managed-thread":
            task["transport_mode"] = "managed_thread"
        elif mode == "negative-original":
            task["submitted_report"]["checks"][0]["status"] = "failed"
        elif mode == "valid-original":
            task["submitted_report"] = legacy.payload["report"]
        with sqlite3.connect(legacy.config.state_dir / "managed-tasks.sqlite3") as db:
            db.execute(
                "UPDATE managed_tasks SET payload=? WHERE id=?",
                (canonical_json(task), task["id"]),
            )
    before = legacy.snapshot(legacy.task["id"])
    with pytest.raises(StateError):
        await recover(legacy)
    assert legacy.snapshot(legacy.task["id"]) == before


@pytest.mark.parametrize(
    "mode", ["no-failure", "failure-before-submission", "other-reason"]
)
async def test_refused_continue_requires_matching_validation_history(legacy, mode):
    task = legacy.task_read(legacy.task["id"])
    task["blocked_reason"] = "terminal_task_requires_explicit_new_revision"
    failures = [
        e
        for e in task["events"]
        if e.get("proof", {}).get("reason") == "malformed_validation_check"
    ]
    assert failures
    task["events"] = [e for e in task["events"] if e not in failures]
    if mode == "failure-before-submission":
        task["events"] = failures + task["events"]
    elif mode == "other-reason":
        task["blocked_reason"] = "unrelated_failure"
        task["events"] += failures
    with sqlite3.connect(legacy.config.state_dir / "managed-tasks.sqlite3") as db:
        db.execute(
            "UPDATE managed_tasks SET payload=? WHERE id=?",
            (canonical_json(task), task["id"]),
        )
    before = legacy.snapshot(legacy.task["id"])
    with pytest.raises(
        StateError, match="bootstrap_pristine_reserved_submission_required"
    ):
        await recover(legacy)
    assert legacy.snapshot(legacy.task["id"]) == before


@pytest.mark.parametrize("status", ["failed", "skipped", "unknown", "", None])
def test_blocked_repair_never_converts_other_verdicts(status):
    original = {
        "checks": [
            {"command": "local test", "status": "passed"},
            {"command": "external validation", "status": status},
        ]
    }
    with pytest.raises(StateError, match="bootstrap_positive_executed_checks_required"):
        bootstrap.replacement_for({}, original)


async def test_actual_daemon_restart_never_rebinds_existing_intent(legacy, monkeypatch):
    actual = SocketMemory.finish

    async def lost(*args, **kwargs):
        await actual(*args, **kwargs)
        raise TimeoutError()

    with monkeypatch.context() as patch:
        patch.setattr(SocketMemory, "finish", lost)
        with pytest.raises(TimeoutError):
            await recover(legacy)
    legacy.process.stdin.write(b'{"action":"stop"}\n')
    await legacy.process.stdin.drain()
    await asyncio.wait_for(legacy.process.wait(), 8)
    assert legacy.process.returncode == 0
    legacy.process = await legacy.spawn()
    before = legacy.snapshot(legacy.task["id"])
    with pytest.raises(StateError, match="exact_service"):
        await recover(legacy)
    current = (await request(legacy.config, make_envelope("status.get", "*", {})))[
        "data"
    ]["runtime"]
    assert current["pid"] != legacy.payload["expected_service"]["pid"]
    legacy.payload["expected_service"] = current
    with pytest.raises(StateError, match="intent_mismatch"):
        await recover(legacy)
    assert legacy.snapshot(legacy.task["id"]) == before


async def test_concurrent_recovery_cannot_enter_held_barrier(legacy):
    async with bootstrap.barrier(legacy.config, legacy.task["id"], legacy.host):
        with pytest.raises(StateError, match="barrier_unavailable"):
            await recover(legacy)
    # A close is processed asynchronously by the old server.
    await asyncio.sleep(0.02)
    await recover(legacy)


async def test_memory_change_between_status_and_sqlite_release(legacy, monkeypatch):
    real_status = bootstrap.cycle_status
    count = 0

    async def changed(*args):
        nonlocal count
        cycle = await real_status(*args)
        if cycle["write_state"] == "saved":
            count += 1
            if count == 2:
                with sqlite3.connect(legacy.config.db_path) as db:
                    stored = json.loads(
                        db.execute("SELECT payload FROM memory_cycles").fetchone()[0]
                    )
                    stored["deliveries"].append({"concurrent": True})
                    db.execute(
                        "UPDATE memory_cycles SET payload=?", (canonical_json(stored),)
                    )
        return cycle

    monkeypatch.setattr(bootstrap, "cycle_status", changed)
    with pytest.raises(StateError, match="memory_"):
        await recover(legacy)
    assert legacy.snapshot(legacy.task["id"])["state"][0]
