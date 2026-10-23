"""Real daemon/socket, memory, Git, admission and ledger; simulated T3 host only."""

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_managed_service import Caller
from test_managed_tasks import git, initialize, report

from oraculo.managed_service import command
from oraculo.managed_tasks import ManagedTasks, ReportValidationError
from oraculo.memory.cycles import report_projection
from oraculo.protocol import content_hash
from oraculo.state import StateError


@pytest.fixture
async def flow(running_service):
    service = running_service
    initialize(service.config.projects_root / "alpha")
    host = Caller(service.config.projects_root)

    async def invoke(action, **payload):
        return await command(service.config, action, payload, host)

    async def begin(independent=False):
        task = await invoke(
            "start",
            kind="work",
            key="report-test",
            text="Bounded work",
            independent=independent,
            cwd=str(service.config.projects_root / "alpha"),
        )
        assert task["phase"] == "executing", task
        return task

    def snapshot(task):
        store = service.managed_service.store
        return {
            "task": store.get(task["id"]),
            "returns": [
                tuple(r) for r in store.db.execute("SELECT * FROM task_returns")
            ],
            "cycle": service.memory_cycles.get(task["memory"]["id"]),
            "reservation": [
                tuple(r)
                for r in service.state.connection.execute(
                    "SELECT * FROM managed_reservations"
                )
            ],
            "vault": git(service.config.vault_root, "rev-parse", "HEAD"),
        }

    return SimpleNamespace(
        service=service, host=host, invoke=invoke, begin=begin, snapshot=snapshot
    )


@pytest.mark.parametrize("history_field", ["decisions", "limitations"])
async def test_final_passed_checks_preserve_corrected_failure_history(
    flow, history_field
):
    task = await flow.begin()
    evidence = report(task)
    evidence["checks"][0].update(
        result="Final execution: 1 passed; exit 0", proof="/private/check-final.log"
    )
    history = (
        "pytest tests/test_example.py: failed, 1 failed, exit 1 "
        "(/private/check-initial.log). Fixed empty-field serialization. "
        "Repeated pytest tests/test_example.py: passed, 1 passed, exit 0 "
        "(/private/check-final.log)."
    )
    evidence.setdefault(history_field, []).append(history)
    canonical_task = flow.service.managed_service.store.get(task["id"])
    assert ManagedTasks._validate_report(canonical_task, evidence) == (
        "implementation",
        False,
    )
    original = copy.deepcopy(evidence)
    flow.host.caller = task["child"]["childThreadId"]
    submitted = await flow.invoke("finish", task_id=task["id"], report=evidence)
    assert submitted["phase"] == "validation_submitted"
    flow.host.complete(flow.host.caller)
    flow.host.caller = "origin"
    result = await flow.invoke("resume", task_id=task["id"])
    assert result["work_status"] == "succeeded", result
    assert result["memory"]["write_state"] == "saved"
    assert result["memory"]["write_receipt"]["state"] == "confirmed"
    saved = flow.snapshot(task)
    assert saved["task"]["submitted_report"] == original
    assert saved["task"]["report"] == original
    assert saved["cycle"]["report"]["evidence"] == original


@pytest.mark.parametrize(
    "later_command",
    [
        "pytest tests/test_example.py",
        "pytest tests/test_example.py -k empty_field",
        "ruff check src",
    ],
    ids=["same-command", "partial-rerun", "different-command"],
)
async def test_failed_check_remains_negative_with_later_passed(flow, later_command):
    task = await flow.begin()
    evidence = report(task)
    evidence["checks"][0].update(
        status="failed", result="1 failed; exit 1", proof="/private/check-failed.log"
    )
    evidence["checks"].append(
        {"command": later_command, "status": "passed", "result": "exit 0"}
    )
    evidence["verdict"] = "PASS"  # Cannot override any failed check.
    canonical_task = flow.service.managed_service.store.get(task["id"])
    assert ManagedTasks._validate_report(canonical_task, evidence) == (
        "implementation",
        True,
    )
    original = copy.deepcopy(evidence)
    flow.host.caller = task["child"]["childThreadId"]
    submitted = await flow.invoke("finish", task_id=task["id"], report=evidence)
    assert submitted["phase"] == "validation_submitted"
    flow.host.complete(flow.host.caller)
    flow.host.caller = "origin"
    result = await flow.invoke("resume", task_id=task["id"])
    assert result["work_status"] == "failed", result
    assert result["blocked_reason"] == "work_not_successful"
    assert result["publication"] is None
    assert result["memory"]["write_state"] == "saved"
    assert result["memory"]["write_receipt"]["state"] == "confirmed"
    saved = flow.snapshot(task)
    assert saved["task"]["submitted_report"] == original
    assert saved["task"]["report"] == original
    assert saved["cycle"]["report"]["evidence"] == original

    rewritten = copy.deepcopy(evidence)
    rewritten["checks"] = rewritten["checks"][1:]
    rejected = await flow.invoke("finish", task_id=task["id"], report=rewritten)
    assert rejected["blocked_reason"] == "terminal_report_reused"
    unchanged = flow.snapshot(task)
    assert unchanged["task"]["submitted_report"] == original
    assert unchanged["task"]["report"] == original
    assert unchanged["cycle"] == saved["cycle"]


@pytest.mark.parametrize(
    "negative", [{"verdict": "FAIL"}, {"summary": "FAIL — review"}]
)
def test_final_passed_checks_do_not_override_explicit_fail(negative):
    evidence = {
        "summary": "Validated example",
        "commit": "a" * 40,
        "checks": [{"command": "pytest", "status": "passed"}],
        **negative,
    }
    assert ManagedTasks._validate_report({"request": {}}, evidence) == (
        "implementation",
        True,
    )


@pytest.mark.parametrize("scope", ["report", "check"])
def test_superseded_by_is_not_a_supported_report_or_check_field(scope):
    evidence = {
        "commit": "a" * 40,
        "checks": [{"command": "pytest", "status": "passed"}],
    }
    target = evidence if scope == "report" else evidence["checks"][0]
    target["superseded_by"] = "later-check"
    reason = (
        "invalid_validation_report"
        if scope == "report"
        else "malformed_validation_check"
    )
    with pytest.raises(ReportValidationError, match=reason):
        ManagedTasks._validate_report({"request": {}}, evidence)


@pytest.mark.parametrize("independent", [False, True])
@pytest.mark.parametrize("negative", [False, True])
async def test_executor_reject_correct_origin_finish(flow, independent, negative):
    task = await flow.begin(independent)
    flow.host.caller = task["child"]["childThreadId"]
    bad = report(task)
    bad["checks"].append({"command": "unexecuted check", "status": "not_run"})
    before = flow.snapshot(task)
    calls = len(flow.host.calls)
    rejected = await flow.invoke("finish", task_id=task["id"], report=bad)
    assert rejected["blocked_reason"] == "malformed_validation_check"
    assert rejected["rejection"]["persisted"] is False
    assert flow.snapshot(task) == before
    assert all(name != "t3_thread_send" for name, _ in flow.host.calls[calls:])
    good = report(task)
    good["limitations"].append("Unexecuted check was not run.")
    if negative:
        good["checks"][0]["status"] = "failed"
        good["verdict"] = "FAIL"
    submitted = await flow.invoke("finish", task_id=task["id"], report=good)
    assert submitted["phase"] == "validation_submitted"
    assert submitted["admission"]["state"] == "executing"
    flow.host.complete(flow.host.caller)
    flow.host.caller = "origin"
    result = await flow.invoke("resume", task_id=task["id"])
    assert result["work_status"] == ("failed" if negative else "succeeded"), result
    assert result["memory"]["write_state"] == "saved"
    assert result["memory"]["write_receipt"]["state"] == "confirmed"
    assert flow.service.state.capacity()["occupied"] == 0
    assert flow.snapshot(task)["cycle"]["report"]["evidence"] == good
    assert len(flow.host.created) == 1


@pytest.mark.parametrize("actor", ["origin", "executor"])
@pytest.mark.parametrize(
    "bad_field",
    [
        "shape",
        "checks",
        "check-key",
        "check-command",
        "verdict",
        "summary",
        "unknown",
        "kind",
        "commit-missing",
        "commit-type",
        "commit-wrong",
        "size",
        "dirty",
        "workspace",
        "parent",
        "base",
    ],
)
async def test_all_rejections_leave_no_persistent_effect(flow, actor, bad_field):
    task = await flow.begin()
    good = report(task)
    bad = copy.deepcopy(good)
    child = task["child"]["childThreadId"]
    if actor == "executor":
        flow.host.caller = child
    if bad_field == "shape":
        bad = []
    elif bad_field == "checks":
        bad["checks"] = []
    elif bad_field == "check-key":
        bad["checks"][0]["extra"] = True
    elif bad_field == "check-command":
        bad["checks"][0]["command"] = " "
    elif bad_field == "verdict":
        bad["verdict"] = []
    elif bad_field == "summary":
        bad["summary"] = {}
    elif bad_field == "unknown":
        bad["something"] = "ignored?"
    elif bad_field == "kind":
        bad["deliverable_kind"] = "plan"
    elif bad_field == "commit-missing":
        bad.pop("commit")
    elif bad_field == "commit-type":
        bad["commit"] = []
    elif bad_field == "commit-wrong":
        bad["commit"] = "f" * 40
    elif bad_field == "size":
        bad["summary"] = "é" * (128 * 1024)
    elif bad_field == "dirty":
        Path(task["child_workspace"]["path"], "unfinished").write_text("pending")
    elif bad_field == "workspace":
        flow.host.threads[child]["worktreePath"] = str(
            flow.service.config.projects_root / "alpha"
        )
    elif bad_field == "parent":
        flow.host.threads[child]["parentThreadId"] = "someone-else"
    elif bad_field == "base":
        git(task["child_workspace"]["path"], "checkout", "--orphan", "unrelated")
        git(
            task["child_workspace"]["path"],
            "-c",
            "user.name=Example",
            "-c",
            "user.email=x@example.org",
            "commit",
            "--allow-empty",
            "-m",
            "Unrelated",
        )
        bad["commit"] = git(task["child_workspace"]["path"], "rev-parse", "HEAD")
    before = flow.snapshot(task)
    result = await flow.invoke("finish", task_id=task["id"], report=bad)
    assert result["phase"] == "blocked", result
    assert result["rejection"]["persisted"] is False
    assert flow.snapshot(task) == before


@pytest.mark.parametrize("char", ["a", "é", "😀"])
@pytest.mark.parametrize("delta", [-1, 0, 1])
async def test_exact_canonical_memory_byte_boundary(flow, char, delta):
    task = await flow.begin()
    flow.host.caller = task["child"]["childThreadId"]
    evidence = report(task)
    evidence["summary"] = ""
    target = 256 * 1024 + delta
    # Independent calculation, not the implementation's projection helper.
    size = len(
        json.dumps(
            evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    )
    n, remainder = divmod(target - size, len(char.encode()))
    evidence["summary"] = char * n + "a" * remainder
    assert len(report_projection(evidence)[1]) == target
    before = flow.snapshot(task)
    result = await flow.invoke("finish", task_id=task["id"], report=evidence)
    if delta > 0:
        assert result["blocked_reason"] == "memory_report_too_large"
        assert flow.snapshot(task) == before
    else:
        assert result["phase"] == "validation_submitted", result
        flow.host.complete(flow.host.caller)
        flow.host.caller = "origin"
        final = await flow.invoke("resume", task_id=task["id"])
        assert final["memory"]["write_state"] == "saved", final


async def legacy(flow, independent=False, negative=False):
    task = await flow.begin(independent)
    original = report(task)
    if negative:
        original["checks"][0]["status"] = "failed"
    original["checks"].append({"command": "optional live trial", "status": "not_run"})
    store = flow.service.managed_service.store
    # Deliberate old-version persisted fixture, never an operational database.
    store.update(
        task["id"],
        "validation_submitted",
        {"report_sha256": content_hash(original)},
        submitted_report=original,
    )
    store.update(
        task["id"],
        "blocked",
        {"reason": "malformed_validation_check"},
        blocked_reason="malformed_validation_check",
        submitted_report=original,
    )
    replacement = report(task)
    if negative:
        replacement["checks"][0]["status"] = "failed"
    replacement["limitations"].append("Optional live trial was not run.")
    flow.host.complete(task["child"]["childThreadId"])
    payload = dict(
        task_id=task["id"],
        report=replacement,
        expected_report_sha256=content_hash(original),
        reason="Move unexecuted trial into limitations.",
    )
    return task, original, payload


@pytest.mark.parametrize("independent", [False, True])
@pytest.mark.parametrize("negative", [False, True])
async def test_recover_legacy_and_replay_then_finish(flow, independent, negative):
    task, original, payload = await legacy(flow, independent, negative)
    before = flow.snapshot(task)
    recovered = await flow.invoke("recover-report", **payload)
    assert recovered["phase"] == "validation_recovered", recovered
    after = flow.snapshot(task)
    audit = after["task"]["report_recovery"]
    assert audit["original_report"] == original
    assert audit["original_sha256"] == content_hash(original)
    assert audit["replacement_sha256"] == content_hash(payload["report"])
    assert after["cycle"] == before["cycle"]
    assert after["reservation"] == before["reservation"]
    assert after["vault"] == before["vault"]
    assert "original_report" not in recovered["report_recovery"]
    await flow.invoke("recover-report", **payload)
    assert flow.snapshot(task) == after
    final = await flow.invoke("resume", task_id=task["id"])
    assert final["work_status"] == ("failed" if negative else "succeeded"), final
    assert final["memory"]["write_state"] == "saved"
    assert flow.service.state.capacity()["occupied"] == 0
    finished = flow.snapshot(task)
    await flow.invoke("recover-report", **payload)
    assert flow.snapshot(task) == finished
    assert flow.snapshot(task)["task"]["report_recovery"] == audit


@pytest.mark.parametrize(
    "guard",
    [
        "executor",
        "foreign-origin",
        "project",
        "inactive-origin",
        "active-child",
        "descendant",
        "digest",
        "valid",
        "certified",
        "memory",
        "replacement",
        "negative",
        "reason",
        "host-failure",
    ],
)
async def test_recovery_guards_preserve_everything(flow, guard, monkeypatch):
    task, original, payload = await legacy(flow, negative=guard == "negative")
    store = flow.service.managed_service.store
    child = task["child"]["childThreadId"]
    if guard == "executor":
        flow.host.caller = child
        flow.host.threads[child]["activeRunId"] = "continued"
    elif guard == "foreign-origin":
        flow.host.caller = "foreign"
        flow.host.threads["foreign"] = dict(
            flow.host.threads["origin"], threadId="foreign"
        )
    elif guard == "project":
        flow.host.threads["origin"]["projectId"] = "different"
    elif guard == "inactive-origin":
        flow.host.threads["origin"]["activeRunId"] = None
    elif guard == "active-child":
        flow.host.threads[child].update(activeRunId="new-run", status="running")
    elif guard == "descendant":
        flow.host.complete(child, pending=True)
    elif guard == "digest":
        payload["expected_report_sha256"] = "f" * 64
    elif guard == "valid":
        original = report(task)
        store.update(
            task["id"],
            "validation_submitted",
            {"report_sha256": content_hash(original)},
            submitted_report=original,
        )
        payload["expected_report_sha256"] = content_hash(original)
    elif guard == "certified":
        store.update(
            task["id"], "memory_pending", {"fixture": "certified"}, report=original
        )
    elif guard == "memory":
        # True memory certification, deliberately stale task-side memory snapshot.
        flow.service.memory_cycles.finish(
            task["memory"]["id"], "succeeded", report(task)
        )
    elif guard == "replacement":
        payload["report"]["checks"][0]["status"] = "not_run"
    elif guard == "negative":
        payload["report"]["checks"][0]["status"] = "passed"
    elif guard == "reason":
        payload["reason"] = " "
    elif guard == "host-failure":
        real_call = flow.host.call

        async def unavailable(name, args):
            if name == "t3_thread_list":
                raise OSError("host unavailable")
            return await real_call(name, args)

        monkeypatch.setattr(flow.host, "call", unavailable)
    before = flow.snapshot(task)
    try:
        result = await flow.invoke("recover-report", **payload)
        assert result["phase"] == "blocked", result
        if guard == "negative":
            assert (
                result["blocked_reason"] == "report_recovery_negative_evidence_required"
            )
        if guard == "valid":
            assert result["blocked_reason"] == "valid_submitted_report_immutable"
    except StateError:
        pass  # Identity guards run before a task transition is considered.
    assert flow.snapshot(task) == before


async def test_recovery_cas_and_failed_transaction(flow, monkeypatch):
    task, original, payload = await legacy(flow)
    store = flow.service.managed_service.store
    real_update = store.update

    def broken(*args, **kwargs):
        real_update(*args, **kwargs)
        raise StateError("injected_failure_before_commit")

    before = flow.snapshot(task)
    with monkeypatch.context() as patch:
        patch.setattr(store, "update", broken)
        result = await flow.invoke("recover-report", **payload)
        assert result["blocked_reason"] == "injected_failure_before_commit"
    assert flow.snapshot(task) == before
    recovered = await flow.invoke("recover-report", **payload)
    assert recovered["phase"] == "validation_recovered"
    after = flow.snapshot(task)
    changed = copy.deepcopy(payload)
    changed["reason"] = "Different retry"
    result = await flow.invoke("recover-report", **changed)
    assert result["blocked_reason"] == "report_recovery_reused"
    assert flow.snapshot(task) == after


async def test_recovery_lost_notification_ack_reuses_audit(flow):
    task, _, payload = await legacy(flow)
    flow.host.fail_send_once = True
    first = await flow.invoke("recover-report", **payload)
    assert first["phase"] == "validation_recovered"
    assert first["pending_returns"] > 0
    audit = flow.snapshot(task)["task"]["report_recovery"]
    final = await flow.invoke("recover-report", **payload)
    assert final["pending_returns"] == 0
    assert flow.snapshot(task)["task"]["report_recovery"] == audit
    assert (
        len(
            [
                e
                for e in flow.snapshot(task)["task"]["events"]
                if e["phase"] == "validation_recovered"
            ]
        )
        == 1
    )


async def run_cli(flow, tmp_path, action, payload, *, wire=None, file_ack=False):
    import asyncio
    import sys

    from oraculo.host_ack import make_ack
    from oraculo.protocol import canonical_json

    config = tmp_path / "cli.toml"
    config.write_text(
        "\n".join(
            f"{k} = {json.dumps(str(getattr(flow.service.config, k)))}"
            for k in ("state_dir", "projects_root", "vault_root")
        )
    )
    path = tmp_path / "payload.json"
    path.write_text(json.dumps(payload))
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "oraculo",
        "--config",
        str(config),
        "task",
        action,
        "--stdio-host",
        "--file",
        str(path),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    result = None
    try:
        async with asyncio.timeout(30):
            while line := await process.stdout.readline():
                if line == b"{\n":
                    result = json.loads(line + await process.stdout.read())
                    break
                frame = json.loads(line)
                if frame.get("marker") == "ORACULO_HOST_CALL_V1":
                    response = await flow.host.call(frame["tool"], frame["arguments"])
                    if file_ack:
                        frame_path, result_path = (
                            tmp_path / "frame.json",
                            tmp_path / "host.json",
                        )
                        frame_path.write_text(canonical_json(frame))
                        result_path.write_text(canonical_json(response))
                        helper = await asyncio.create_subprocess_exec(
                            sys.executable,
                            "-m",
                            "oraculo",
                            "bridge",
                            "ack",
                            "--frame",
                            str(frame_path),
                            "--from-file",
                            str(result_path),
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE,
                        )
                        ack_bytes, error = await helper.communicate()
                        assert helper.returncode == 0, error
                    else:
                        ack_bytes = (
                            canonical_json(
                                make_ack(frame, json.dumps(response).encode())
                            )
                            + "\n"
                        ).encode()
                    if wire is not None:
                        before_frame = {
                            k: v
                            for k, v in frame.items()
                            if k not in {"issued_at", "ack_protocol"}
                        }
                        before_ack = {
                            "request_id": frame["request_id"],
                            "result": response,
                        }
                        wire.append(
                            dict(
                                tool=frame["tool"],
                                before=len(
                                    (
                                        canonical_json(before_frame)
                                        + "\n"
                                        + canonical_json(before_ack)
                                        + "\n"
                                    ).encode()
                                ),
                                after=len(line) + len(ack_bytes),
                            )
                        )
                    process.stdin.write(ack_bytes)
                    await process.stdin.drain()
                else:
                    result = frame
            code = await process.wait()
            return code, result, (await process.stderr.read()).decode()
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def test_cli_rejection_correction_and_origin_completion(flow, tmp_path):
    task = await flow.begin()
    flow.host.caller = task["child"]["childThreadId"]
    bad = report(task)
    bad["checks"][0]["status"] = "not_run"
    before = flow.snapshot(task)
    code, result, _ = await run_cli(
        flow, tmp_path, "finish", dict(task_id=task["id"], report=bad)
    )
    assert code == 1 and result["rejection"]["persisted"] is False
    assert "limitations" in result["rejection"]["hint"]
    assert flow.snapshot(task) == before
    code, result, _ = await run_cli(
        flow, tmp_path, "finish", dict(task_id=task["id"], report=report(task))
    )
    assert code == 0 and result["phase"] == "validation_submitted"
    flow.host.complete(flow.host.caller)
    flow.host.caller = "origin"
    code, result, _ = await run_cli(flow, tmp_path, "resume", dict(task_id=task["id"]))
    assert code == 0 and result["memory"]["write_state"] == "saved"


async def test_cli_recovery_and_old_service_capability_guard(
    flow, tmp_path, monkeypatch
):
    task, _, payload = await legacy(flow)
    real_handle = flow.service.handle

    async def old_service(envelope):
        result = await real_handle(envelope)
        if envelope.kind == "managed.capabilities":
            result["data"].pop("invalid_report_recovery", None)
        return result

    before = flow.snapshot(task)
    with monkeypatch.context() as patch:
        patch.setattr(flow.service, "handle", old_service)
        code, result, stderr = await run_cli(flow, tmp_path, "recover-report", payload)
        assert code != 0
        assert "report_recovery_service_upgrade_required" in str(result) + stderr
    assert flow.snapshot(task) == before
    code, result, stderr = await run_cli(flow, tmp_path, "recover-report", payload)
    assert code == 0 and result["phase"] == "validation_recovered", (result, stderr)


@pytest.mark.parametrize(
    "fault",
    [
        "memory-unavailable",
        "malformed-host",
        "duplicate-run",
        "other-active-run",
        "conflicting-run",
    ],
)
async def test_recovery_read_failures_and_ambiguous_evidence_are_pure(
    flow, fault, monkeypatch
):
    from oraculo.task_transport import SocketMemory

    task, _, payload = await legacy(flow)
    if fault == "memory-unavailable":

        async def unavailable(*args):
            raise StateError("memory_adapter_unavailable")

        monkeypatch.setattr(SocketMemory, "status", unavailable)
    else:
        real_call = flow.host.call

        async def faulty(name, args):
            result = await real_call(name, args)
            if (
                name == "t3_thread_read"
                and args["threadId"] == task["child"]["childThreadId"]
                and args.get("runLimit") == 50
            ):
                if fault == "malformed-host":
                    result.pop("recentRuns")
                elif fault == "duplicate-run":
                    result["recentRuns"] *= 2
                elif fault == "other-active-run":
                    result["recentRuns"].append({"runId": "other", "status": "running"})
                elif fault == "conflicting-run":
                    result["recentRuns"][0]["status"] = "failed"
            return result

        monkeypatch.setattr(flow.host, "call", faulty)
    before = flow.snapshot(task)
    result = await flow.invoke("recover-report", **payload)
    assert result["phase"] == "blocked", result
    assert flow.snapshot(task) == before


async def test_recovery_cas_preserves_concurrent_task_change(flow, monkeypatch):
    task, _, payload = await legacy(flow)
    real_call = flow.host.call
    expected = None

    async def concurrent(name, args):
        nonlocal expected
        result = await real_call(name, args)
        if name == "task_status":
            flow.service.managed_service.store.update(
                task["id"],
                "blocked",
                {"concurrent": "change"},
                blocked_reason="concurrent",
            )
            expected = flow.snapshot(task)
        return result

    monkeypatch.setattr(flow.host, "call", concurrent)
    result = await flow.invoke("recover-report", **payload)
    assert result["blocked_reason"] == "report_recovery_task_changed_retry"
    assert flow.snapshot(task) == expected


async def test_recovery_lost_final_response_can_retry_without_replacement(
    flow, monkeypatch
):
    from oraculo.managed_tasks import ManagedTasks

    task, _, payload = await legacy(flow)

    async def lost(*args):
        raise StateError("simulated_lost_response_after_commit")

    with monkeypatch.context() as patch:
        patch.setattr(ManagedTasks, "flush_returns", lost)
        with pytest.raises(StateError, match="simulated_lost_response_after_commit"):
            await flow.invoke("recover-report", **payload)
    audit = flow.snapshot(task)["task"]["report_recovery"]
    result = await flow.invoke("recover-report", **payload)
    assert result["phase"] == "validation_recovered"
    assert flow.snapshot(task)["task"]["report_recovery"] == audit


async def test_review_metadata_is_preserved_but_not_projected_into_memory(flow):
    task = await flow.begin()
    flow.host.caller = task["child"]["childThreadId"]
    evidence = report(task)
    evidence["findings"] = "é" * 140000
    submitted = await flow.invoke("finish", task_id=task["id"], report=evidence)
    assert submitted["phase"] == "validation_submitted"
    flow.host.complete(flow.host.caller)
    flow.host.caller = "origin"
    final = await flow.invoke("resume", task_id=task["id"])
    assert final["memory"]["write_state"] == "saved"
    saved = flow.snapshot(task)
    assert saved["task"]["report"] == evidence
    assert "findings" not in saved["cycle"]["report"]["evidence"]


async def test_recovery_memory_changed_during_terminal_reads_is_rejected(
    flow, monkeypatch
):
    task, _, payload = await legacy(flow)
    real_call = flow.host.call
    expected = None

    async def concurrent(name, args):
        nonlocal expected
        result = await real_call(name, args)
        if name == "task_status":
            flow.service.memory_cycles.finish(
                task["memory"]["id"], "succeeded", report(task)
            )
            expected = flow.snapshot(task)
        return result

    monkeypatch.setattr(flow.host, "call", concurrent)
    result = await flow.invoke("recover-report", **payload)
    assert result["blocked_reason"] == "report_recovery_memory_changed_retry"
    assert flow.snapshot(task) == expected


async def test_recovery_checks_digest_against_historical_submission(flow):
    task, original, payload = await legacy(flow)
    original["summary"] = "Changed since original submission event"
    flow.service.managed_service.store.update(
        task["id"],
        "blocked",
        {"fixture": "divergent digest"},
        submitted_report=original,
    )
    payload["expected_report_sha256"] = content_hash(original)
    before = flow.snapshot(task)
    result = await flow.invoke("recover-report", **payload)
    assert result["blocked_reason"] == "report_recovery_submission_digest_mismatch"
    assert flow.snapshot(task) == before
