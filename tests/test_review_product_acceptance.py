"""Product journeys: implementation delivery, quality review, CI and merge differ.

Local service/socket/Git/memory/test processes are real; T3/GitHub are synthetic.
"""

import copy
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
from test_managed_tasks import git, report
from test_report_prevalidation import flow as flow_fixture
from test_stdio_fifo import run_cli_fifo

from oraculo.delivery_review import target

flow = flow_fixture


def observe_agents(flow):
    original = flow.host.call

    async def call(name, args):
        result = await original(name, args)
        if name == "t3_thread_read":
            for run in result["recentRuns"]:
                run.update(providerInstanceId="codex", model="example")
        return result

    flow.host.call = call
    flow.service.state.max_executors = 3


def execute(cwd, proof, *, script=None):
    argv = [
        sys.executable,
        "-B",
        "-c",
        script
        or "from example import answer; assert answer() == 42; print('behavior passed')",
    ]
    with proof.open("w") as stream:
        result = subprocess.run(argv, cwd=cwd, stdout=stream, stderr=subprocess.STDOUT)
    return dict(
        command=" ".join(argv),
        status="passed" if result.returncode == 0 else "failed",
        result=f"Observed exit {result.returncode}",
        proof=str(proof),
    )


async def implementation(
    flow, key="normal", *, negative=False, evidence=None, extra_files=None, script=None
):
    flow.host.caller = "origin"
    task = await flow.invoke(
        "start",
        kind="work",
        key=key,
        text="Implement requested answer; mandatory behavior tests are part of delivery; product review runs in CI",
        cwd=str(flow.service.config.projects_root / "alpha"),
    )
    assert task["phase"] == "executing", task
    cwd = Path(task["child_workspace"]["path"])
    (cwd / "example.py").write_text(
        f"def answer():\n    return {41 if negative else 42}\n"
    )
    for name, content in (extra_files or {}).items():
        (cwd / name).write_text(content)
    git(cwd, "add", ".")
    git(
        cwd,
        "-c",
        "user.name=Example",
        "-c",
        "user.email=example@example.org",
        "commit",
        "-m",
        "Requested product behavior",
    )
    proof = cwd.parent / f"{key}-implementation.log"
    data = report(task)
    if evidence:
        data["evidence"] = evidence
    data["checks"] = [execute(cwd, proof, script=script)]
    flow.host.caller = task["child"]["childThreadId"]
    submitted = await flow.invoke("finish", task_id=task["id"], report=data)
    assert submitted["phase"] == "validation_submitted", submitted
    return task, data, proof


async def reviewer(
    flow,
    task,
    *,
    outcome="no_verdict",
    role="quality_review",
    complete_parent=True,
    script=None,
):
    flow.host.caller = task["child"]["childThreadId"]
    stored = flow.service.managed_service.store.get(task["id"])
    request = dict(
        kind="work",
        key="review-" + outcome,
        text="Synthetic quality review; a quality opinion is distinct from required functional tests",
        cwd=task["child_workspace"]["path"],
        base=stored["submitted_report"]["commit"],
        deliverable_kind="validation",
    )
    if role:
        request["task_role"] = role
    if role in {"quality_review", "functional_tests"}:
        request["review_target"] = target(stored)
    child = await flow.invoke("start", **request)
    assert child["phase"] == "executing", child
    thread = child["child"]["childThreadId"]
    if outcome in {"live", "queued", "unknown"}:
        flow.host.threads[thread]["status"] = {
            "live": "running",
            "queued": "queued",
            "unknown": "waiting",
        }[outcome]
    else:
        status = (
            "failed"
            if outcome in {"no_verdict", "security", "no_report"}
            else "completed"
        )
        flow.host.complete(thread, status=status)
        if outcome == "security":
            flow.host.threads[thread]["providerMessage"] = (
                "Synthetic possible provider safety restriction; no final opinion"
            )
        if outcome != "no_report":
            proof = (
                Path(child["child_workspace"]["path"]).parent / f"{outcome}-review.log"
            )
            data = report(child)
            data["deliverable_kind"] = "validation"
            if status == "failed":
                proof.write_text(
                    json.dumps(
                        await flow.host.call(
                            "task_status", {"taskId": child["child"]["taskId"]}
                        )
                    )
                )
                data.update(
                    summary="Reviewer ended without final technical opinion",
                    checks=[
                        dict(
                            command="Observe original provider task",
                            status="failed",
                            layer="host",
                            result="Original run failed; no final opinion",
                            proof=str(proof),
                        )
                    ],
                )
            elif outcome in {"quality_finding", "functional_failure"}:
                check = execute(
                    child["child_workspace"]["path"],
                    proof,
                    script="assert False, 'synthetic negative check'",
                )
                check["layer"] = (
                    "quality" if outcome == "quality_finding" else "integration"
                )
                data.update(
                    summary="Negative validation retained",
                    verdict="FAIL",
                    checks=[check],
                )
            else:
                data["checks"] = [
                    execute(child["child_workspace"]["path"], proof, script=script)
                ]
            saved = await flow.invoke("finish", task_id=child["id"], report=data)
            assert saved["memory"]["write_receipt"]["state"] == "confirmed", saved
    if complete_parent:
        flow.host.complete(task["child"]["childThreadId"])
        flow.host.caller = "origin"
    return child


@pytest.mark.parametrize("cli", [False, True], ids=["api", "cli"])
@pytest.mark.parametrize(
    "outcome", ["no_verdict", "security", "no_report", "quality_finding", "passed"]
)
async def test_delivery_completes_without_quality_approval_or_humans(
    flow, tmp_path, cli, outcome
):
    observe_agents(flow)
    task, data, _ = await implementation(flow)
    child = await reviewer(flow, task, outcome=outcome)
    store = flow.service.managed_service.store
    original_child = store.get(child["id"])
    if cli:
        code, result, _, stderr = await run_cli_fifo(
            flow,
            tmp_path,
            "resume",
            dict(task_id=task["id"]),
            pad_hex=14000,
            minimum=8192,
        )
        assert code == 0, (result, stderr)
    else:
        result = await flow.invoke("resume", task_id=task["id"])
    assert result["phase"] == "pr_pending", result
    assert result["work_status"] == "succeeded"
    assert result["memory"]["write_receipt"]["state"] == "confirmed"
    assert result["admission"]["state"] == "not_reserved"
    assert result["pending_returns"] == 0
    assert result["delivery_review"]["review_approved"] is False
    assert result["delivery_review"]["integration_authorized"] is False
    quality = result["delivery_review"]["quality_reviews"][0]
    assert quality["approval"] is False and quality["human_certification"] is False
    assert quality["redispatch_permitted"] is False
    assert quality["quality_state"] == (
        "changes_requested" if outcome == "quality_finding" else "not_approved"
    )
    if outcome == "security":
        assert quality["host_status"] == "failed"
        assert quality["provider_restriction"] == "unresolved_or_unproven"
    stored = store.get(task["id"])
    assert stored["report"] == stored["submitted_report"] == data
    assert stored.get("review_adjudication") is None
    assert stored["child"] == task["child"]
    assert store.get(child["id"]) == original_child
    assert flow.service.state.capacity()["occupied"] == (
        1 if outcome == "no_report" else 0
    )
    after = flow.snapshot(task)
    calls = len(flow.host.calls)
    again = await flow.invoke("resume", task_id=task["id"])
    assert again["phase"] == "pr_pending" and flow.snapshot(task) == after
    assert not any(
        n in {"delegate_task", "t3_thread_launch"} for n, _ in flow.host.calls[calls:]
    )


@pytest.mark.parametrize(
    "role,outcome",
    [
        ("functional_tests", "functional_failure"),
        ("quality_review", "functional_failure"),
        (None, "no_verdict"),
        ("quality_review", "live"),
        ("quality_review", "queued"),
        ("quality_review", "unknown"),
    ],
)
async def test_functional_failure_unknown_role_and_live_work_still_block_delivery(
    flow, role, outcome
):
    observe_agents(flow)
    task, _, _ = await implementation(flow)
    child = await reviewer(flow, task, outcome=outcome, role=role)
    original = flow.service.managed_service.store.get(child["id"])
    result = await flow.invoke("resume", task_id=task["id"])
    assert result["phase"] == "blocked", result
    assert result["work_status"] == "pending"
    assert result["memory"]["write_state"] == "not_requested"
    assert result["admission"]["state"] == "executing"
    if outcome == "functional_failure":
        assert result["blocked_reason"] == "mandatory_functional_validation_failed"
    assert flow.service.managed_service.store.get(child["id"]) == original


async def test_functional_negative_correction_preserves_original_report_memory_and_ids(
    flow,
):
    observe_agents(flow)
    old, negative, _ = await implementation(flow, "broken", negative=True)
    child = await reviewer(flow, old)
    failed = await flow.invoke("resume", task_id=old["id"])
    assert failed["work_status"] == "failed"
    original_report = flow.service.managed_service.store.get(old["id"])["report"]
    original_memory = copy.deepcopy(failed["memory"])
    original_child = flow.service.managed_service.store.get(child["id"])
    fixed, positive, _ = await implementation(flow, "authorized-correction")
    flow.host.complete(fixed["child"]["childThreadId"])
    flow.host.caller = "origin"
    accepted = await flow.invoke("resume", task_id=fixed["id"])
    assert accepted["phase"] == "pr_pending", accepted
    abandoned = await flow.invoke(
        "supersede",
        task_id=old["id"],
        replacement_task_id=fixed["id"],
        reason="Authorized corrected implementation and new mandatory tests; preserve negative original",
    )
    assert abandoned["phase"] == "superseded"
    stored = flow.service.managed_service.store.get(old["id"])
    assert stored["report"] == original_report and stored["memory"] == original_memory
    assert stored["child"] == old["child"]
    assert flow.service.managed_service.store.get(child["id"]) == original_child
    assert positive["commit"] != negative["commit"]


@pytest.mark.parametrize(
    "change", ["new_head", "test_log", "new_review_run", "role_target"]
)
async def test_delivery_revalidates_after_memory_without_fake_review_pass(flow, change):
    observe_agents(flow)
    task, _, proof = await implementation(flow)
    child = await reviewer(flow, task, outcome="security")
    facade = flow.service.managed_service
    factory = facade.memory

    def memory():
        adapter = factory()
        finish = adapter.finish

        async def changed(*args, **kwargs):
            cycle = await finish(*args, **kwargs)
            if change == "new_head":
                git(
                    task["child_workspace"]["path"],
                    "-c",
                    "user.name=Example",
                    "-c",
                    "user.email=example@example.org",
                    "commit",
                    "--allow-empty",
                    "-m",
                    "New HEAD",
                )
            elif change == "test_log":
                proof.write_text("changed test output")
            elif change == "new_review_run":
                flow.host.threads[child["child"]["childThreadId"]].update(
                    activeRunId="new-run", latestRunId="new-run", status="running"
                )
            else:
                get = facade.store.get

                def changed_target(task_id):
                    value = get(task_id)
                    if task_id == child["id"]:
                        value["request"]["review_target"]["commit"] = "0" * 40
                    return value

                facade.store.get = changed_target
            return cycle

        adapter.finish = changed
        return adapter

    facade.memory = memory
    result = await flow.invoke("resume", task_id=task["id"])
    assert result["phase"] == "blocked", result
    assert result["memory"]["write_receipt"]["state"] == "confirmed"
    assert result["admission"]["state"] == "executing"
    assert not result["merged"] and not result["settled"]


@pytest.mark.parametrize("role", ["quality_review", "functional_tests"])
def test_old_daemon_cannot_silently_accept_new_validation_roles(
    service_config, tmp_path, monkeypatch, capsys, role
):
    from oraculo.cli import main

    _, config = service_config
    path = tmp_path / "request.json"
    path.write_text(json.dumps(dict(kind="work", task_role=role)))

    async def capabilities(*args):
        return {"ok": True, "data": {"protocol_version": 1}}

    monkeypatch.setattr("oraculo.cli.request", capabilities)
    assert main(["--config", str(config), "task", "start", "--file", str(path)]) == 1
    assert "delivery_review_service_upgrade_required" in capsys.readouterr().err


async def test_quality_role_and_host_label_require_original_bound_receipt(flow):
    observe_agents(flow)
    task, _, _ = await implementation(flow)
    child = await reviewer(flow, task)
    original = flow.service.managed_service.store.get(child["id"])
    Path(original["report"]["checks"][0]["proof"]).write_text('{"approved":true}')
    result = await flow.invoke("resume", task_id=task["id"])
    assert result["blocked_reason"] == "quality_review_interruption_receipt_unproven", (
        result
    )
    assert result["memory"]["write_state"] == "not_requested"
    assert flow.service.managed_service.store.get(child["id"]) == original


@pytest.mark.parametrize(
    "ci_state", ["pending", "failed", "new_head", "quality_finding", "ready"]
)
async def test_delivered_quality_review_does_not_authorize_merge_and_ci_revalidates_head(
    flow, monkeypatch, ci_state
):
    from test_managed_tasks import GitHubHost, initialize
    from test_publication import specification
    from test_task_closure import Lifecycle

    from oraculo.publication import GitHubPublisher

    observe_agents(flow)
    task, evidence, _ = await implementation(flow)
    review = await reviewer(
        flow,
        task,
        outcome="quality_finding" if ci_state == "quality_finding" else "security",
    )
    delivered = await flow.invoke("resume", task_id=task["id"])
    assert delivered["phase"] == "pr_pending", delivered
    receipt = delivered["memory"]["write_receipt"]
    original_review = flow.service.managed_service.store.get(review["id"])
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
        source_commit=evidence["commit"],
        task_id=task["id"],
    )
    github = GitHubHost(spec)
    lifecycle = Lifecycle()
    lifecycle.ready = ci_state not in {"pending", "failed"}
    lifecycle.blockers = ["product_ci_" + ci_state] if not lifecycle.ready else []
    monkeypatch.setattr(
        "oraculo.publication.RepositoryPublisher", lambda: GitHubPublisher(github)
    )
    monkeypatch.setattr("oraculo.pr_lifecycle.RepositoryPRLifecycle", lambda: lifecycle)
    original = flow.host.call

    async def observed(name, args):
        if name in {"watch_pull_request", "unwatch_pull_request"}:
            return dict(url=args["url"], watching=name == "watch_pull_request")
        if name == "t3_thread_organize":
            flow.host.threads[args["threadId"]]["settled"] = True
            return {"accepted": True}
        result = await original(name, args)
        if name == "list_thread_pull_requests":
            result["pullRequests"] = [
                dict(
                    url=r["url"],
                    host="github.com",
                    repository=spec["repository"],
                    number=int(r["url"].rsplit("/", 1)[1]),
                    state="merged" if lifecycle.merged else "open",
                    isDraft=False,
                    baseBranch=spec["base"],
                )
                for r in result["pullRequests"]
            ]
        return result

    flow.host.call = observed
    published = await flow.invoke("publish", task_id=task["id"], publication=spec)
    assert published["phase"] == "pr_open", published
    policy = dict(
        repository=spec["repository"],
        base=spec["base"],
        head_sha="c" * 40 if ci_state == "new_head" else spec["head_sha"],
        merge_authorized=True,
        required_checks=["Product review (exact HEAD)"],
        required_review=False,
        method="squash",
        policy_source="Synthetic explicit CI-only quality policy; no human requirement",
    )
    result = await flow.invoke("merge", task_id=task["id"], policy=policy)
    if ci_state == "ready":
        assert result["phase"] == "settled", result
        assert result["merged"] and lifecycle.calls == 1
    else:
        assert not result["merged"] and not result["settled"]
        assert lifecycle.calls == 0
        if ci_state == "quality_finding":
            assert result["blocked_reason"] == "quality_review_changes_requested"
        elif ci_state == "new_head":
            assert (
                result["blocked_reason"]
                == "new_head_requires_new_validation_and_publication"
            )
        else:
            assert result["phase"] == "pr_waiting", result
    assert result["memory"]["write_receipt"] == receipt
    assert result["work_status"] == "succeeded"
    assert result["delivery_review"]["review_approved"] is False
    assert result["delivery_review"]["integration_authorized"] is False
    assert flow.service.managed_service.store.get(review["id"]) == original_review
    assert flow.host.threads[review["child"]["childThreadId"]]["status"] == (
        "completed" if ci_state == "quality_finding" else "failed"
    )


@pytest.mark.parametrize(
    "with_quality", [False, True], ids=["functional-only", "with-quality-review"]
)
@pytest.mark.parametrize("when", ["memory-yield", "certified-replay"])
async def test_mandatory_functional_log_is_bound_to_first_delivery_receipt(
    flow, tmp_path, with_quality, when
):
    observe_agents(flow)
    task, _, _ = await implementation(flow)
    functional = await reviewer(
        flow,
        task,
        outcome="passed",
        role="functional_tests",
        complete_parent=not with_quality,
    )
    if with_quality:
        await reviewer(flow, task, outcome="security")
    stored_functional = flow.service.managed_service.store.get(functional["id"])
    proof = Path(stored_functional["report"]["checks"][0]["proof"])
    original_bytes = proof.read_bytes()
    original_digest = hashlib.sha256(original_bytes).hexdigest()
    facade = flow.service.managed_service
    factory = facade.memory
    if when == "memory-yield":

        def memory():
            adapter = factory()
            finish = adapter.finish

            async def changed(*args, **kwargs):
                cycle = await finish(*args, **kwargs)
                proof.write_bytes(b"edited delegated functional evidence\n")
                return cycle

            adapter.finish = changed
            return adapter

        facade.memory = memory
        blocked = await flow.invoke("resume", task_id=task["id"])
        assert blocked["blocked_reason"] == "mandatory_functional_evidence_changed", (
            blocked
        )
        assert blocked["memory"]["write_receipt"]["state"] == "confirmed"
        assert blocked["admission"]["state"] == "executing"
        receipt = blocked["memory"]["write_receipt"]
        first = facade.store.get(task["id"])["delivery_review"][
            "mandatory_validations"
        ][0]
        assert first["checks"][0]["sha256"] == original_digest
        facade.memory = factory
        proof.write_bytes(original_bytes)
        delivered = await flow.invoke("resume", task_id=task["id"])
        assert delivered["phase"] == "pr_pending", delivered
        assert delivered["memory"]["write_receipt"] == receipt
    else:
        delivered = await flow.invoke("resume", task_id=task["id"])
        assert delivered["phase"] == "pr_pending", delivered
        receipt = delivered["memory"]["write_receipt"]
        proof.write_bytes(b"changed after implementation certification\n")
        code, blocked, _, stderr = await run_cli_fifo(
            flow,
            tmp_path,
            "resume",
            dict(task_id=task["id"]),
            pad_hex=14000,
            minimum=8192,
        )
        assert code == 1, (blocked, stderr)
        assert blocked["blocked_reason"] == "mandatory_functional_evidence_changed"
        assert blocked["memory"]["write_receipt"] == receipt
        assert blocked["admission"]["state"] == "not_reserved"
        proof.write_bytes(original_bytes)
        delivered = await flow.invoke("resume", task_id=task["id"])
        assert delivered["phase"] == "pr_pending", delivered
        assert delivered["memory"]["write_receipt"] == receipt
    exact = flow.snapshot(task)
    replay = await flow.invoke("resume", task_id=task["id"])
    assert replay["phase"] == "pr_pending"
    assert flow.snapshot(task) == exact
    assert facade.store.get(functional["id"]) == stored_functional
    assert delivered["admission"]["state"] == "not_reserved"


async def test_changed_delegated_functional_log_blocks_publication_before_host_effect(
    flow, monkeypatch
):
    from test_managed_tasks import GitHubHost, initialize
    from test_publication import specification

    from oraculo.publication import GitHubPublisher

    observe_agents(flow)
    task, evidence, _ = await implementation(flow)
    functional = await reviewer(flow, task, outcome="passed", role="functional_tests")
    delivered = await flow.invoke("resume", task_id=task["id"])
    assert delivered["phase"] == "pr_pending", delivered
    receipt = delivered["memory"]["write_receipt"]
    functional_report = flow.service.managed_service.store.get(functional["id"])[
        "report"
    ]
    proof = Path(functional_report["checks"][0]["proof"])
    proof.write_text("tampered delegated functional test evidence")
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
        source_commit=evidence["commit"],
        task_id=task["id"],
    )
    github = GitHubHost(spec)
    monkeypatch.setattr(
        "oraculo.publication.RepositoryPublisher", lambda: GitHubPublisher(github)
    )
    blocked = await flow.invoke("publish", task_id=task["id"], publication=spec)
    assert blocked["blocked_reason"] == "mandatory_functional_evidence_changed", blocked
    assert github.created == 0
    assert blocked["memory"]["write_receipt"] == receipt
    assert blocked["publication"] is None
    assert (
        flow.service.managed_service.store.get(functional["id"])["report"]
        == functional_report
    )
