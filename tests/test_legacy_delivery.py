"""Legacy product journeys: real local daemon/socket/Git/memory/processes.

Only provider/host ports are synthetic; these are never private client receipts.
"""

import copy
import hashlib
import json
from pathlib import Path

import pytest
from test_managed_tasks import git, report
from test_report_prevalidation import flow as flow_fixture
from test_review_product_acceptance import execute, observe_agents
from test_stdio_fifo import run_cli_fifo

from oraculo.protocol import canonical_json

flow = flow_fixture


def commit(cwd, text):
    (Path(cwd) / "example.py").write_text(text)
    git(cwd, "add", "example.py")
    git(
        cwd,
        "-c",
        "user.name=Example",
        "-c",
        "user.email=example@example.org",
        "commit",
        "-m",
        "Synthetic behavior",
    )


async def legacy(flow, *, negative=None, base_relation="older", transport=None):
    observe_agents(flow)
    if transport is not None:
        call = flow.host.call

        async def launch_receipt(name, args):
            result = await call(name, args)
            if name == "delegate_task":
                result = dict(result, status="running", workState="working")
            return result

        flow.host.call = launch_receipt
    task = await flow.begin()
    cwd = Path(task["child_workspace"]["path"])
    commit(
        cwd,
        "def answer():\n    return "
        + ("42" if base_relation == "same" else "41")
        + "\n",
    )
    flow.host.caller = task["child"]["childThreadId"]
    child = await flow.invoke(
        "start",
        kind="work",
        key="legacy-interruption",
        text="Synthetic legacy request without role metadata",
        return_format="json" if transport is not None else "readable",
        cwd=str(cwd),
    )
    assert child["phase"] == "executing", child
    flow.host.complete(child["child"]["childThreadId"], status="failed")
    receipt = await flow.host.call("task_status", {"taskId": child["child"]["taskId"]})
    host_log = cwd.parent / "original-host.json"
    host_log.write_text(canonical_json(receipt))
    if transport is not None:
        code, status, _, stderr = await run_cli_fifo(
            flow,
            transport,
            "resume",
            dict(task_id=child["id"]),
            pad_hex=14000,
            minimum=8192,
        )
        assert code == 0 and status["phase"] == "validating", (status, stderr)
        host_log = next(transport.glob("resume-*/out.txt"))
    failed = report(child)
    failed.update(
        summary="Execution interrupted without a final technical opinion",
        checks=[
            dict(
                command="Observe original host execution",
                status="failed",
                layer="host",
                result="Original terminal failed host execution",
                proof=str(host_log),
            )
        ],
    )
    if negative == "functional":
        failed["checks"][0]["layer"] = "integration"
    if negative == "technical":
        failed["verdict"] = "FAIL"
    if negative == "findings":
        failed["findings"] = ["Unresolved technical finding"]
    if negative == "ambiguous":
        failed["checks"][0].pop("layer")
    if negative == "mislabeled_functional":
        failed["checks"] = [
            execute(
                child["child_workspace"]["path"],
                host_log,
                script="assert False, 'functional failure'",
            )
        ]
        failed["checks"][0]["layer"] = "host"
    ended = await flow.invoke("finish", task_id=child["id"], report=failed)
    assert ended["work_status"] == "failed", ended
    assert ended["memory"]["write_state"] == "saved", ended
    if base_relation == "divergent":
        git(cwd, "reset", "--hard", task["base_sha"])
    if base_relation != "same":
        commit(cwd, "def answer():\n    return 42\n")
    checks = cwd.parent / "final-behavior.log"
    positive = report(task)
    positive["checks"] = [execute(cwd, checks)]
    submitted = await flow.invoke("finish", task_id=task["id"], report=positive)
    assert submitted["phase"] == "validation_submitted", submitted
    flow.host.complete(task["child"]["childThreadId"])
    flow.host.caller = "origin"
    return task, child, positive, checks, host_log


async def decision(flow, task):
    response = await flow.invoke("diagnose-delivery", task_id=task["id"])
    diagnosis = response["legacy_delivery_diagnosis"]
    assert diagnosis["eligible"], diagnosis
    facts = diagnosis["evidence"]
    doc = dict(
        kind="post_execution_delivery_policy_v1",
        author="automated_origin",
        scope="implementation_delivery_only",
        evidence=facts,
        dispositions=[
            dict(
                task_id=r["task_id"],
                scope="host_interruption_only_no_technical_verdict",
                rationale="Current origin policy accepts evidenced implementation delivery; this is not historical role or technical approval.",
                failed_checks=[
                    dict(
                        index=c["index"],
                        check_sha256=c["check_sha256"],
                        log_sha256=c["sha256"],
                    )
                    for c in r["checks"]
                    if c["status"] == "failed"
                ],
            )
            for r in facts["interruptions"]
        ],
    )
    path = Path(task["child_workspace"]["path"]).parent / "delivery-policy.json"
    payload = dict(
        task_id=task["id"],
        reason="Apply current delivery versus integration policy after execution",
    )
    return write_decision(payload, path, doc), doc


def write_decision(payload, path, doc):
    raw = canonical_json(doc).encode()
    path.write_bytes(raw)
    return dict(
        payload, decision=dict(path=str(path), sha256=hashlib.sha256(raw).hexdigest())
    )


@pytest.mark.parametrize("cli", [False, True], ids=["api", "cli"])
@pytest.mark.parametrize("base_relation", ["same", "older"])
async def test_legacy_delivery_journey_without_human_or_retrospective_role(
    flow, tmp_path, cli, base_relation
):
    task, child, positive, _, _ = await legacy(flow, base_relation=base_relation)
    store = flow.service.managed_service.store
    original_child = store.get(child["id"])
    original_request = copy.deepcopy(store.get(task["id"])["request"])
    blocked = await flow.invoke("resume", task_id=task["id"])
    assert blocked["blocked_reason"] == "terminal_descendant_review_required", blocked
    assert blocked["memory"]["write_state"] == "not_requested"
    assert blocked["admission"]["state"] == "executing"
    payload, doc = await decision(flow, task)
    if cli:
        code, result, _, stderr = await run_cli_fifo(
            flow, tmp_path, "recover-delivery", payload, pad_hex=14000, minimum=8192
        )
        assert code == 0, (result, stderr)
    else:
        result = await flow.invoke("recover-delivery", **payload)
    assert result["phase"] == "pr_pending", result
    assert result["work_status"] == "succeeded"
    assert result["memory"]["write_receipt"]["state"] == "confirmed"
    assert result["admission"]["state"] == "not_reserved"
    assert (
        result["publication"] is None and not result["merged"] and not result["settled"]
    )
    stored = store.get(task["id"])
    assert stored["report"] == stored["submitted_report"] == positive
    assert stored["request"] == original_request and "task_role" not in original_request
    assert stored["legacy_delivery_decision"]["document"] == doc
    assert stored["legacy_delivery_decision"]["actor"]["thread_id"] == "origin"
    assert "document" not in result["legacy_delivery_decision"]
    review = stored["delivery_review"]["quality_reviews"][0]
    assert review["host_status"] == "failed"
    assert (review["historical_base"] == positive["commit"]) == (
        base_relation == "same"
    )
    public_review = result["delivery_review"]["quality_reviews"][0]
    assert public_review["provenance"] == "post_execution_origin_policy"
    assert public_review["historical_base"] == review["historical_base"]
    assert public_review["historical_role_asserted"] is False
    assert public_review["final_head_reviewed"] is False
    assert result["legacy_delivery_decision"]["authorship"] == "automated_origin"
    assert review["approval"] is False and review["final_head_reviewed"] is False
    assert (
        review["human_certification"] is False
        and review["redispatch_permitted"] is False
    )
    assert review["provider_restriction"] == "unresolved_or_unproven"
    assert store.get(child["id"]) == original_child
    after = flow.snapshot(task)
    replay = await flow.invoke("recover-delivery", **payload)
    assert replay["phase"] == "pr_pending", replay
    assert flow.snapshot(task) == after  # response lost/retry: same audit and memory
    resumed = await flow.invoke("resume", task_id=task["id"])
    assert resumed["phase"] == "pr_pending"
    assert store.get(child["id"]) == original_child
    assert not any(
        name == "t3_thread_send"
        and args.get("threadId") == child["child"]["childThreadId"]
        for name, args in flow.host.calls
    )


@pytest.mark.parametrize(
    "negative",
    ["functional", "technical", "findings", "ambiguous", "mislabeled_functional"],
)
async def test_legacy_delivery_original_negative_never_erased_by_green_final_checks(
    flow, negative
):
    task, child, *_ = await legacy(flow, negative=negative)
    before = flow.snapshot(task)
    original = flow.service.managed_service.store.get(child["id"])
    response = await flow.invoke("diagnose-delivery", task_id=task["id"])
    assert response["legacy_delivery_diagnosis"] == dict(
        eligible=False,
        missing_evidence=(
            "legacy_delivery_original_host_receipt_required"
            if negative == "mislabeled_functional"
            else "legacy_delivery_negative_scope_unproven"
        ),
        certification=False,
    )
    assert flow.snapshot(task) == before
    assert flow.service.managed_service.store.get(child["id"]) == original


@pytest.mark.parametrize(
    "change",
    [
        "head",
        "target",
        "request",
        "child",
        "base",
        "run",
        "log",
        "disposition",
        "author",
        "scope",
    ],
)
async def test_legacy_delivery_rejects_swapped_or_stale_artifacts_without_effects(
    flow, change
):
    task, _, _, _, _ = await legacy(flow)
    payload, doc = await decision(flow, task)
    facts = doc["evidence"]
    if change == "head":
        facts["target"]["commit"] = "0" * 40
    elif change == "target":
        facts["target"]["task_id"] = "other-task"
    elif change == "request":
        facts["target"]["request_sha256"] = "0" * 64
    elif change == "child":
        facts["interruptions"][0]["task_id"] = "other-child"
    elif change == "base":
        facts["interruptions"][0]["historical_base"] = facts["target"]["commit"]
    elif change == "run":
        facts["execution"]["latest_run_id"] = "other-run"
    elif change == "log":
        facts["implementation_checks"][0]["sha256"] = "0" * 64
    elif change == "disposition":
        doc["dispositions"][0]["failed_checks"] = []
    else:
        doc[change] = "human" if change == "author" else "integration"
    payload = write_decision(payload, Path(payload["decision"]["path"]), doc)
    before = flow.snapshot(task)
    result = await flow.invoke("recover-delivery", **payload)
    assert result["rejection"]["persisted"] is False, result
    assert flow.snapshot(task) == before


@pytest.mark.parametrize("caller", ["executor", "sibling", "wrong_project"])
async def test_legacy_delivery_requires_actual_origin(flow, caller):
    task, child, *_ = await legacy(flow)
    payload, _ = await decision(flow, task)
    if caller == "wrong_project":
        flow.host.threads["origin"]["projectId"] = "beta"
    else:
        flow.host.caller = (
            task["child"]["childThreadId"]
            if caller == "executor"
            else child["child"]["childThreadId"]
        )
        thread = flow.host.threads[flow.host.caller]
        thread.update(status="running", activeRunId=thread["latestRunId"])
    before = flow.snapshot(task)
    if caller == "sibling":
        from oraculo.state import StateError

        with pytest.raises(StateError, match="managed_task_origin_mismatch"):
            await flow.invoke("recover-delivery", **payload)
    else:
        result = await flow.invoke("recover-delivery", **payload)
        assert result["rejection"]["reason"] == "recovery_requires_origin", result
    assert flow.snapshot(task) == before


@pytest.mark.parametrize("when", ["before", "during_memory", "after"])
@pytest.mark.parametrize(
    "change",
    [
        "head",
        "checks",
        "host_log",
        "new_run",
        "new_child",
        "history",
        "request",
        "failed_memory",
        "parent_memory",
    ],
)
async def test_legacy_delivery_fresh_gates_prevent_release_or_reuse(flow, when, change):
    from oraculo.state import StateError

    task, child, _, checks, host_log = await legacy(flow)
    payload, _ = await decision(flow, task)
    facade = flow.service.managed_service
    factory = facade.memory
    get = facade.store.get
    armed = False

    def mutate():
        nonlocal armed
        armed = True
        if change == "head":
            git(
                task["child_workspace"]["path"],
                "-c",
                "user.name=Example",
                "-c",
                "user.email=example@example.org",
                "commit",
                "--allow-empty",
                "-m",
                "New source",
            )
        elif change == "checks":
            checks.write_text("Modified check log")
        elif change == "host_log":
            host_log.write_text('{"status":"failed","unknown":true}')
        elif change == "new_run":
            flow.host.threads[child["child"]["childThreadId"]].update(
                status="running", activeRunId="new-run", latestRunId="new-run"
            )
        elif change == "new_child":
            flow.host.complete(task["child"]["childThreadId"], pending=True)

    def changed_get(task_id):
        value = get(task_id)
        if armed:
            if change == "history" and task_id == child["id"]:
                value["events"] = value["events"][1:]
            if change == "request" and task_id == child["id"]:
                value["request"]["text"] = "Replaced legacy request"
        return value

    facade.store.get = changed_get

    def memory():
        adapter = factory()
        finish, status = adapter.finish, adapter.status

        async def current(*args, **kwargs):
            cycle = await status(*args, **kwargs)
            if armed:
                if change == "failed_memory" and cycle["id"] == child["memory"]["id"]:
                    cycle["write_receipt"] = None
                if change == "parent_memory" and cycle["id"] == task["memory"]["id"]:
                    raise StateError("synthetic_memory_unavailable")
            return cycle

        async def write(*args, **kwargs):
            # The reservation must survive until after the post-memory gates.
            assert flow.service.state.capacity()["occupied"] == 1
            cycle = await finish(*args, **kwargs)
            if when == "during_memory":
                mutate()
            return cycle

        adapter.status, adapter.finish = current, write
        return adapter

    facade.memory = memory
    if when == "after":
        accepted = await flow.invoke("recover-delivery", **payload)
        assert accepted["work_status"] == "succeeded", accepted
        mutate()
        # Same payload or resume must re-read; a previous green audit is no cache.
        action, arguments = "resume", dict(task_id=task["id"])
    else:
        if when == "before":
            mutate()
        action, arguments = "recover-delivery", payload
    result = await flow.invoke(action, **arguments)
    assert result["phase"] == "blocked", result
    assert result["publication"] is None and not result["merged"]
    if when != "after":
        assert result["admission"]["state"] == "executing", result
        if when == "before":
            assert result["memory"]["write_state"] == "not_requested"
        else:
            assert result["memory"]["write_receipt"]["state"] == "confirmed"
    assert get(child["id"])["work_status"] == "failed"


@pytest.mark.parametrize(
    "defect",
    [
        "truncated_runs",
        "missing_runs",
        "missing_child",
        "effect",
        "unproven_log",
        "functional_label",
    ],
)
async def test_legacy_delivery_missing_operational_proof_blocks_with_specific_gap(
    flow, defect
):
    task, child, _, _, host_log = await legacy(flow)
    facade = flow.service.managed_service
    original_call, get = flow.host.call, facade.store.get
    if defect in {"truncated_runs", "missing_runs"}:

        async def call(name, args):
            result = await original_call(name, args)
            if (
                name == "t3_thread_read"
                and args["threadId"] == child["child"]["childThreadId"]
            ):
                if defect == "truncated_runs":
                    result["thread"]["runCount"] = 2
                else:
                    result["recentRuns"] = []
            return result

        flow.host.call = call
    elif defect == "missing_child":

        def missing(task_id):
            value = get(task_id)
            if task_id == child["id"]:
                value["child"]["childThreadId"] = "another-thread"
            return value

        facade.store.get = missing
    elif defect == "effect":
        (Path(child["child_workspace"]["path"]) / "uncertain.txt").write_text(
            "uncommitted effect"
        )
    elif defect == "unproven_log":
        host_log.write_text("provider declined; no structured host receipt")
    else:

        def functional(task_id):
            value = get(task_id)
            if task_id == child["id"]:
                value["request"]["task_role"] = "functional_tests"
            return value

        facade.store.get = functional
    before = flow.snapshot(task)
    result = await flow.invoke("diagnose-delivery", task_id=task["id"])
    assert (
        result.get("phase") == "blocked"
        or result["legacy_delivery_diagnosis"]["eligible"] is False
    ), result
    assert result["admission"]["state"] == "executing"
    assert flow.snapshot(task)["cycle"] == before["cycle"]


@pytest.mark.parametrize(
    "defect", ["relative", "inside", "symlink", "oversize", "digest", "directory"]
)
async def test_legacy_delivery_private_artifact_bounds(flow, defect):
    task, _, *_ = await legacy(flow)
    payload, doc = await decision(flow, task)
    path = Path(payload["decision"]["path"])
    if defect == "relative":
        payload["decision"]["path"] = path.name
    elif defect in {"inside", "symlink"}:
        inside = Path(task["child_workspace"]["path"]) / "private-policy.json"
        inside.write_text(canonical_json(doc))
        if defect == "inside":
            payload["decision"]["path"] = str(inside)
        else:
            link = path.parent / "linked-policy.json"
            link.symlink_to(inside)
            payload["decision"]["path"] = str(link)
    elif defect == "oversize":
        path.write_bytes(b"x" * 524289)
        payload["decision"]["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    elif defect == "directory":
        payload["decision"]["path"] = str(path.parent)
    else:
        payload["decision"]["sha256"] = "0" * 64
    before = flow.snapshot(task)
    result = await flow.invoke("recover-delivery", **payload)
    assert result["rejection"]["persisted"] is False, result
    assert flow.snapshot(task) == before


async def test_legacy_delivery_conflicting_retry_preserves_accepted_decision(flow):
    task, _, *_ = await legacy(flow)
    payload, _ = await decision(flow, task)
    accepted = await flow.invoke("recover-delivery", **payload)
    assert accepted["work_status"] == "succeeded", accepted
    original = flow.snapshot(task)
    conflict = await flow.invoke(
        "recover-delivery", **dict(payload, reason="Different policy intention")
    )
    assert conflict["blocked_reason"] == "legacy_delivery_intent_reused"
    # An accepted audit remains even when its conflicting replay is refused.
    after = flow.snapshot(task)
    assert (
        after["task"]["legacy_delivery_decision"]
        == original["task"]["legacy_delivery_decision"]
    )
    assert after["cycle"] == original["cycle"]


@pytest.mark.parametrize("action", ["diagnose-delivery", "recover-delivery"])
def test_legacy_delivery_old_daemon_refuses_new_policy(
    service_config, tmp_path, monkeypatch, capsys, action
):
    from oraculo.cli import main

    _, config = service_config
    path = tmp_path / "request.json"
    path.write_text(json.dumps(dict(task_id="synthetic-task")))

    async def capabilities(*args):
        return {"ok": True, "data": {"protocol_version": 1}}

    monkeypatch.setattr("oraculo.cli.request", capabilities)
    assert main(["--config", str(config), "task", action, "--file", str(path)]) == 1
    assert "legacy_delivery_service_upgrade_required" in capsys.readouterr().err


async def test_legacy_delivery_nonancestor_base_is_not_operational_scope_proof(flow):
    task, child, *_ = await legacy(flow, base_relation="divergent")
    before = flow.snapshot(task)
    result = await flow.invoke("diagnose-delivery", task_id=task["id"])
    diagnosis = result["legacy_delivery_diagnosis"]
    assert diagnosis["eligible"] is False
    assert (
        diagnosis["missing_evidence"] == "legacy_delivery_historical_base_not_ancestor"
    )
    assert flow.snapshot(task) == before
    assert (
        flow.service.managed_service.store.get(child["id"])["work_status"] == "failed"
    )


@pytest.mark.parametrize("action", ["diagnose-delivery", "recover-delivery"])
async def test_legacy_delivery_api_rejects_old_daemon_before_host_or_effects(
    service_config, monkeypatch, action
):
    from oraculo.managed_service import command
    from oraculo.state import StateError

    config, _ = service_config

    async def capabilities(*args):
        return {"ok": True, "data": {"protocol_version": 1}}

    monkeypatch.setattr("oraculo.cli.request", capabilities)
    with pytest.raises(StateError, match="legacy_delivery_service_upgrade_required"):
        await command(config, action, {}, None)


async def test_legacy_delivery_memory_write_unconfirmed_holds_reservation(flow):
    task, _, *_ = await legacy(flow)
    payload, _ = await decision(flow, task)
    facade = flow.service.managed_service
    factory = facade.memory

    def memory():
        adapter = factory()
        finish = adapter.finish

        async def unavailable(*args, **kwargs):
            cycle = await finish(*args, **kwargs)
            # Simulate unavailable ACK after the real isolated memory write.
            cycle["write_state"], cycle["write_receipt"] = "pending", None
            return cycle

        adapter.finish = unavailable
        return adapter

    facade.memory = memory
    failed = await flow.invoke("recover-delivery", **payload)
    assert failed["blocked_reason"] == "memory_write_unconfirmed", failed
    assert failed["admission"]["state"] == "executing"
    assert failed["legacy_delivery_decision"] is not None
    facade.memory = factory
    # Lost memory response uses the original cycle and policy, never a new task.
    resumed = await flow.invoke("recover-delivery", **payload)
    assert resumed["work_status"] == "succeeded", resumed
    assert resumed["admission"]["state"] == "not_reserved"
    assert resumed["memory"]["id"] == task["memory"]["id"]


@pytest.mark.parametrize("stage", ["publish", "publish_replay", "merge", "ci_pending"])
async def test_legacy_delivery_integration_has_separate_authority_and_fresh_proofs(
    flow, monkeypatch, stage
):
    from test_managed_tasks import GitHubHost, initialize
    from test_publication import specification
    from test_task_closure import Lifecycle

    from oraculo.publication import GitHubPublisher

    task, child, positive, checks, _ = await legacy(flow)
    payload, _ = await decision(flow, task)
    accepted = await flow.invoke("recover-delivery", **payload)
    assert accepted["publication"] is None and not accepted["merged"]
    store = flow.service.managed_service.store
    original_child = store.get(child["id"])
    public = flow.service.config.projects_root / "public-project"
    initialize(public)
    git(public, "switch", "-c", "task/example")
    git(
        public,
        "remote",
        "add",
        "origin",
        "https://github.com/example/public-project.git",
    )
    spec = specification(public)
    spec.update(
        cwd=str(public),
        publication_root=str(public),
        repository="example/public-project",
        source_project_id="alpha",
        source_commit=positive["commit"],
        task_id=task["id"],
    )
    publisher = GitHubPublisher(GitHubHost(spec))
    lifecycle = Lifecycle()
    lifecycle.ready = False  # no remote receipt can be inferred from recovery
    lifecycle.blockers = ["required_product_ci_pending"]
    monkeypatch.setattr("oraculo.publication.RepositoryPublisher", lambda: publisher)
    monkeypatch.setattr("oraculo.pr_lifecycle.RepositoryPRLifecycle", lambda: lifecycle)
    call = flow.host.call

    async def host(name, args):
        if name == "watch_pull_request":
            return dict(url=args["url"], watching=True)
        return await call(name, args)

    flow.host.call = host
    if stage != "publish":
        published = await flow.invoke("publish", task_id=task["id"], publication=spec)
        assert published["phase"] == "pr_open", published
    if stage != "ci_pending":
        checks.write_text("Changed implementation proof after accepted policy")
    if stage in {"publish", "publish_replay"}:
        result = await flow.invoke("publish", task_id=task["id"], publication=spec)
    else:
        policy = dict(
            repository=spec["repository"],
            base=spec["base"],
            head_sha=spec["head_sha"],
            merge_authorized=True,
            required_checks=["Product review (exact HEAD)"],
            required_review=True,
            method="squash",
            policy_source="Separate synthetic integration policy",
        )
        result = await flow.invoke("merge", task_id=task["id"], policy=policy)
    assert not result["merged"] and not result["settled"]
    assert lifecycle.calls == 0
    if stage == "ci_pending":
        assert result["phase"] == "pr_waiting", result
    else:
        assert (
            result["blocked_reason"]
            == "legacy_delivery_evidence_changed_or_scope_mismatch"
        ), result
    assert result["legacy_delivery_decision"]["integration_authorized"] is False
    assert store.get(child["id"]) == original_child


@pytest.mark.parametrize("change", ["head", "check_log", "child_effect", "new_run"])
async def test_legacy_delivery_rechecks_host_and_bytes_after_memory_reads(flow, change):
    task, child, _, checks, _ = await legacy(flow)
    payload, _ = await decision(flow, task)
    facade = flow.service.managed_service
    factory = facade.memory
    changed = False

    def memory():
        adapter = factory()
        status = adapter.status

        async def read(*args, **kwargs):
            nonlocal changed
            cycle = await status(*args, **kwargs)
            if (
                not changed
                and cycle["id"] == task["memory"]["id"]
                and facade.store.get(task["id"]).get("legacy_delivery_decision")
            ):
                changed = True
                if change == "head":
                    git(
                        task["child_workspace"]["path"],
                        "-c",
                        "user.name=Example",
                        "-c",
                        "user.email=example@example.org",
                        "commit",
                        "--allow-empty",
                        "-m",
                        "Source changed during memory read",
                    )
                elif change == "check_log":
                    checks.write_text("Changed while memory status yielded")
                elif change == "child_effect":
                    (Path(child["child_workspace"]["path"]) / "unknown.txt").write_text(
                        "uncertain effect"
                    )
                else:
                    flow.host.threads[child["child"]["childThreadId"]].update(
                        status="running",
                        activeRunId="later-run",
                        latestRunId="later-run",
                    )
            return cycle

        adapter.status = read
        return adapter

    facade.memory = memory
    result = await flow.invoke("recover-delivery", **payload)
    assert changed
    assert result["phase"] == "blocked", result
    assert result["admission"]["state"] == "executing"
    assert result["memory"]["write_state"] == "not_requested"
    assert result["legacy_delivery_decision"] is not None


@pytest.mark.parametrize("cli", [False, True], ids=["api", "cli"])
@pytest.mark.parametrize("base_relation", ["same", "older"])
async def test_legacy_stream_delivery_uses_explicit_current_corroboration(
    flow, tmp_path, cli, base_relation
):
    task, child, _, _, stream = await legacy(
        flow, base_relation=base_relation, transport=tmp_path
    )
    store = flow.service.managed_service.store
    original_child = store.get(child["id"])
    original_bytes = stream.read_bytes()
    start_calls = len(flow.host.calls)
    payload, doc = await decision(flow, task)
    evidence = doc["evidence"]["interruptions"][0]
    historic = evidence["checks"][0]["historical_artifact"]
    assert historic["kind"] == "historical_managed_stdio_stream_v1"
    assert historic["historical_terminal_receipt"] is False
    assert historic["historical_launch_status"] == "running"
    assert (
        evidence["terminal_corroboration"]["basis"]
        == "current_caller_bound_host_snapshot"
    )
    assert evidence["terminal_corroboration"]["temporal_scope"] == "post_execution"
    assert evidence["terminal_corroboration"]["host_status"] == "failed"
    assert (
        evidence["terminal_corroboration"]["original_run_id"]
        == child["child"]["childRunId"]
    )
    if cli:
        code, result, _, stderr = await run_cli_fifo(
            flow, tmp_path, "recover-delivery", payload, pad_hex=14000, minimum=8192
        )
        assert code == 0, (result, stderr)
    else:
        result = await flow.invoke("recover-delivery", **payload)
    assert result["work_status"] == "succeeded", result
    assert result["admission"]["state"] == "not_reserved"
    assert result["memory"]["write_receipt"]["state"] == "confirmed"
    assert result["publication"] is None and not result["merged"]
    assert result["legacy_delivery_decision"]["final_head_reviewed"] is False
    assert result["legacy_delivery_decision"]["provider_redispatch_permitted"] is False
    assert store.get(child["id"]) == original_child
    assert stream.read_bytes() == original_bytes
    # Deep descendants are corroborated through real caller-bound tree reads;
    # the origin never impersonates their parent to query its native task.
    assert not any(
        name == "task_status" and args["taskId"] == child["child"]["taskId"]
        for name, args in flow.host.calls[start_calls:]
    )
    after = flow.snapshot(task)
    replay = await flow.invoke("recover-delivery", **payload)
    assert replay["phase"] == "pr_pending", replay
    assert flow.snapshot(task) == after


@pytest.mark.parametrize(
    "defect",
    [
        "mixed",
        "truncated",
        "hidden_json",
        "functional_stdout",
        "stdout_json",
        "wrong_frame",
        "wrong_run",
        "wrong_origin",
        "event_prefix",
        "missing_frame",
        "duplicate_key",
        "unknown_tool",
    ],
)
async def test_legacy_stream_rejects_unbound_mixed_or_functional_artifacts(
    flow, tmp_path, defect
):
    task, child, _, _, stream = await legacy(flow, transport=tmp_path)
    lines = stream.read_text().splitlines()
    index = lines.index("{")
    frames = [json.loads(line) for line in lines[:index]]
    snapshot = json.loads("\n".join(lines[index:]))
    if defect == "mixed":
        raw = stream.read_text() + "\nassert False\n"
    elif defect == "truncated":
        raw = stream.read_text()[:-100]
    elif defect == "hidden_json":
        raw = json.dumps({"stdout": stream.read_text()})
    elif defect == "functional_stdout":
        raw = "Traceback: AssertionError: required functional test failed"
    elif defect == "stdout_json":
        raw = json.dumps(
            dict(
                taskId=child["child"]["taskId"],
                childThreadId=child["child"]["childThreadId"],
                childRunId=child["child"]["childRunId"],
                status="failed",
                workState="result_available",
                hasPendingChildRuns=False,
                hasPendingQuestion=False,
                stdout="assert False",
            )
        )
    else:
        if defect == "wrong_frame":
            frames[4]["arguments"]["taskId"] = "swapped-task"
        elif defect == "wrong_run":
            snapshot["child"]["childRunId"] = "swapped-run"
        elif defect == "wrong_origin":
            frames[1]["arguments"]["threadId"] = "swapped-origin"
        elif defect == "event_prefix":
            snapshot["events"][-1]["proof"] = dict(status="completed")
        elif defect == "missing_frame":
            frames.pop(4)
        elif defect == "unknown_tool":
            frames[2]["tool"] = "arbitrary_tool"
        raw = (
            "\n".join(canonical_json(f) for f in frames)
            + "\n"
            + json.dumps(snapshot, indent=2)
            + "\n"
        )
        if defect == "duplicate_key":
            raw = raw.replace(
                '"source": "service"', '"source": "service", "source": "service"'
            )
    stream.write_text(raw)
    before = flow.snapshot(task)
    result = await flow.invoke("diagnose-delivery", task_id=task["id"])
    assert result["legacy_delivery_diagnosis"]["eligible"] is False, result
    assert flow.snapshot(task) == before
    assert result["admission"]["state"] == "executing"


@pytest.mark.parametrize(
    "defect", ["missing_history", "wrong_run", "not_terminal", "wrong_relation"]
)
async def test_legacy_stream_without_fresh_coherent_corroboration_stays_blocked(
    flow, tmp_path, defect
):
    task, child, *_ = await legacy(flow, transport=tmp_path)
    call = flow.host.call

    async def host(name, args):
        result = await call(name, args)
        if (
            name == "t3_thread_read"
            and args["threadId"] == child["child"]["childThreadId"]
        ):
            if defect == "missing_history":
                result["recentRuns"] = []
            elif defect == "wrong_run":
                result["thread"]["latestRunId"] = "different-original"
                result["recentRuns"][0]["runId"] = "different-original"
            elif defect == "wrong_relation":
                result["thread"]["parentThreadId"] = "different-parent"
            else:
                result["thread"].update(
                    status="running", activeRunId=child["child"]["childRunId"]
                )
                result["recentRuns"][0]["status"] = "running"
        return result

    flow.host.call = host
    before = flow.snapshot(task)
    result = await flow.invoke("diagnose-delivery", task_id=task["id"])
    # N-shared-diff-3: a failing diagnosis stays eligible=false and does not
    # persist blocked, including when the child run is still active.
    assert result["legacy_delivery_diagnosis"]["eligible"] is False, result
    assert result["phase"] == before["task"]["phase"]
    assert flow.snapshot(task) == before
    assert result["admission"]["state"] == "executing"


@pytest.mark.parametrize(
    "negative",
    ["technical", "functional", "findings", "ambiguous", "mislabeled_functional"],
)
async def test_legacy_stream_never_disposes_proven_or_ambiguous_technical_negatives(
    flow, tmp_path, negative
):
    task, child, *_ = await legacy(flow, transport=tmp_path, negative=negative)
    store = flow.service.managed_service.store
    before = flow.snapshot(task)
    original = store.get(child["id"])
    result = await flow.invoke("diagnose-delivery", task_id=task["id"])
    assert result["legacy_delivery_diagnosis"]["eligible"] is False, result
    assert store.get(child["id"]) == original
    assert flow.snapshot(task) == before
    assert result["memory"]["write_state"] == "not_requested"
    assert result["admission"]["state"] == "executing"
