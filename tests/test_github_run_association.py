"""Simulated GitHub metadata; real observer, Git, service, CLI and memory."""

import copy
import json
import subprocess

import pytest
from test_github_policy_compatibility import setup
from test_report_prevalidation import flow as flow_fixture

flow = flow_fixture


class AssociationTransport:
    def __init__(self, host, *, absent=False):
        self.host = host
        self.absent = absent
        self.association_reads = 0
        self.reads = {}

    def modify(self, path, value):
        return value

    def __call__(self, argv, cwd):
        if argv[:2] == ["gh", "api"] and argv[2].endswith(
            f"/commits/{self.host.head}/pulls?per_page=100"
        ):
            self.host.calls.append(copy.deepcopy(argv))
            self.association_reads += 1
            value = [
                dict(
                    number=getattr(self.host, "pr_number", 1),
                    head=dict(sha=self.host.head),
                    base=dict(
                        ref="main",
                        repo=dict(
                            full_name="example/public-project"
                            if hasattr(self.host, "pr_number")
                            else "example/repo"
                        ),
                    ),
                    state="closed" if self.host.merged else "open",
                )
            ]
        else:
            raw = self.host(argv, cwd)
            if argv[:2] != ["gh", "api"]:
                return raw
            value = json.loads(raw)
            if "/actions/runs/" in argv[2] and "/attempts/" not in argv[2]:
                value["event"] = "pull_request"
                if self.absent:
                    value.pop("pull_requests")
                else:
                    value["pull_requests"] = []
        self.reads[argv[2]] = self.reads.get(argv[2], 0) + 1
        return json.dumps(self.modify(argv[2], value))


@pytest.mark.parametrize("historical", [False, True])
@pytest.mark.parametrize("absent", [False, True])
def test_github_run_association_empty_metadata_uses_authenticated_commit_relation(
    tmp_path, historical, absent
):
    host, lifecycle, pr, policy = setup(tmp_path)
    host.merged = historical
    transport = AssociationTransport(host, absent=absent)
    lifecycle.runner = transport
    result = lifecycle.observe_pr_ci(pr, policy, tmp_path)
    assert result["event"]["conclusion"] == "passed"
    assert {r["run"] for r in result["evidence"]["runs"]} == {"42", "43"}
    assert transport.association_reads >= 2
    proof = result["evidence"]["run_pr_associations"]
    assert set(proof) == {"42", "43"}
    assert all(p["source"] == "commit_associated_pull_requests" for p in proof.values())
    observations = result["evidence"]["association_observations"]
    assert len(observations) == 2
    assert all(
        o["projection"] and o["source"] == "authenticated_gh_api" for o in observations
    )
    assert all(c[:3] != ["gh", "pr", "merge"] for c in host.calls)


@pytest.mark.parametrize(
    "change",
    [
        "empty",
        "ambiguous",
        "boundary",
        "null",
        "object",
        "row_null",
        "head_null",
        "base_null",
        "repo_null",
        "number_bool",
        "wrong_pr",
        "wrong_head",
        "wrong_base",
        "wrong_repo",
        "wrong_state",
        "missing_state",
        "malformed_state",
    ],
)
def test_github_run_association_invalid_commit_relation_blocks(tmp_path, change):
    from oraculo.github_policy import GitHubPolicyError

    host, lifecycle, pr, policy = setup(tmp_path)
    transport = AssociationTransport(host)

    def modify(path, value):
        if not path.endswith("/pulls?per_page=100"):
            return value
        if change == "empty":
            return []
        if change == "ambiguous":
            return value * 2
        if change == "boundary":
            return value * 100
        if change == "null":
            return None
        if change == "object":
            return {"items": value}
        if change == "row_null":
            return [None]
        row = value[0]
        if change == "head_null":
            row["head"] = None
        if change == "base_null":
            row["base"] = None
        if change == "repo_null":
            row["base"]["repo"] = None
        if change == "number_bool":
            row["number"] = True
        if change == "wrong_pr":
            row["number"] = 2
        if change == "wrong_head":
            row["head"]["sha"] = "e" * 40
        if change == "wrong_base":
            row["base"]["ref"] = "other"
        if change == "wrong_repo":
            row["base"]["repo"]["full_name"] = "other/repo"
        if change == "wrong_state":
            row["state"] = "closed"
        if change == "missing_state":
            row.pop("state")
        if change == "malformed_state":
            row["state"] = []
        return value

    transport.modify = modify
    lifecycle.runner = transport
    with pytest.raises(
        GitHubPolicyError, match="ci_(commit_pr_association|host_binding)"
    ):
        lifecycle.observe_pr_ci(pr, policy, tmp_path)
    assert all(c[:3] != ["gh", "pr", "merge"] for c in host.calls)


@pytest.mark.parametrize(
    "change",
    [
        "conflict",
        "mixed",
        "duplicate",
        "null",
        "object",
        "row_null",
        "bool",
        "push",
        "dispatch",
        "target",
        "missing_event",
    ],
)
@pytest.mark.parametrize("run_id", [42, 43])
def test_github_run_association_run_conflict_never_falls_back(tmp_path, change, run_id):
    from oraculo.github_policy import GitHubPolicyError

    host, lifecycle, pr, policy = setup(tmp_path)
    transport = AssociationTransport(host)

    def modify(path, value):
        if not path.endswith(f"/actions/runs/{run_id}"):
            return value
        if change == "conflict":
            value["pull_requests"] = [{"number": 2}]
        if change == "mixed":
            value["pull_requests"] = [{"number": 1}, {"number": 2}]
        if change == "duplicate":
            value["pull_requests"] = [{"number": 1}] * 2
        if change == "null":
            value["pull_requests"] = None
        if change == "object":
            value["pull_requests"] = {"number": 1}
        if change == "row_null":
            value["pull_requests"] = [None]
        if change == "bool":
            value["pull_requests"] = [{"number": True}]
        if change == "push":
            value["event"] = "push"
        if change == "dispatch":
            value["event"] = "workflow_dispatch"
        if change == "target":
            value["event"] = "pull_request_target"
        if change == "missing_event":
            value.pop("event")
        return value

    transport.modify = modify
    lifecycle.runner = transport
    with pytest.raises(GitHubPolicyError, match="ci_(run_pr_association|host_binding)"):
        lifecycle.observe_pr_ci(pr, policy, tmp_path)
    if run_id == 42:
        assert transport.association_reads == 0


@pytest.mark.parametrize("status", [401, 403, 404, 429, 500])
def test_github_run_association_http_failure_is_not_absence(tmp_path, status):
    from oraculo.github_policy import GitHubPolicyError

    host, lifecycle, pr, policy = setup(tmp_path)
    transport = AssociationTransport(host)

    def modify(path, value):
        if path.endswith("/pulls?per_page=100"):
            raise subprocess.CalledProcessError(
                1,
                ["gh", "api", path],
                output="{}",
                stderr=f"gh: denied (HTTP {status})",
            )
        return value

    transport.modify = modify
    lifecycle.runner = transport
    with pytest.raises(GitHubPolicyError, match="ci_host_policy_"):
        lifecycle.observe_pr_ci(pr, policy, tmp_path)


@pytest.mark.parametrize("change", ["relation", "jobs", "run_list"])
def test_github_run_association_changed_final_snapshot_blocks(tmp_path, change):
    from oraculo.state import StateError

    host, lifecycle, pr, policy = setup(tmp_path)
    transport = AssociationTransport(host)

    def modify(path, value):
        if transport.reads[path] <= 1:
            return value
        if change == "relation" and path.endswith("/pulls?per_page=100"):
            value[0]["head"]["sha"] = "e" * 40
        if change == "jobs" and "/attempts/" in path:
            value["jobs"][0]["conclusion"] = "failure"
        # observe_pr_ci reads this endpoint once before the observer itself.
        if (
            change == "run_list"
            and "/actions/runs?" in path
            and transport.reads[path] > 2
        ):
            value["workflow_runs"][0]["run_attempt"] += 1
        return value

    transport.modify = modify
    lifecycle.runner = transport
    with pytest.raises(StateError, match="ci_snapshot_changed"):
        lifecycle.observe_pr_ci(pr, policy, tmp_path)


@pytest.mark.parametrize(
    "change",
    [
        "head",
        "repo",
        "attempt",
        "suite",
        "app",
        "job",
        "failed",
        "cancelled",
        "pending",
    ],
)
def test_github_run_association_does_not_waive_execution_graph(tmp_path, change):
    from oraculo.publication import PublicationError
    from oraculo.state import StateError

    host, lifecycle, pr, policy = setup(tmp_path)
    transport = AssociationTransport(host)

    def modify(path, value):
        if path.endswith("/actions/runs/43"):
            if change == "head":
                value["head_sha"] = "e" * 40
            if change == "repo":
                value["repository"]["full_name"] = "other/repo"
            if change == "attempt":
                value["run_attempt"] += 1
            if change == "suite":
                value["check_suite_id"] += 1
            if change == "failed":
                value["conclusion"] = "failure"
            if change == "cancelled":
                value["conclusion"] = "cancelled"
            if change == "pending":
                value.update(status="in_progress", conclusion=None)
        if change == "app" and "/check-runs?" in path:
            value["check_runs"][0]["app"]["slug"] = "other"
        if change == "job" and "/attempts/" in path:
            value["jobs"][0]["check_run_url"] = (
                "https://api.github.com/repos/other/repo/check-runs/100"
            )
        return value

    transport.modify = modify
    lifecycle.runner = transport
    try:
        result = lifecycle.observe_pr_ci(pr, policy, tmp_path)
    except (StateError, PublicationError):
        pass
    else:
        assert result["event"]["conclusion"] != "passed"


@pytest.mark.parametrize("absent", [False, True])
def test_github_run_association_actual_jq_filters_private_bodies(tmp_path, absent):
    host, lifecycle, pr, policy = setup(tmp_path)
    transport = AssociationTransport(host, absent=absent)
    fragment = "unselected-private-source-fragment"

    def runner(argv, cwd):
        raw = transport(argv, cwd)
        if argv[:2] != ["gh", "api"]:
            return raw
        value = json.loads(raw)
        if argv[2].endswith("/pulls?per_page=100"):
            value[0].update(body=fragment, title=fragment, files=[dict(patch=fragment)])
        elif "/actions/runs/" in argv[2] and "/attempts/" not in argv[2]:
            value.update(display_title=fragment, head_commit=dict(message=fragment))
        if "--jq" in argv:
            return subprocess.run(
                ["jq", "-c", argv[argv.index("--jq") + 1]],
                input=json.dumps(value),
                text=True,
                capture_output=True,
                check=True,
            ).stdout
        return json.dumps(value)

    lifecycle.runner = runner
    result = lifecycle.observe_pr_ci(pr, policy, tmp_path)
    assert result["event"]["conclusion"] == "passed"
    assert fragment not in json.dumps(result)


@pytest.mark.parametrize("entrypoint", ["api", "cli"])
@pytest.mark.parametrize("timing", ["ci_memory", "settlement_memory"])
async def test_github_run_association_revalidates_after_memory(
    flow, monkeypatch, tmp_path, entrypoint, timing
):
    from test_ci_round_memory import setup as managed_setup
    from test_github_policy_compatibility import ManagedPolicyHost
    from test_stdio_fifo import run_cli_fifo

    from oraculo.pr_lifecycle import RepositoryPRLifecycle

    task, _, (_, policy, original) = await managed_setup(flow, monkeypatch)
    host = ManagedPolicyHost(original.head)
    host.pr_number = 12
    host.required = policy["required_checks"].copy()
    host.merged = True
    policy["method"] = "merge"
    transport = AssociationTransport(host)
    conflict = False
    armed = True

    def modify(path, value):
        if conflict and path.endswith("/pulls?per_page=100"):
            value[0]["number"] = 99
        return value

    transport.modify = modify
    lifecycle = RepositoryPRLifecycle(transport)
    monkeypatch.setattr("oraculo.pr_lifecycle.RepositoryPRLifecycle", lambda: lifecycle)
    original_call = flow.host.call
    settlements = []

    async def call(name, args):
        if name == "t3_thread_organize":
            settlements.append(args)
            flow.host.threads[args["threadId"]]["settled"] = True
            return dict(accepted=True)
        value = await original_call(name, args)
        if name == "list_thread_pull_requests":
            value["pullRequests"] = [
                dict(
                    host="github.com",
                    repository=policy["repository"],
                    number=12,
                    state="merged",
                    isDraft=False,
                    baseBranch="main",
                )
            ]
        return value

    flow.host.call = call
    factory = flow.service.managed_service.memory

    def memory():
        adapter = factory()
        finish = adapter.finish

        async def after(*args, **kwargs):
            nonlocal conflict, armed
            value = await finish(*args, **kwargs)
            summary = args[-1].get("summary", "")
            if armed and summary.startswith(
                "CI loop anchor:" if timing == "ci_memory" else "Task merged;"
            ):
                conflict = True
                armed = False
            return value

        adapter.finish = after
        return adapter

    flow.service.managed_service.memory = memory
    store = flow.service.managed_service.store
    before = copy.deepcopy(store.get(task["id"]))

    async def check(payload, expected):
        if entrypoint == "api":
            return await flow.invoke("pr-check", **payload)
        code, result, _, stderr = await run_cli_fifo(
            flow, tmp_path, "pr-check", payload, pad_hex=14000, minimum=8192
        )
        assert code == expected, (result, stderr)
        return result

    blocked = await check(dict(task_id=task["id"], policy=policy), 1)
    assert conflict and not armed
    assert blocked["blocked_reason"] == "ci_host_binding_mismatch", blocked
    assert not blocked["settled"] and settlements == []
    saved = copy.deepcopy(store.get(task["id"])["ci_rounds"])
    assert saved[0]["memory"]["write_receipt"]["state"] == "confirmed"
    conflict = False
    recovered = await check(dict(task_id=task["id"]), 0)
    assert recovered["phase"] == "settled", recovered
    assert store.get(task["id"])["ci_rounds"] == saved
    for key in ("request", "report", "memory", "child"):
        assert store.get(task["id"])[key] == before[key]
    assert all(c[:3] != ["gh", "pr", "merge"] for c in host.calls)
