"""Canonical CI recovery through API/CLI, real daemon/Git/memory, synthetic gh.

The existing authenticated repository observer validates transport fixtures. No
remote repository, provider, customer checkout or deployed runtime is contacted.
"""

import copy
import hashlib
import html
import json
import subprocess
import sys

import pytest
from test_ci_host import GitHubCIHost
from test_managed_tasks import GitHubHost, git, initialize
from test_publication import specification
from test_report_prevalidation import flow as flow_fixture
from test_review_product_acceptance import implementation, observe_agents
from test_stdio_fifo import run_cli_fifo

from oraculo.pr_lifecycle import RepositoryPRLifecycle
from oraculo.publication import GitHubPublisher
from oraculo.task_transport import SocketMemory

flow = flow_fixture


class CITransport(GitHubCIHost):
    """Adapt the existing full gh fixture to the publication fixture's identity."""

    def __init__(self, head):
        super().__init__(head)
        self.required = ["Product review (exact HEAD)"]
        self.require_review = False
        self.conclusion = "failure"

    def __call__(self, argv, cwd):
        if argv[0] != "gh":
            return super().__call__(argv, cwd)
        translated = [
            a.replace("example/public-project", "example/repo")
            .replace("/pulls/12", "/pulls/1")
            .replace("/pull/12", "/pull/1")
            .replace(f"/attempts/{self.attempt}/", "/attempts/1/")
            for a in argv
        ]
        data = json.loads(super().__call__(translated, cwd))

        def replace(value, key=None):
            if isinstance(value, dict):
                return {k: replace(v, k) for k, v in value.items()}
            if isinstance(value, list):
                return [replace(v) for v in value]
            if isinstance(value, str):
                value = value.replace("example/repo", "example/public-project").replace(
                    "/pull/1", "/pull/12"
                )
                if key == "conclusion" and self.conclusion != "success":
                    return (
                        self.conclusion.upper() if value.isupper() else self.conclusion
                    )
                return value
            return 12 if key == "number" and value == 1 else value

        return json.dumps(replace(data))


async def publish(flow, monkeypatch, task, evidence, name, *, number=12):
    public = flow.service.config.projects_root / name
    initialize(public)
    # A real, different public HEAD for each implementation candidate.
    (public / "candidate.txt").write_text(evidence["commit"] + "\n")
    git(public, "add", ".")
    git(
        public,
        "-c",
        "user.name=Example",
        "-c",
        "user.email=example@example.org",
        "commit",
        "-m",
        "Candidate",
    )
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

    def publisher_transport(argv, cwd):
        raw = github(argv, cwd)
        if number == 12 or argv[0] != "gh":
            return raw
        if raw.startswith("https://"):
            return raw.replace("/pull/12", f"/pull/{number}")

        def translate(value, key=None):
            if isinstance(value, dict):
                return {k: translate(v, k) for k, v in value.items()}
            if isinstance(value, list):
                return [translate(v) for v in value]
            if isinstance(value, str):
                return value.replace("/pull/12", f"/pull/{number}")
            return number if key == "number" and value == 12 else value

        return json.dumps(translate(json.loads(raw)))

    monkeypatch.setattr(
        "oraculo.publication.RepositoryPublisher",
        lambda: GitHubPublisher(publisher_transport),
    )
    result = await flow.invoke("publish", task_id=task["id"], publication=spec)
    assert result["phase"] == "pr_open", result
    transport = CITransport(spec["head_sha"])
    lifecycle = RepositoryPRLifecycle(transport)
    monkeypatch.setattr("oraculo.pr_lifecycle.RepositoryPRLifecycle", lambda: lifecycle)
    policy = dict(
        repository=spec["repository"],
        base="main",
        head_sha=spec["head_sha"],
        merge_authorized=True,
        required_checks=transport.required,
        required_review=False,
        method="squash",
        policy_source="Synthetic observed remote policy; explicit origin authority",
    )
    return result, policy, transport


async def setup(flow, monkeypatch):
    observe_agents(flow)
    original = flow.host.call

    async def host(name, args):
        if name in {"watch_pull_request", "unwatch_pull_request"}:
            return dict(url=args["url"], watching=name == "watch_pull_request")
        return await original(name, args)

    flow.host.call = host
    task, evidence, _ = await implementation(flow)
    flow.host.complete(task["child"]["childThreadId"])
    flow.host.caller = "origin"
    delivered = await flow.invoke("resume", task_id=task["id"])
    assert delivered["work_status"] == "succeeded", delivered
    return (
        task,
        evidence,
        await publish(flow, monkeypatch, task, evidence, "public-first"),
    )


@pytest.mark.parametrize("entrypoint", ["api", "cli"])
async def test_ci_memory_loop_preserves_failure_replay_correction_and_new_conversation(
    flow, monkeypatch, tmp_path, entrypoint
):  # noqa: F811
    task, evidence, (delivered, policy, transport) = await setup(flow, monkeypatch)
    store = flow.service.managed_service.store
    implementation_before = copy.deepcopy(store.get(task["id"]))

    async def check(task_id, policy=None):
        payload = dict(task_id=task_id, **({"policy": policy} if policy else {}))
        if entrypoint == "api":
            return await flow.invoke("pr-check", **payload)
        code, result, _, stderr = await run_cli_fifo(
            flow, tmp_path, "pr-check", payload, pad_hex=14000, minimum=8192
        )
        assert code == 0, (result, stderr)
        return result

    first = await check(task["id"], policy)
    assert first["phase"] == "pr_waiting", first
    assert first["work_status"] == "succeeded" and not first["merged"]
    round1 = store.get(task["id"])["ci_rounds"][0]
    assert round1["work_status"] == "failed"
    assert round1["memory"]["write_receipt"]["state"] == "confirmed"
    assert round1["facts"]["identity"]["observed_runs"] == [{"run": "42", "attempt": 1}]
    assert round1["report"]["evidence"]["previous"] is None
    before = flow.service.publisher.git.head()
    cycles = len(flow.service.memory_cycles.list())
    await check(task["id"])
    assert len(flow.service.memory_cycles.list()) == cycles
    assert flow.service.publisher.git.head() == before
    assert store.get(task["id"])["ci_rounds"] == [round1]
    for k in ("report", "submitted_report", "memory", "work_status", "child"):
        assert store.get(task["id"])[k] == implementation_before[k]

    # Read from a fresh conversation, then make an observable bounded decision
    # using only recovered anchors, not a direct ledger lookup or executor prose.
    recalled = await SocketMemory(flow.service.config).begin(
        "alpha",
        dict(thread_id="new-conversation", run_id="new-run"),
        "recover-negative-ci",
        "CI failure diagnosis correction evaluation history",
    )
    anchors = [
        json.loads(html.unescape(line.removeprefix("CI loop anchor: ")))
        for doc in recalled["context"]["context"]["bundle"]["documents"]
        for line in doc["content"].splitlines()
        if line.startswith("CI loop anchor: ")
    ]
    failures = [
        a
        for a in anchors
        if a["work_status"] == "failed"
        and a["head"] == delivered["publication"]["head_sha"]
    ]
    assert len(failures) == 1
    recovered_failure = failures[0]
    decision = dict(
        action="diagnose_before_authorized_correction",
        task_id=recovered_failure["task_id"],
        round_id=recovered_failure["round_id"],
        context_sha256=recalled["artifact"],
    )
    assert decision["round_id"] == round1["id"]

    # A real diagnosis command/artifact, then a new authorized implementation.
    diagnosis_log = tmp_path / "diagnosis.log"
    with diagnosis_log.open("w") as stream:
        diagnosis_run = subprocess.run(
            [
                sys.executable,
                "-c",
                "from pathlib import Path; assert not Path('test_boundary.py').exists(); print('Missing boundary coverage in the synthetic candidate')",
            ],
            cwd=task["child_workspace"]["path"],
            stdout=stream,
            stderr=subprocess.STDOUT,
        )
    assert diagnosis_run.returncode == 0
    reference = {k: decision[k] for k in ("task_id", "round_id")}
    diagnosis = dict(
        summary="Missing boundary coverage in the synthetic candidate",
        proof=dict(
            path=str(diagnosis_log),
            sha256=hashlib.sha256(diagnosis_log.read_bytes()).hexdigest(),
        ),
    )
    corrected, correction, _ = await implementation(
        flow,
        "corrected",
        evidence=dict(
            ci_predecessor=reference,
            ci_diagnosis=diagnosis,
            ci_memory_decision=decision,
        ),
        extra_files={
            "test_boundary.py": "from example import answer\nassert answer() == 42\n"
        },
        script="from pathlib import Path; exec(Path('test_boundary.py').read_text()); print('boundary assertion passed')",
    )
    flow.host.complete(corrected["child"]["childThreadId"])
    flow.host.caller = "origin"
    saved = await flow.invoke("resume", task_id=corrected["id"])
    assert saved["work_status"] == "succeeded", saved
    _, next_policy, next_transport = await publish(
        flow, monkeypatch, corrected, correction, "public-corrected"
    )
    next_transport.conclusion = "success"
    result = await check(corrected["id"], next_policy)
    assert result["phase"] == "pr_ready", result
    assert not result["merged"]
    round2 = store.get(corrected["id"])["ci_rounds"][0]
    previous = round2["report"]["evidence"]["previous"]
    assert previous["round_id"] == round1["id"]
    assert previous["correction_commit"] == correction["commit"] != evidence["commit"]
    assert previous["diagnosis"] == diagnosis
    assert round2["memory"]["write_receipt"]["state"] == "confirmed"
    assert store.get(task["id"])["ci_rounds"] == [round1]
    # New actor/conversation uses the official read, not the managed ledger.
    recovered = await SocketMemory(flow.service.config).begin(
        "alpha",
        dict(thread_id="new-conversation", run_id="new-run"),
        "new-ci-investigation",
        "CI failure diagnosis correction evaluation history",
    )
    raw = json.dumps(recovered["context"])
    assert round1["id"] in raw and round2["id"] in raw
    assert "failed" in raw and correction["commit"] in raw
    assert round1["memory"]["id"] != round2["memory"]["id"]
    assert len(flow.host.created) == 2  # no CI redispatch or duplicate implementation


@pytest.mark.parametrize("fault", ["read", "write", "lost_begin", "lost_finish"])
async def test_ci_memory_loop_recovers_same_cycle_after_transient_or_uncertain_io(
    flow, monkeypatch, fault
):  # noqa: F811
    task, _, (_, policy, transport) = await setup(flow, monkeypatch)
    facade = flow.service.managed_service
    factory = facade.memory
    seen = []

    def memory():
        adapter = factory()
        method = adapter.begin if fault in {"read", "lost_begin"} else adapter.finish

        async def fail(*args, **kwargs):
            if fault.startswith("lost"):
                result = await method(*args, **kwargs)
                seen.append(
                    result["cycle"]["id"] if "cycle" in result else result["id"]
                )
            raise TimeoutError("synthetic lost response")

        if fault in {"read", "lost_begin"}:
            adapter.begin = fail
        else:
            adapter.finish = fail
        return adapter

    facade.memory = memory
    blocked = await flow.invoke("pr-check", task_id=task["id"], policy=policy)
    assert (
        blocked["blocked_reason"]
        == "ci_round_memory_"
        + ("read" if fault in {"read", "lost_begin"} else "write")
        + "_unconfirmed"
    ), blocked
    assert (
        blocked["work_status"] == "succeeded"
        and blocked["memory"]["write_state"] == "saved"
    )
    assert not blocked["merged"]
    intent = facade.store.get(task["id"])["ci_rounds"][0]
    facade.memory = factory
    result = await flow.invoke("pr-check", task_id=task["id"])
    assert result["phase"] == "pr_waiting", result
    confirmed = facade.store.get(task["id"])["ci_rounds"][0]
    assert confirmed["id"] == intent["id"]
    assert confirmed["facts"]["work_status"] == "failed"
    assert confirmed["memory"]["write_state"] == "saved"
    if seen:
        assert seen == [confirmed["memory"]["id"]]
    assert len(facade.store.get(task["id"])["ci_rounds"]) == 1
    assert len(flow.host.created) == 1


async def test_ci_memory_loop_new_attempt_appends_without_clock_replay_or_fake_diagnosis(
    flow, monkeypatch
):  # noqa: F811
    task, _, (_, policy, transport) = await setup(flow, monkeypatch)
    first = await flow.invoke("pr-check", task_id=task["id"], policy=policy)
    assert first["phase"] == "pr_waiting", first
    round1 = first["ci_rounds"][0]
    transport.conclusion, transport.attempt = "success", 2
    result = await flow.invoke("pr-check", task_id=task["id"])
    assert result["phase"] == "pr_ready", result
    assert len(result["ci_rounds"]) == 2
    assert result["ci_rounds"][1]["facts"]["identity"]["observed_runs"] == [
        {"run": "42", "attempt": 2}
    ]
    assert result["ci_rounds"][1]["previous"] == dict(
        task_id=task["id"], round_id=round1["id"]
    )
    assert "diagnosis" not in result["ci_rounds"][1]["previous"]
    assert result["ci_rounds"][0] == round1
    assert not result["merged"]


@pytest.mark.parametrize(
    "change", ["head", "attempt", "repository", "terminal_change", "during_memory"]
)
async def test_ci_memory_loop_rejects_divergent_identity_and_stale_memory_gate(
    flow, monkeypatch, change
):  # noqa: F811
    task, _, (_, policy, transport) = await setup(flow, monkeypatch)
    first = await flow.invoke("pr-check", task_id=task["id"], policy=policy)
    assert first["phase"] == "pr_waiting", first
    round1 = first["ci_rounds"][0]
    if change == "head":
        transport.head = "e" * 40
    elif change == "repository":
        spec = flow.service.managed_service.store.get(task["id"])["publication_spec"]
        git(
            spec["cwd"],
            "remote",
            "set-url",
            "origin",
            "https://github.com/another/repository.git",
        )
    elif change == "terminal_change":
        transport.conclusion = (
            "success"  # same actual run/attempt cannot rewrite failure
        )
    elif change == "attempt":
        original = transport.__class__.__call__

        def mismatch(self, argv, cwd):
            data = original(self, argv, cwd)
            if argv[0:2] == ["gh", "api"] and argv[2].endswith("/actions/runs/42"):
                raw = json.loads(data)
                raw["run_attempt"] = 9
                return json.dumps(raw)
            return data

        monkeypatch.setattr(transport.__class__, "__call__", mismatch)
    else:
        transport.attempt = 2
        facade = flow.service.managed_service
        factory = facade.memory

        def memory():
            adapter = factory()
            finish = adapter.finish

            async def changed(*args, **kwargs):
                saved = await finish(*args, **kwargs)
                transport.attempt = 3
                return saved

            adapter.finish = changed
            return adapter

        facade.memory = memory
    result = await flow.invoke("merge", task_id=task["id"])
    assert result["phase"] == "blocked", result
    assert not result["merged"] and result["work_status"] == "succeeded"
    assert result["ci_rounds"][0] == round1
    if change == "terminal_change":
        assert result["blocked_reason"] == "ci_round_terminal_evidence_changed"
    if change == "during_memory":
        assert result["blocked_reason"] == "ci_round_execution_changed"
        assert result["ci_rounds"][1]["memory"]["write_state"] == "saved"


async def test_ci_memory_loop_publisher_failure_blocks_merge_and_retries_official_cycle(
    flow, monkeypatch
):
    task, _, (_, policy, transport) = await setup(flow, monkeypatch)
    transport.conclusion = "success"
    publisher = flow.service.publisher
    publish_one = publisher.publish
    monkeypatch.setattr(
        publisher,
        "publish",
        lambda *a, **k: (_ for _ in ()).throw(
            OSError("synthetic unavailable publisher")
        ),
    )
    result = await flow.invoke("merge", task_id=task["id"], policy=policy)
    assert result["blocked_reason"] == "ci_round_memory_write_unconfirmed", result
    assert not result.get("merge_attempted") and not result["merged"]
    assert (
        result["work_status"] == "succeeded"
        and result["memory"]["write_state"] == "saved"
    )
    pending = result["ci_rounds"][0]
    assert pending["memory"]["write_state"] in {"failed", "pending"}
    monkeypatch.setattr(publisher, "publish", publish_one)
    result = await flow.invoke("pr-check", task_id=task["id"])
    assert result["phase"] == "pr_ready", result
    assert result["ci_rounds"][0]["id"] == pending["id"]
    assert result["ci_rounds"][0]["memory"]["id"] == pending["memory"]["id"]
    assert result["ci_rounds"][0]["memory"]["write_state"] == "saved"


async def test_ci_memory_loop_snapshot_precision_clock_dedup_and_review_remains_separate(
    flow, monkeypatch
):
    from test_task_closure import Lifecycle

    task, _, (_, policy, _) = await setup(flow, monkeypatch)
    lifecycle = Lifecycle()
    inspect = lifecycle.inspect
    clock = [1]

    def observed(*args):
        clock[0] += 1
        return dict(inspect(*args), observed_at=clock[0])

    lifecycle.inspect = observed
    monkeypatch.setattr("oraculo.pr_lifecycle.RepositoryPRLifecycle", lambda: lifecycle)
    first = await flow.invoke("pr-check", task_id=task["id"], policy=policy)
    assert first["phase"] == "pr_ready", first
    item = first["ci_rounds"][0]
    assert (
        item["facts"]["check_bindings"][0]["precision"]
        == "check_snapshot_without_run_attempt"
    )
    assert item["facts"]["identity"]["observed_runs"] == []
    before = flow.service.publisher.git.head()
    replay = await flow.invoke("pr-check", task_id=task["id"])
    assert replay["ci_rounds"] == first["ci_rounds"]
    assert replay["pr_observation"] == first["pr_observation"]
    assert flow.service.publisher.git.head() == before
    # CI tests success is distinct from a missing quality/review/policy approval.
    lifecycle.ready = False
    lifecycle.blockers = ["pr_review_pending"]

    def review_pending(*args):
        result = inspect(*args)
        result["checks"] = item["facts"]["checks"]
        return result

    lifecycle.inspect = review_pending
    blocked = await flow.invoke("merge", task_id=task["id"])
    assert blocked["phase"] == "pr_waiting", blocked
    assert blocked["ci_rounds"] == first["ci_rounds"] and not blocked["merged"]
    assert lifecycle.calls == 0


async def test_ci_memory_loop_rejects_stale_real_attempt_without_erasing_rounds(
    flow, monkeypatch
):
    task, _, (_, policy, transport) = await setup(flow, monkeypatch)
    transport.attempt = 2
    first = await flow.invoke("pr-check", task_id=task["id"], policy=policy)
    assert first["phase"] == "pr_waiting", first
    transport.attempt = 1
    result = await flow.invoke("pr-check", task_id=task["id"])
    assert result["blocked_reason"] == "ci_round_stale_attempt", result
    assert result["ci_rounds"] == first["ci_rounds"]


@pytest.mark.parametrize("action", ["pr-check", "merge", "settle"])
def test_ci_memory_loop_old_daemon_explicitly_requires_official_upgrade(
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
    assert "ci_round_memory_service_upgrade_required" in capsys.readouterr().err


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "neutral", "skipped"])
async def test_ci_memory_loop_negative_or_inconclusive_checks_are_preserved_without_fake_pass(
    flow, monkeypatch, conclusion
):
    task, _, (_, policy, transport) = await setup(flow, monkeypatch)
    transport.conclusion = conclusion
    result = await flow.invoke("merge", task_id=task["id"], policy=policy)
    assert result["phase"] == "pr_waiting", result
    assert result["work_status"] == "succeeded" and not result["merged"]
    item = result["ci_rounds"][0]
    assert item["facts"]["checks"][0]["conclusion"] == conclusion.upper()
    assert item["memory"]["work_status"] == "failed"
    assert item["memory"]["write_state"] == "saved"


@pytest.mark.parametrize("field", ["project_id", "id", "identity", "report_sha256"])
async def test_ci_memory_loop_rejects_divergent_canonical_receipt(
    flow, monkeypatch, field
):
    task, _, (_, policy, transport) = await setup(flow, monkeypatch)
    first = await flow.invoke("pr-check", task_id=task["id"], policy=policy)
    assert first["phase"] == "pr_waiting", first
    facade = flow.service.managed_service
    factory = facade.memory

    def memory():
        adapter = factory()
        status = adapter.status

        async def divergent(*args, **kwargs):
            receipt = await status(*args, **kwargs)
            receipt[field] = (
                {"thread_id": "another", "run_id": "other"}
                if field == "identity"
                else "different"
            )
            return receipt

        adapter.status = divergent
        return adapter

    facade.memory = memory
    result = await flow.invoke("merge", task_id=task["id"])
    assert result["blocked_reason"] == "ci_round_memory_binding_mismatch", result
    assert result["ci_rounds"] == first["ci_rounds"] and not result["merged"]
    assert result["work_status"] == "succeeded"


@pytest.mark.parametrize("missing", [None, []])
async def test_ci_memory_loop_missing_required_check_evidence_cannot_be_ready(
    flow, monkeypatch, missing
):
    from test_task_closure import Lifecycle

    task, _, (_, policy, _) = await setup(flow, monkeypatch)
    lifecycle = Lifecycle()
    inspect = lifecycle.inspect
    lifecycle.inspect = lambda *args: dict(inspect(*args), checks=missing)
    monkeypatch.setattr("oraculo.pr_lifecycle.RepositoryPRLifecycle", lambda: lifecycle)
    result = await flow.invoke("merge", task_id=task["id"], policy=policy)
    assert result["blocked_reason"] == (
        "ci_round_checks_unobserved"
        if missing is None
        else "ci_round_memory_terminal_evidence_required"
    ), result
    assert result["ci_rounds"] == [] and lifecycle.calls == 0
    assert not result["merged"]


@pytest.mark.parametrize("bad", ["missing_round", "diagnosis_hash"])
async def test_ci_memory_loop_correction_requires_existing_failed_round_and_real_diagnosis(
    flow, monkeypatch, tmp_path, bad
):
    task, _, (_, policy, _) = await setup(flow, monkeypatch)
    first = await flow.invoke("pr-check", task_id=task["id"], policy=policy)
    assert first["phase"] == "pr_waiting", first
    previous = first["ci_rounds"][0]
    diagnosis = tmp_path / "diagnosis.log"
    diagnosis.write_text("Synthetic diagnostic evidence\n")
    evidence = dict(
        ci_predecessor=dict(
            task_id=task["id"],
            round_id="missing" if bad == "missing_round" else previous["id"],
        )
    )
    if bad == "diagnosis_hash":
        evidence["ci_diagnosis"] = dict(
            summary="Observed diagnosis",
            proof=dict(path=str(diagnosis), sha256="0" * 64),
        )
    child, report, _ = await implementation(
        flow,
        "correction",
        evidence=evidence,
        extra_files={"test_boundary.py": "assert True\n"},
    )
    flow.host.complete(child["child"]["childThreadId"])
    flow.host.caller = "origin"
    saved = await flow.invoke("resume", task_id=child["id"])
    assert saved["work_status"] == "succeeded", saved
    _, next_policy, host = await publish(
        flow, monkeypatch, child, report, "public-correction"
    )
    host.conclusion = "success"
    result = await flow.invoke("pr-check", task_id=child["id"], policy=next_policy)
    assert result["blocked_reason"] == (
        "ci_predecessor_binding_mismatch"
        if bad == "missing_round"
        else "review_artifact_digest_mismatch"
    ), result
    assert result["ci_rounds"] == [] and not result["merged"]
    assert (
        flow.service.managed_service.store.get(task["id"])["ci_rounds"][0]["id"]
        == previous["id"]
    )


async def test_ci_memory_loop_recovery_anchor_precedes_long_truncated_evidence(
    flow, monkeypatch
):
    task, _, (_, policy, transport) = await setup(flow, monkeypatch)
    transport.required[:] = [
        "Product review (exact HEAD) " + "boundary evidence " * 200
    ]
    result = await flow.invoke("pr-check", task_id=task["id"], policy=policy)
    assert result["phase"] == "pr_waiting", result
    item = result["ci_rounds"][0]
    recovered = await SocketMemory(flow.service.config).begin(
        "alpha",
        dict(thread_id="fresh-reader", run_id="fresh-run"),
        "read-long-ci",
        "CI failure diagnosis correction evaluation history",
    )
    documents = recovered["context"]["context"]["bundle"]["documents"]
    matched = [d for d in documents if "CI loop anchor:" in d["content"]]
    assert len(matched) == 1
    anchor = next(
        line
        for line in matched[0]["content"].splitlines()
        if line.startswith("CI loop anchor: ")
    )
    data = json.loads(html.unescape(anchor.removeprefix("CI loop anchor: ")))
    assert data["round_id"] == item["id"] and data["head"] == policy["head_sha"]
    assert data["work_status"] == "failed"
    assert "Síntese abreviada" in matched[0]["content"]
    # The full canonical source remains intact; its successful write alone is
    # weaker proof than this relevant, freshly delivered recovery anchor.
    cycle = flow.service.memory_cycles.get(item["memory"]["id"])
    assert (
        cycle["report"]["evidence"]["evidence"]["facts"]["checks"][0]["name"]
        == transport.required[0]
    )


class MixedCITransport(CITransport):
    """A remote-required workflow plus managed-only B and an optional check C."""

    def __init__(self, head):
        super().__init__(head)
        self.required = ["Remote unit"]
        self.conclusion = "success"
        self.product = "failure"
        self.optional_revision = 1

    def __call__(self, argv, cwd):
        if argv[:2] == ["gh", "api"] and "/actions/runs/84" in argv[2]:
            self.calls.append(copy.deepcopy(argv))
            attempt = 2 if self.product == "success" else 1
            if "/jobs?" in argv[2]:
                return json.dumps(
                    dict(
                        total_count=1,
                        jobs=[
                            dict(
                                name="Product review (exact HEAD)",
                                head_sha=self.head,
                                run_id=84,
                                run_attempt=attempt,
                                status="completed",
                                conclusion=self.product,
                                check_run_url="https://api.github.com/repos/example/public-project/check-runs/200",
                            )
                        ],
                    )
                )
            return json.dumps(
                dict(
                    id=84,
                    check_suite_id=184,
                    head_sha=self.head,
                    run_attempt=attempt,
                    repository=dict(full_name="example/public-project"),
                    pull_requests=[dict(number=12)],
                    updated_at="2026-11-03T11:00:00Z",
                    status="completed",
                    conclusion=self.product,
                )
            )
        result = super().__call__(argv, cwd)
        if argv[0] != "gh":
            return result
        data = json.loads(result)
        if argv[1:3] == ["pr", "view"]:
            data["statusCheckRollup"] += [
                dict(
                    __typename="CheckRun",
                    name="Product review (exact HEAD)",
                    status="COMPLETED",
                    conclusion=self.product.upper(),
                ),
                dict(
                    __typename="CheckRun",
                    name="Optional telemetry",
                    status="COMPLETED",
                    conclusion="SUCCESS",
                ),
            ]
        elif argv[2].endswith("/check-runs?filter=latest&per_page=100"):
            data["check_runs"] += [
                dict(
                    id=200,
                    name="Product review (exact HEAD)",
                    head_sha=self.head,
                    app=dict(slug="github-actions"),
                    check_suite=dict(id=184),
                    status="completed",
                    conclusion=self.product,
                ),
                dict(
                    id=300,
                    name="Optional telemetry",
                    head_sha=self.head,
                    app=dict(slug="github-actions"),
                    check_suite=dict(id=142),
                    status="completed",
                    conclusion="success",
                    details_url=f"https://example.org/optional/{self.optional_revision}",
                ),
            ]
            data["total_count"] = len(data["check_runs"])
        elif "/actions/runs?" in argv[2]:
            data["workflow_runs"].append(
                dict(
                    id=84,
                    check_suite_id=184,
                    run_attempt=2 if self.product == "success" else 1,
                )
            )
            data["total_count"] = len(data["workflow_runs"])
        elif "/runs/42/attempts/" in argv[2]:
            data["jobs"].append(
                dict(
                    name="Optional telemetry",
                    status="completed",
                    conclusion="success",
                    head_sha=self.head,
                    run_id=42,
                    run_attempt=self.attempt,
                    check_run_url="https://api.github.com/repos/example/public-project/check-runs/300",
                    display_revision=self.optional_revision,
                )
            )
            data["total_count"] = len(data["jobs"])
        return json.dumps(data)


@pytest.mark.parametrize("missing", [True, False])
async def test_ci_memory_loop_declared_actions_coverage_requires_matching_actual_job(
    flow, monkeypatch, missing
):
    task, _, (_, policy, transport) = await setup(flow, monkeypatch)
    transport.conclusion = "success"
    original = transport.__class__.__call__

    def incomplete(self, argv, cwd):
        raw = original(self, argv, cwd)
        if (
            argv[0:2] == ["gh", "api"]
            and "/attempts/" in argv[2]
            and "/jobs?" in argv[2]
        ):
            data = json.loads(raw)
            if missing:
                data["jobs"], data["total_count"] = [], 0
            else:
                for job in data["jobs"]:
                    job["conclusion"] = "failure"
            return json.dumps(data)
        return raw

    monkeypatch.setattr(transport.__class__, "__call__", incomplete)
    result = await flow.invoke("merge", task_id=task["id"], policy=policy)
    assert result["blocked_reason"] == (
        "ci_round_check_execution_unproven"
        if missing
        else "ci_round_check_evidence_mismatch"
    ), result
    assert result["ci_rounds"] == [] and not result["merged"]
    assert (
        result["pr_observation"]["receipt"]["ci_execution"]["event"]["conclusion"]
        == "failed"
    )
    assert (
        result["work_status"] == "succeeded"
        and result["memory"]["write_state"] == "saved"
    )


@pytest.mark.parametrize("entrypoint", ["api", "cli"])
async def test_ci_memory_loop_mixed_policy_rerun_optional_facts_and_real_attempt_contradiction(
    flow, monkeypatch, tmp_path, entrypoint
):
    task, _, (_, policy, original) = await setup(flow, monkeypatch)
    transport = MixedCITransport(original.head)
    lifecycle = RepositoryPRLifecycle(transport)
    monkeypatch.setattr("oraculo.pr_lifecycle.RepositoryPRLifecycle", lambda: lifecycle)
    policy["required_checks"] = ["Remote unit", "Product review (exact HEAD)"]

    async def check(expected_exit=0):
        if entrypoint == "api":
            return await flow.invoke("pr-check", task_id=task["id"], policy=policy)
        code, result, _, stderr = await run_cli_fifo(
            flow,
            tmp_path,
            "pr-check",
            dict(task_id=task["id"], policy=policy),
            pad_hex=14000,
            minimum=8192,
        )
        assert code == expected_exit, (result, stderr)
        return result

    failed = await check()
    assert failed["phase"] == "pr_waiting", failed
    first = failed["ci_rounds"][0]
    by_name = {c["check"]["name"]: c for c in first["facts"]["check_bindings"]}
    assert by_name["Remote unit"]["precision"] == "workflow_check_run_attempt"
    assert (
        by_name["Product review (exact HEAD)"]["precision"]
        == "workflow_check_run_attempt"
    )
    assert by_name["Product review (exact HEAD)"]["proof"]["run"] == "84"
    assert first["memory"]["write_state"] == "saved" and not failed["merged"]
    transport.product = "success"  # rerun only B, A stays run42/attempt1
    passed = await check()
    assert passed["phase"] == "pr_ready", passed
    assert len(passed["ci_rounds"]) == 2
    assert passed["ci_rounds"][0] == first
    assert passed["ci_rounds"][1]["memory"]["write_state"] == "saved"
    assert passed["ci_rounds"][1]["previous"] == dict(
        task_id=task["id"], round_id=first["id"]
    )
    before = flow.service.publisher.git.head()
    replay = await check()
    assert replay["ci_rounds"] == passed["ci_rounds"]
    transport.optional_revision += 1  # endpoint/job hashes change, policy facts do not
    irrelevant = await check()
    assert irrelevant["phase"] == "pr_ready", irrelevant
    assert irrelevant["ci_rounds"] == passed["ci_rounds"]
    assert flow.service.publisher.git.head() == before
    recovered = await SocketMemory(flow.service.config).begin(
        "alpha",
        dict(thread_id="fresh-mixed-reader", run_id="fresh-mixed-run"),
        "mixed-ci-recovery",
        "CI failure diagnosis correction evaluation history",
    )
    raw = json.dumps(recovered["context"])
    assert first["id"] in raw and passed["ci_rounds"][1]["id"] in raw
    # A really identified check cannot change conclusion in the same attempt,
    # even when B has its own different snapshot/round identity.
    transport.conclusion = "failure"
    illegal = await check(1 if entrypoint == "cli" else 0)
    assert illegal["blocked_reason"] == "ci_round_terminal_evidence_changed", illegal
    assert illegal["ci_rounds"] == passed["ci_rounds"]
    assert not illegal["merged"] and illegal["work_status"] == "succeeded"
