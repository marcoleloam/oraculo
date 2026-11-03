"""Real service/socket/Git/memory/files and test execution; synthetic host only."""

import copy
import hashlib
import json
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from test_managed_tasks import git, initialize, report
from test_origin_closure import sibling
from test_report_prevalidation import flow as flow_fixture

from oraculo.protocol import canonical_json, content_hash
from oraculo.review_adjudication import coverage, descendant_binding, read_private
from oraculo.state import StateError

flow = flow_fixture


def artifact(path, value):
    raw = canonical_json(value).encode()
    path.write_bytes(raw)
    return dict(path=str(path), sha256=hashlib.sha256(raw).hexdigest())


def message(source, text):
    return dict(
        itemId=source["item_id"],
        messageId=source["message_id"],
        sourceThreadId=source["thread_id"],
        visibility="local",
        type="user_message",
        createdBy="user",
        creationSource="web",
        textTruncated=False,
        text=text,
    )


async def interrupted(flow):
    task = await flow.begin()
    cwd = Path(task["child_workspace"]["path"])
    (cwd / "example.py").write_text("def answer():\n    return 42\n")
    git(cwd, "add", "example.py")
    git(
        cwd,
        "-c",
        "user.name=Example",
        "-c",
        "user.email=example@example.org",
        "commit",
        "-m",
        "Example change",
    )
    proof = cwd.parent / "executed-check.log"
    args = [
        sys.executable,
        "-B",
        "-c",
        "from example import answer; assert answer() == 42; print('1 passed')",
    ]
    with proof.open("w") as out:
        subprocess.run(args, cwd=cwd, stdout=out, stderr=subprocess.STDOUT, check=True)
    evidence = report(task)
    evidence["checks"] = [
        dict(
            command=" ".join(args),
            status="passed",
            result="1 passed; exit 0",
            proof=str(proof),
        )
    ]
    child = task["child"]["childThreadId"]
    flow.host.caller = child
    submitted = await flow.invoke("finish", task_id=task["id"], report=evidence)
    assert submitted["phase"] == "validation_submitted", submitted
    flow.host.complete(child)
    reviewer = "failed-review"
    flow.host.threads[reviewer] = dict(
        threadId=reviewer,
        projectId="alpha",
        parentThreadId=child,
        status="failed",
        activeRunId=None,
        latestRunId="original-review-run",
        title="Interrupted review",
        providerMessage="provider declined review",
    )
    flow.host.caller = "origin"
    return task, evidence, proof


async def human_evidence(flow, task, evidence, *, dispositions=None):
    response = await flow.invoke("diagnose-review", task_id=task["id"])
    diagnostic = response["review_diagnosis"]
    assert diagnostic["reason"] == "terminal_descendant_review_required"
    flow.host.threads["human-review"] = dict(
        flow.host.threads["origin"],
        threadId="human-review",
        parentThreadId=None,
    )
    stored = flow.service.managed_service.store.get(task["id"])
    doc = dict(
        diagnostic["binding"],
        kind="human_review_v1",
        verdict="PASS",
        descendants=descendant_binding(diagnostic["snapshot"]),
        scope_assessment="Reviewed the complete request and verified the changed function behavior.",
        coverage=[
            dict(
                r,
                assessment="Inspected function and assertion against expected result.",
            )
            for r in coverage(stored, evidence["commit"])
        ],
        findings=[],
        operational_dispositions=dispositions or [],
        reviewer=dict(
            name="Synthetic human reviewer",
            independence_attestation="I did not implement this change and reviewed it independently.",
        ),
        checks=[
            dict(c, sha256=hashlib.sha256(Path(c["proof"]).read_bytes()).hexdigest())
            for c in evidence["checks"]
        ],
    )
    path = Path(task["child_workspace"]["path"]).parent / "human-review.json"
    intent = dict(
        task_id=task["id"],
        reason="Human review after interrupted descendant",
        review=artifact(path, doc),
    )
    bindings = dict(diagnostic["binding"], review_sha256=intent["review"]["sha256"])
    artifact_bytes = path.read_bytes()
    messages = {}

    def send_human(role, thread_id, text):
        # A synthetic host allocates IDs AFTER immutable message text is sent.
        # Neither caller nor artifact can know these IDs before sending.
        source = dict(
            thread_id=thread_id, item_id=uuid.uuid4().hex, message_id=uuid.uuid4().hex
        )
        messages[role] = message(source, text)
        return source

    review_source = send_human(
        "human-item",
        "human-review",
        "ORACULO_HUMAN_REVIEW_V1\n" + canonical_json(bindings),
    )
    approval_source = send_human(
        "approval-item",
        "origin",
        "ORACULO_REVIEW_APPROVAL_V1\n"
        + canonical_json(
            dict(
                bindings,
                review_message_id=review_source["message_id"],
                review_thread_id=review_source["thread_id"],
            )
        ),
    )
    intent.update(review_source=review_source, approval=approval_source)
    assert path.read_bytes() == artifact_bytes  # no circular hash/host-ID dependency
    original = flow.host.call

    async def call(name, args):
        result = await original(name, args)
        if name == "t3_thread_read":
            match = [m for m in messages.values() if m["itemId"] == args.get("itemId")]
            if match:
                result["items"] = copy.deepcopy(match)
        return result

    flow.host.call = call
    return intent, doc, messages


async def operational_failure(flow, task, *, technical=False, functional=False):
    """Create the original failed review through the actual managed lifecycle."""
    flow.host.threads.pop("failed-review")
    executor = task["child"]["childThreadId"]
    flow.host.caller = executor
    flow.host.threads[executor].update(
        status="running", activeRunId=task["child"]["childRunId"]
    )
    flow.service.state.max_executors = 3
    review = await flow.invoke(
        "start",
        kind="work",
        key="interrupted-review",
        text="Review the implementation security scope",
        cwd=task["child_workspace"]["path"],
    )
    assert review["phase"] == "executing", review
    flow.host.complete(review["child"]["childThreadId"], status="failed")
    host_status = await flow.host.call(
        "task_status", {"taskId": review["child"]["taskId"]}
    )
    proof = Path(task["child_workspace"]["path"]).parent / "operational-failure.log"
    proof.write_text(canonical_json(host_status))
    negative = report(review)
    negative["summary"] = (
        "Independent review interrupted before a final technical verdict"
    )
    negative["checks"] = [
        dict(
            command="T3 task_status of independent review",
            status="failed",
            layer="integration" if functional else "host",
            result="Original review run terminal failed; provider declined review",
            proof=str(proof),
        )
    ]
    if technical:
        negative["verdict"] = "FAIL"
    saved = await flow.invoke("finish", task_id=review["id"], report=negative)
    assert saved["blocked_reason"] == "work_not_successful", saved
    assert saved["memory"]["write_receipt"]["state"] == "confirmed"
    flow.host.complete(executor)
    flow.host.caller = "origin"
    store = flow.service.managed_service.store
    original = store.get(review["id"])
    cycle = flow.service.memory_cycles.get(review["memory"]["id"])
    disposition = dict(
        task_id=review["id"],
        thread_id=review["child"]["childThreadId"],
        child_run_id=review["child"]["childRunId"],
        request_sha256=content_hash(original["request"]),
        report_sha256=content_hash(negative),
        memory_cycle_id=cycle["id"],
        memory_report_sha256=cycle["report_sha256"],
        memory_receipt_sha256=content_hash(cycle["write_receipt"]),
        classification="operational_review_interruption_without_verdict",
        rationale="Inspected original host receipt and report. It records provider interruption, not a technical verdict; performed final human review separately.",
        failed_checks=[
            dict(
                index=0,
                check_sha256=content_hash(negative["checks"][0]),
                proof=dict(
                    path=str(proof),
                    sha256=hashlib.sha256(proof.read_bytes()).hexdigest(),
                ),
                assessment="Confirmed original failed run identity and absence of a final review verdict.",
            )
        ],
    )
    return review, disposition


async def test_human_disposition_preserves_original_operational_failure_and_memory(
    flow,
):
    task, evidence, _ = await interrupted(flow)
    review, disposition = await operational_failure(flow, task)
    store = flow.service.managed_service.store
    before_review = store.get(review["id"])
    before_memory = copy.deepcopy(
        flow.service.memory_cycles.get(review["memory"]["id"])
    )
    intent, _, _ = await human_evidence(
        flow, task, evidence, dispositions=[disposition]
    )
    result = await flow.invoke("adjudicate-review", **intent)
    assert result["phase"] == "pr_pending", result
    assert result["work_status"] == "succeeded"
    assert result["memory"]["write_receipt"]["state"] == "confirmed"
    assert flow.service.state.capacity()["occupied"] == 0
    assert store.get(review["id"]) == before_review
    assert flow.service.memory_cycles.get(review["memory"]["id"]) == before_memory
    assert before_review["work_status"] == "failed"
    assert flow.host.threads[review["child"]["childThreadId"]]["status"] == "failed"
    after = flow.snapshot(task)
    replay = await flow.invoke("adjudicate-review", **intent)
    assert replay["phase"] == "pr_pending", replay
    assert flow.snapshot(task) == after


@pytest.mark.parametrize(
    "defect",
    [
        "missing",
        "classification",
        "report",
        "run",
        "scope",
        "receipt",
        "check_digest",
        "check_proof",
        "no_assessment",
        "technical_fail",
        "functional_fail",
    ],
)
async def test_host_layer_alone_cannot_waive_an_original_negative_report(flow, defect):
    task, evidence, _ = await interrupted(flow)
    review, disposition = await operational_failure(
        flow,
        task,
        technical=defect == "technical_fail",
        functional=defect == "functional_fail",
    )
    if defect in {"classification", "report", "run", "scope", "receipt"}:
        key = {
            "classification": "classification",
            "report": "report_sha256",
            "run": "child_run_id",
            "scope": "request_sha256",
            "receipt": "memory_receipt_sha256",
        }[defect]
        disposition[key] = "unrelated"
    elif defect == "check_digest":
        disposition["failed_checks"][0]["check_sha256"] = "0" * 64
    elif defect == "check_proof":
        disposition["failed_checks"][0]["proof"]["sha256"] = "0" * 64
    elif defect == "no_assessment":
        disposition["failed_checks"][0]["assessment"] = ""
    intent, _, _ = await human_evidence(
        flow, task, evidence, dispositions=[] if defect == "missing" else [disposition]
    )
    before = flow.snapshot(task)
    original_review = flow.service.managed_service.store.get(review["id"])
    result = await flow.invoke("adjudicate-review", **intent)
    assert result["phase"] == "blocked", result
    assert result["rejection"]["persisted"] is False
    assert flow.snapshot(task) == before
    assert flow.service.managed_service.store.get(review["id"]) == original_review
    assert result["memory"]["write_state"] == "not_requested"


async def test_terminal_failure_has_precise_diagnosis_and_does_not_auto_pass(flow):
    task, evidence, _ = await interrupted(flow)
    before = flow.snapshot(task)
    result = await flow.invoke("diagnose-review", task_id=task["id"])
    assert flow.snapshot(task) == before  # diagnosis is read only
    snapshot = result["review_diagnosis"]["snapshot"]
    assert snapshot["childrenTerminal"] is True
    assert snapshot["childrenSuccessful"] is False
    assert snapshot["hasPendingChildRuns"] is False
    result = await flow.invoke("resume", task_id=task["id"])
    assert result["blocked_reason"] == "terminal_descendant_review_required", result
    assert result["memory"]["write_state"] == "not_requested"
    assert result["work_status"] == "pending"
    assert flow.service.state.capacity()["occupied"] == 1
    assert flow.snapshot(task)["task"]["submitted_report"] == evidence


async def test_human_review_closes_same_task_with_immutable_history_and_replay(flow):
    task, evidence, _ = await interrupted(flow)
    other = await sibling(flow)
    intent, doc, _ = await human_evidence(flow, task, evidence)
    before = flow.snapshot(task)
    calls = len(flow.host.calls)
    result = await flow.invoke("adjudicate-review", **intent)
    assert result["phase"] == "pr_pending", result
    assert result["memory"]["write_receipt"]["state"] == "confirmed"
    assert result["memory"]["write_receipt"]["commit"]
    assert result["work_status"] == "succeeded"
    assert result["publication"] is None
    assert flow.service.state.capacity()["occupied"] == 1
    stored = flow.snapshot(task)["task"]
    assert stored["id"] == task["id"] and stored["child"] == task["child"]
    assert stored["submitted_report"] == stored["report"] == evidence
    assert stored["events"][: len(before["task"]["events"])] == before["task"]["events"]
    audit = stored["review_adjudication"]
    assert audit["document"] == doc
    assert audit["terminal_snapshot"]["childrenSuccessful"] is False
    assert flow.host.threads["failed-review"]["status"] == "failed"
    assert (
        flow.host.threads["failed-review"]["providerMessage"]
        == "provider declined review"
    )
    assert flow.host.threads[other["child"]["childThreadId"]]["status"] == "running"
    assert not any(
        n in {"delegate_task", "t3_thread_launch", "t3_thread_send"}
        and a.get("threadId") != "origin"
        for n, a in flow.host.calls[calls:]
    )
    after = flow.snapshot(task)
    for action, payload in (
        ("adjudicate-review", intent),
        ("resume", dict(task_id=task["id"])),
        ("finish", dict(task_id=task["id"], report=evidence)),
    ):
        replay = await flow.invoke(action, **payload)
        assert replay["phase"] == "pr_pending", replay
        assert flow.snapshot(task) == after


@pytest.mark.parametrize(
    "defect",
    [
        "active",
        "queued",
        "unknown",
        "incomplete_chain",
        "generic_approval",
        "approval_only",
        "scope",
        "head",
        "report_hash",
        "artifact_hash",
        "coverage",
        "review_agent",
        "approval_agent",
        "review_mcp",
        "approval_mcp",
        "truncated",
        "review_same_actor",
        "approval_wrong_thread",
        "approval_wrong_message",
        "review_wrong_project",
        "missing_test_log",
        "changed_test_log",
        "unresolved_finding",
        "negative_report",
        "negative_check",
        "child_actor",
        "negative_descendant_report",
    ],
)
async def test_no_self_declared_approval_or_invalid_evidence_can_certify(flow, defect):
    task, evidence, proof = await interrupted(flow)
    intent, doc, messages = await human_evidence(flow, task, evidence)
    store = flow.service.managed_service.store
    if defect in {"active", "queued", "unknown"}:
        flow.host.threads["failed-review"].update(
            status={"active": "running", "queued": "queued", "unknown": "unknown"}[
                defect
            ]
        )
        if defect == "active":
            flow.host.threads["failed-review"]["activeRunId"] = "still-working"
    elif defect == "incomplete_chain":
        flow.host.threads["failed-review"]["runCount"] = 2
    elif defect == "generic_approval":
        messages["approval-item"]["text"] = (
            "Authorized to implement and resolve the blocker"
        )
    elif defect == "approval_only":
        doc = dict(kind="approval", human=True, approved=True)
    elif defect in {"scope", "head", "report_hash"}:
        doc[
            {
                "scope": "request_sha256",
                "head": "commit",
                "report_hash": "report_sha256",
            }[defect]
        ] = "0" * 40
    elif defect == "artifact_hash":
        intent["review"]["sha256"] = "0" * 64
    elif defect == "coverage":
        doc["coverage"] = []
    elif defect in {"review_agent", "approval_agent"}:
        messages["human-item" if defect == "review_agent" else "approval-item"][
            "createdBy"
        ] = "agent"
    elif defect in {"review_mcp", "approval_mcp"}:
        messages["human-item" if defect == "review_mcp" else "approval-item"][
            "creationSource"
        ] = "mcp"
    elif defect == "truncated":
        messages["human-item"]["textTruncated"] = True
    elif defect == "review_same_actor":
        intent["review_source"]["thread_id"] = task["child"]["childThreadId"]
    elif defect == "approval_wrong_thread":
        intent["approval"]["thread_id"] = "human-review"
    elif defect == "approval_wrong_message":
        messages["approval-item"]["messageId"] = "different-human-message"
    elif defect == "review_wrong_project":
        flow.host.threads["human-review"]["projectId"] = "unrelated"
    elif defect == "missing_test_log":
        proof.unlink()
    elif defect == "changed_test_log":
        proof.write_text("failed")
    elif defect == "unresolved_finding":
        doc["findings"] = [dict(finding="Regression", resolution="", status="open")]
    elif defect in {"negative_report", "negative_check"}:
        original = copy.deepcopy(evidence)
        if defect == "negative_report":
            original["verdict"] = "FAIL"
        else:
            original["checks"][0]["status"] = "failed"
        store.update(
            task["id"],
            "validation_submitted",
            {"negative": True},
            submitted_report=original,
        )
    elif defect == "child_actor":
        flow.host.caller = task["child"]["childThreadId"]
        flow.host.threads[flow.host.caller]["activeRunId"] = task["child"]["childRunId"]
    elif defect == "negative_descendant_report":
        other = store.create(
            dict(
                thread_id=task["child"]["childThreadId"], project_id="alpha", run_id="r"
            ),
            "negative",
            {},
        )
        store.update(
            other["id"],
            "blocked",
            {"negative": True},
            child=dict(childThreadId="failed-review"),
            report=dict(evidence, verdict="FAIL"),
        )
    if defect != "artifact_hash":
        intent["review"] = artifact(Path(intent["review"]["path"]), doc)
    before = flow.snapshot(task)
    result = await flow.invoke("adjudicate-review", **intent)
    assert result["phase"] == "blocked", result
    assert flow.snapshot(task) == before  # invalid evidence cannot persist an audit
    assert result["rejection"]["persisted"] is False
    assert result["memory"]["write_state"] == "not_requested"
    assert flow.service.state.capacity()["occupied"] == 1


@pytest.mark.parametrize(
    "change", ["active", "new_run", "new_child", "log", "approval", "dirty"]
)
async def test_revalidate_after_memory_and_release_only_after_fresh_proof(flow, change):
    task, evidence, proof = await interrupted(flow)
    intent, _, messages = await human_evidence(flow, task, evidence)
    facade = flow.service.managed_service
    factory = facade.memory
    saved_threads = copy.deepcopy(flow.host.threads)
    original_log = proof.read_bytes()
    original_approval = copy.deepcopy(messages["approval-item"])
    dirty = Path(task["child_workspace"]["path"]) / "unfinished"

    def memory():
        adapter = factory()
        finish = adapter.finish

        async def changed(*args, **kwargs):
            cycle = await finish(*args, **kwargs)
            if change == "active":
                flow.host.threads["failed-review"]["activeRunId"] = "later"
            elif change == "new_run":
                flow.host.threads["failed-review"].update(
                    latestRunId="later", runCount=2
                )
            elif change == "new_child":
                flow.host.threads["extra"] = dict(
                    flow.host.threads["failed-review"], threadId="extra"
                )
            elif change == "log":
                proof.write_text("changed")
            elif change == "approval":
                messages["approval-item"]["text"] = "withdrawn"
            else:
                dirty.write_text("unfinished")
            return cycle

        adapter.finish = changed
        return adapter

    facade.memory = memory
    result = await flow.invoke("adjudicate-review", **intent)
    assert result["phase"] == "blocked", result
    assert result["memory"]["write_receipt"]["state"] == "confirmed"
    receipt = result["memory"]["write_receipt"]
    assert flow.service.state.capacity()["occupied"] == 1
    flow.host.threads = saved_threads
    proof.write_bytes(original_log)
    messages["approval-item"] = original_approval
    dirty.unlink(missing_ok=True)
    facade.memory = factory
    result = await flow.invoke("adjudicate-review", **intent)
    assert result["phase"] == "pr_pending", result
    assert result["memory"]["write_receipt"] == receipt
    assert flow.service.state.capacity()["occupied"] == 0


@pytest.mark.parametrize("action", ["diagnose-review", "adjudicate-review"])
def test_cli_requires_service_capability(
    service_config, tmp_path, monkeypatch, capsys, action
):
    from oraculo.cli import main

    _, config = service_config
    path = tmp_path / "request.json"
    path.write_text(json.dumps(dict(task_id="existing")))

    async def capabilities(*args):
        return {"ok": True, "data": {"protocol_version": 1}}

    monkeypatch.setattr("oraculo.cli.request", capabilities)
    assert main(["--config", str(config), "task", action, "--file", str(path)]) == 1
    assert "review_adjudication_service_upgrade_required" in capsys.readouterr().err


@pytest.mark.parametrize(
    "first", [" leading.txt", "\tleading.txt", "\nleading.txt", "\r\nleading.txt"]
)
def test_exact_diff_paths_blobs_deletions_and_renames(tmp_path, first):
    cwd = tmp_path / "repo"
    initialize(cwd)
    for name in ("old.txt", "deleted.txt"):
        (cwd / name).write_text("base\n")
    git(cwd, "add", ".")
    git(
        cwd,
        "-c",
        "user.name=Example",
        "-c",
        "user.email=example@example.org",
        "commit",
        "-m",
        "Base files",
    )
    base = git(cwd, "rev-parse", "HEAD")
    (cwd / "deleted.txt").unlink()
    (cwd / "old.txt").rename(cwd / "renamed.txt")
    for name in (first, ":literal[1].txt"):
        (cwd / name).write_text("added\n")
    git(cwd, "add", "--all")
    git(
        cwd,
        "-c",
        "user.name=Example",
        "-c",
        "user.email=example@example.org",
        "commit",
        "-m",
        "Whitespace, rename and deletion",
    )
    head = git(cwd, "rev-parse", "HEAD")
    task = dict(base_sha=base, child_workspace=dict(path=str(cwd)))
    actual = coverage(task, head)
    assert [r["path"] for r in actual] == sorted(
        [first, ":literal[1].txt", "old.txt", "deleted.txt", "renamed.txt"]
    )
    for row in actual:
        if row["path"] in {"old.txt", "deleted.txt"}:
            assert row["blob_oid"] is None
        else:
            assert row["blob_oid"] == git(cwd, "rev-parse", f"{head}:{row['path']}")


@pytest.mark.parametrize("root", ["project", "child", "parent", "parent_symlink"])
def test_private_evidence_excludes_all_bound_checkouts(tmp_path, root):
    paths = {name: tmp_path / name for name in ("project", "child", "parent")}
    for path in paths.values():
        path.mkdir()
    task = dict(
        project=dict(project_path=str(paths["project"])),
        child_workspace=dict(path=str(paths["child"])),
        parent_workspace=dict(path=str(paths["parent"])),
    )
    path = paths[root if root != "parent_symlink" else "parent"] / "proof.json"
    ref = artifact(path, dict(private=True))
    if root == "parent_symlink":
        link = tmp_path / "outside-link"
        link.symlink_to(path)
        ref["path"] = str(link)
    with pytest.raises(StateError, match="review_evidence_must_be_private"):
        read_private(ref, task, limit=1024)


@pytest.mark.parametrize("invalidate", [False, True])
async def test_adjudicated_publication_merge_and_settlement_revalidate_human_evidence(
    flow, monkeypatch, invalidate
):
    from test_managed_tasks import GitHubHost
    from test_publication import specification
    from test_task_closure import Lifecycle

    from oraculo.publication import GitHubPublisher

    task, evidence, _ = await interrupted(flow)
    review, disposition = await operational_failure(flow, task)
    original_review = flow.service.managed_service.store.get(review["id"])
    intent, _, _ = await human_evidence(
        flow, task, evidence, dispositions=[disposition]
    )
    accepted = await flow.invoke("adjudicate-review", **intent)
    assert accepted["phase"] == "pr_pending", accepted
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
    assert published["publication_memory"]["write_receipt"]["state"] == "confirmed"
    assert github.created == 1
    if invalidate:
        Path(disposition["failed_checks"][0]["proof"]["path"]).write_text(
            "changed operational proof"
        )
    merged = await flow.invoke(
        "merge",
        task_id=task["id"],
        policy=dict(
            repository=spec["repository"],
            base=spec["base"],
            head_sha=spec["head_sha"],
            merge_authorized=True,
            required_checks=["unit"],
            required_review=True,
            method="squash",
            policy_source="Synthetic explicit origin authorization",
        ),
    )
    if invalidate:
        assert merged["blocked_reason"] == "review_artifact_digest_mismatch", merged
        assert lifecycle.calls == 0
        assert not merged["merged"] and not merged["settled"]
    else:
        assert merged["phase"] == "settled", merged
        assert merged["merged"] and merged["settled"]
        assert merged["final_memory"]["write_receipt"]["state"] == "confirmed"
        assert lifecycle.calls == 1
    assert flow.service.managed_service.store.get(review["id"]) == original_review
    assert flow.host.threads[review["child"]["childThreadId"]]["status"] == "failed"
