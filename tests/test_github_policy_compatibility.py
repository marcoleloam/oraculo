"""Simulated GitHub transport; real adapter, local Git/service/canonical memory."""

import copy
import json
import subprocess

import pytest
from test_ci_host import GitHubCIHost
from test_managed_tasks import git, initialize
from test_report_prevalidation import flow as flow_fixture

from oraculo.pr_lifecycle import RepositoryPRLifecycle
from oraculo.publication import PublicationError

flow = flow_fixture

LIMIT = "Upgrade to GitHub Pro or make this repository public to enable this feature."


class PolicyHost(GitHubCIHost):
    def __init__(self, head):
        super().__init__(head)
        self.require_review = False
        self.review = ""
        self.capability = "limited"
        self.protection_error = None
        self.rules_error = None
        self.rules = []
        self.merged = False
        self.merge_sha = "d" * 40
        self.merge_parent_base = self.base
        self.actor = "example-actor"
        self.protected = False
        self.policy_checks = None

    def __call__(self, argv, cwd):
        if argv[:3] == ["gh", "pr", "merge"]:
            self.calls.append(copy.deepcopy(argv))
            assert argv[argv.index("--match-head-commit") + 1] == self.head
            assert "--admin" not in argv and "--auto" not in argv
            if getattr(self, "on_merge", None):
                self.on_merge()
            self.merged = True
            return "accepted"
        if argv[:2] == ["gh", "api"]:
            path = argv[2].removesuffix("?per_page=100")
            value = None
            if path == "user":
                value = dict(login=self.actor, id=7)
            elif path == "repos/example/repo":
                value = dict(full_name=self.remote, id=11, private=True)
            elif path == "repos/example/repo/branches/main":
                value = dict(
                    name="main",
                    protected=self.protected,
                    commit=dict(
                        sha=getattr(
                            self,
                            "current_base_head",
                            self.merge_sha if self.merged else self.base,
                        )
                    ),
                )
            elif path.endswith("/commits/" + self.head):
                value = dict(sha=self.head)
            elif path.endswith("/commits/" + self.merge_sha):
                value = dict(
                    sha=self.merge_sha,
                    parents=[dict(sha=self.merge_parent_base), dict(sha=self.head)],
                )
            elif "/compare/" in path:
                value = dict(
                    status="ahead",
                    merge_base_commit=dict(
                        sha=path.split("/compare/")[1].split("...")[0]
                    ),
                    commits=[dict(sha=self.merge_sha)],
                )
            elif "/collaborators/" in path and path.endswith("/permission"):
                login = path.split("/collaborators/")[1].split("/")[0]
                assert login.startswith("reviewer-")
                value = dict(
                    permission="write",
                    user=dict(id=int(login.removeprefix("reviewer-")), login=login),
                )
            elif path.endswith("/pulls/1/reviews"):
                value = getattr(self, "review_rows", [])
            elif path.endswith("/protection") or path.endswith("/rules/branches/main"):
                error = (
                    self.protection_error
                    if path.endswith("/protection")
                    else self.rules_error
                )
                if error is None and self.capability == "limited":
                    error = (403, LIMIT)
                if error:
                    self.calls.append(copy.deepcopy(argv))
                    status, message = error
                    raise subprocess.CalledProcessError(
                        1,
                        argv,
                        output=json.dumps(dict(message=message, status=str(status))),
                        stderr=f"gh: {message} (HTTP {status})",
                    )
                if path.endswith("/rules/branches/main"):
                    value = self.rules
                else:
                    value = dict(
                        required_status_checks=dict(
                            contexts=self.policy_checks or self.required, checks=[]
                        ),
                        required_pull_request_reviews=dict(
                            required_approving_review_count=getattr(
                                self, "remote_review_count", int(self.require_review)
                            ),
                            **getattr(self, "review_params", {}),
                        ),
                    )
            if value is not None:
                self.calls.append(copy.deepcopy(argv))
                return json.dumps(value)
        raw = super().__call__(argv, cwd)
        if self.merged and argv[:3] == ["gh", "pr", "view"]:
            value = json.loads(raw)
            value.update(state="MERGED", mergeCommit=dict(oid=self.merge_sha))
            return json.dumps(value)
        if self.merged and argv[:2] == ["gh", "api"] and argv[2].endswith("/pulls/1"):
            value = json.loads(raw)
            value.update(
                state="closed",
                merged=True,
                merge_commit_sha=self.merge_sha,
                merged_at="2026-11-04T12:00:00Z",
            )
            return json.dumps(value)
        return raw


def setup(tmp_path):
    initialize(tmp_path)
    git(tmp_path, "remote", "add", "safe", "https://github.com/example/repo.git")
    head = git(tmp_path, "rev-parse", "HEAD")
    host = PolicyHost(head)
    pr = dict(
        host="github",
        repository="example/repo",
        base="main",
        head_sha=head,
        number=1,
        url="https://github.com/example/repo/pull/1",
    )
    policy = dict(
        repository="example/repo",
        base="main",
        head_sha=head,
        required_checks=host.required.copy(),
        required_review=False,
        method="merge",
        merge_authorized=True,
        policy_source="Origin authorized project CI and automated review; no human approval required",
    )
    return host, RepositoryPRLifecycle(host), pr, policy


def test_github_policy_compatibility_explicit_policy_survives_subscription_limit(
    tmp_path,
):
    host, lifecycle, pr, policy = setup(tmp_path)
    result = lifecycle.validate_host_policy(pr, policy, tmp_path)
    assert result["required_checks"] == sorted(policy["required_checks"])
    assert result["remote_capability"]["state"] == "feature_unavailable"
    assert result["remote_capability"]["actor"]["login"] == host.actor
    assert result["remote_required_checks"] == []
    assert any("/rules/branches/" in c[2] for c in host.calls if c[:2] == ["gh", "api"])


def test_github_policy_compatibility_observer_uses_managed_checks(tmp_path):
    host, lifecycle, pr, policy = setup(tmp_path)
    result = lifecycle.observe_pr_ci(pr, policy, tmp_path)
    assert result["event"]["conclusion"] == "passed"
    assert {c["name"] for c in result["evidence"]["check_bindings"]} == set(
        policy["required_checks"]
    )
    assert (
        result["evidence"]["policy"]["remote_capability"]["state"]
        == "feature_unavailable"
    )


def test_github_policy_compatibility_historical_merge_readonly(tmp_path):
    host, lifecycle, pr, policy = setup(tmp_path)
    host.merged = True
    result = lifecycle.inspect(pr, policy, tmp_path)
    assert result["state"] == "merged"
    assert result["merge_verification"]["merge_sha"] == host.merge_sha
    result = lifecycle.observe_pr_ci(pr, policy, tmp_path)
    assert result["event"]["conclusion"] == "passed"
    assert result["evidence"]["observation_timing"] == "after_merge"
    assert all(c[:3] != ["gh", "pr", "merge"] for c in host.calls)


@pytest.mark.parametrize(
    "status,message,code",
    [
        (401, "Bad credentials", "authentication_failed"),
        (403, "Resource not accessible by personal access token", "access_denied"),
        (403, "Forbidden", "access_denied"),
        (404, "Not Found", "not_found"),
        (429, "Too many requests", "rate_limited"),
        (500, "Server error", "server_error"),
    ],
)
def test_github_policy_compatibility_no_failopen(tmp_path, status, message, code):
    host, lifecycle, pr, policy = setup(tmp_path)
    host.protection_error = status, message
    with pytest.raises(PublicationError, match=code) as error:
        lifecycle.validate_host_policy(pr, policy, tmp_path)
    assert error.value.observations[-1]["http_status"] == status
    assert json.loads(error.value.observations[-1]["stdout"])["message"] == message


@pytest.mark.parametrize(
    "kind",
    [
        "unprotected",
        "rules",
        "reviews",
        "enforced",
        "unspecified",
        "no_checks",
        "review_bool",
        "source",
        "identity",
        "rules_access",
    ],
)
def test_github_policy_compatibility_remote_floor_and_explicit_authority(
    tmp_path, kind
):
    host, lifecycle, pr, policy = setup(tmp_path)
    expected = None
    if kind == "unprotected":
        host.capability = "available"
        host.protection_error = 404, "Branch not protected"
    if kind in {"rules", "reviews"}:
        host.capability = "available"
        host.protection_error = 403, LIMIT
        host.rules = (
            [
                dict(
                    type="required_status_checks",
                    parameters=dict(
                        required_status_checks=[dict(context="extra remote")]
                    ),
                )
            ]
            if kind == "rules"
            else [
                dict(
                    type="pull_request",
                    parameters=dict(required_approving_review_count=1),
                )
            ]
        )
        expected = "actual_host_policy"
    if kind == "enforced":
        host.capability = "available"
        host.protected = True
    if kind == "unspecified":
        policy["required_checks"] = []
        expected = "explicit_project_policy"
    if kind == "no_checks":
        policy["allow_no_checks"] = True
        expected = "explicit_project_policy"
    if kind == "review_bool":
        del policy["required_review"]
        expected = "explicit_project_policy"
    if kind == "source":
        policy["policy_source"] = ""
        expected = "explicit_project_policy"
    if kind == "identity":
        host.remote = "other/repo"
        expected = "identity_mismatch"
    if kind == "rules_access":
        host.rules_error = 403, "Resource not accessible by integration"
        expected = "access_denied"
    if expected:
        with pytest.raises(PublicationError, match=expected):
            lifecycle.validate_host_policy(pr, policy, tmp_path)
    else:
        result = lifecycle.validate_host_policy(pr, policy, tmp_path)
        assert result["remote_capability"]["state"] == (
            "unprotected" if kind == "unprotected" else "enforced"
        )


@pytest.mark.parametrize(
    "kind",
    [
        "truncated",
        "malformed",
        "network",
        "source",
        "duplicate",
        "wrong_run",
        "wrong_head",
        "pending",
        "missing",
        "skipped",
        "cancelled",
        "failed",
        "review",
    ],
)
def test_github_policy_compatibility_ci_negatives(tmp_path, kind):
    host, lifecycle, pr, policy = setup(tmp_path)
    original = host.__call__
    if kind == "malformed":
        host.capability = "available"

    def transport(argv, cwd):
        if kind == "network" and argv[:2] == ["gh", "api"] and "/protection" in argv[2]:
            raise subprocess.TimeoutExpired(argv, 60)
        raw = original(argv, cwd)
        if argv[:2] != ["gh", "api"]:
            return raw
        path = argv[2]
        if kind == "malformed" and "/rules/branches/" in path:
            return "["
        data = json.loads(raw)
        if path.endswith("/check-runs?filter=latest&per_page=100"):
            checks = data["check_runs"]
            if kind == "source":
                checks[0]["app"]["slug"] = "other"
            if kind == "duplicate":
                checks.append(copy.deepcopy(checks[0]))
                data["total_count"] += 1
            if kind == "wrong_head":
                checks[0]["head_sha"] = "e" * 40
            if kind == "missing":
                checks.pop()
                data["total_count"] -= 1
            if kind in {"pending", "skipped", "cancelled", "failed"}:
                checks[0].update(
                    status="in_progress" if kind == "pending" else "completed",
                    conclusion=None
                    if kind == "pending"
                    else "failure"
                    if kind == "failed"
                    else kind,
                )
            if kind == "truncated":
                data["total_count"] += 1
        if kind == "wrong_run" and path.endswith("/actions/runs/42"):
            data["repository"]["full_name"] = "other/repo"
        return json.dumps(data)

    if kind == "review":
        policy["required_review"] = True
    lifecycle.runner = transport
    try:
        result = lifecycle.observe_pr_ci(pr, policy, tmp_path)
    except Exception as error:
        # Typed adapter/state errors, never an accidental raw subprocess crash.
        assert hasattr(error, "code") or type(error).__name__ == "StateError", repr(
            error
        )
    else:
        assert result is None or result["event"]["conclusion"] != "passed"
    assert not any(c[:3] == ["gh", "pr", "merge"] for c in host.calls)


class ManagedPolicyHost(PolicyHost):
    """Only fixture identity translation; no real GitHub credential or endpoint."""

    def __call__(self, argv, cwd):
        number = getattr(self, "pr_number", 12)
        offset = getattr(self, "run_offset", 0)
        translated = [
            a.replace("example/public-project", "example/repo")
            .replace(f"/pulls/{number}", "/pulls/1")
            .replace(f"/pull/{number}", "/pull/1")
            for a in argv
        ]
        if offset:
            translated = [
                a.replace(f"/runs/{42 + offset}", "/runs/42").replace(
                    f"/runs/{43 + offset}", "/runs/43"
                )
                for a in translated
            ]
        raw = super().__call__(translated, cwd)
        if argv[0] != "gh" or argv[:3] == ["gh", "pr", "merge"]:
            return raw

        def translate(value, key=None):
            if isinstance(value, dict):
                return {k: translate(v, k) for k, v in value.items()}
            if isinstance(value, list):
                return [translate(v) for v in value]
            if isinstance(value, str):
                value = value.replace("example/repo", "example/public-project").replace(
                    "/pull/1", f"/pull/{number}"
                )
                for check_id in (100, 101, 102):
                    value = value.replace(
                        f"/check-runs/{check_id}", f"/check-runs/{check_id + offset}"
                    )
                return value
            if key == "number" and value == 1:
                return number
            if key in {"id", "run_id", "check_suite_id"} and value in {
                42,
                43,
                100,
                101,
                102,
                142,
                143,
            }:
                return value + offset
            return value

        return json.dumps(translate(json.loads(raw)))


@pytest.mark.parametrize("entrypoint", ["api", "cli"])
@pytest.mark.parametrize("historical", [False, True])
@pytest.mark.parametrize("run_association", ["native", "commit"])
async def test_github_policy_compatibility_journey(
    flow, monkeypatch, tmp_path, entrypoint, historical, run_association
):
    from test_ci_round_memory import setup as managed_setup
    from test_stdio_fifo import run_cli_fifo

    from oraculo.task_transport import SocketMemory

    task, _, (_, policy, original) = await managed_setup(flow, monkeypatch)
    host = ManagedPolicyHost(original.head)
    host.required = policy["required_checks"].copy()
    host.merged = historical
    policy["method"] = "merge"
    # A read-only observation uses the existing immutable policy; no new merge intent.
    lifecycle = RepositoryPRLifecycle(host)
    if run_association == "commit":
        from test_github_run_association import AssociationTransport

        host.pr_number = 12
        lifecycle.runner = AssociationTransport(host)
    monkeypatch.setattr("oraculo.pr_lifecycle.RepositoryPRLifecycle", lambda: lifecycle)
    store = flow.service.managed_service.store
    before = copy.deepcopy(store.get(task["id"]))
    original_call = flow.host.call

    async def call(name, args):
        if name == "t3_thread_organize":
            flow.host.threads[args["threadId"]]["settled"] = True
            return dict(accepted=True)
        result = await original_call(name, args)
        if name == "list_thread_pull_requests" and host.merged:
            result["pullRequests"] = [
                dict(
                    host="github.com",
                    repository=policy["repository"],
                    number=12,
                    state="merged",
                    isDraft=False,
                    baseBranch="main",
                )
            ]
        return result

    flow.host.call = call

    async def check(policy_payload=None):
        payload = dict(
            task_id=task["id"], **({"policy": policy_payload} if policy_payload else {})
        )
        if entrypoint == "api":
            return await flow.invoke("pr-check", **payload)
        code, result, _, stderr = await run_cli_fifo(
            flow, tmp_path, "pr-check", payload, pad_hex=14000, minimum=8192
        )
        assert code == 0, (result, stderr)
        return result

    # Preserve a real adapter failure, then recover the same task with fresh reads.
    host.protection_error = 403, "Forbidden"
    if entrypoint == "api":
        blocked = await flow.invoke("pr-check", task_id=task["id"], policy=policy)
    else:
        code, blocked, _, _ = await run_cli_fifo(
            flow,
            tmp_path,
            "pr-check",
            dict(task_id=task["id"], policy=policy),
            pad_hex=14000,
            minimum=8192,
        )
        assert code == 1
    assert blocked["blocked_reason"] == "ci_host_policy_access_denied"
    host.protection_error = None
    result = await check()
    assert result["merged"] is historical, result
    assert result["phase"] == ("settled" if historical else "pr_ready"), result
    current = store.get(task["id"])
    round1 = current["ci_rounds"][0]
    assert round1["memory"]["write_receipt"]["state"] == "confirmed"
    if run_association == "commit":
        saved_proof = round1["report"]["evidence"]["execution"]["evidence"]
        assert saved_proof["association_observations"]
        assert all(
            p["source"] == "commit_associated_pull_requests"
            for p in saved_proof["run_pr_associations"].values()
        )
    assert len(round1["facts"]["check_bindings"]) == len(policy["required_checks"])
    assert round1["facts"]["identity"].get("observation_timing") == (
        "after_external_merge" if historical else None
    )
    assert current.get("merge_attempted") is not True
    assert (
        current["pr_observation"]["receipt"]["observed_host_policy"][
            "remote_capability"
        ]["state"]
        == "feature_unavailable"
    )
    for key in ("request", "report", "submitted_report", "memory", "child", "base_sha"):
        assert current.get(key) == before.get(key)
    assert any(
        e["proof"].get("reason") == "ci_host_policy_access_denied"
        for e in store.describe(task["id"])["events"]
    )
    replay = await check()
    assert replay["ci_rounds"] == result["ci_rounds"]
    recalled = await SocketMemory(flow.service.config).begin(
        "alpha",
        dict(thread_id="fresh-reader", run_id="fresh-run"),
        "recover-policy-round",
        "CI failure diagnosis correction evaluation history",
    )
    assert round1["id"] in json.dumps(recalled["context"])
    assert all(c[:3] != ["gh", "pr", "merge"] for c in host.calls)

    if historical:
        # A later external merge advances main, not the original PR's immutable
        # head/merge or the check executions already in canonical memory.
        host.base = "e" * 40
        host.current_base_head = host.base
        advanced = await check()
        assert advanced["phase"] == "settled", advanced
        assert advanced["merge_receipt"]["merge_sha"] == host.merge_sha
        assert advanced["ci_rounds"] == result["ci_rounds"]
        assert all(c[:3] != ["gh", "pr", "merge"] for c in host.calls)
    if not historical:

        def require_canonical_memory():
            saved = store.get(task["id"])["ci_rounds"]
            assert saved and all(
                r["memory"]["write_receipt"]["state"] == "confirmed" for r in saved
            )

        host.on_merge = require_canonical_memory
        if entrypoint == "api":
            merged = await flow.invoke("merge", task_id=task["id"])
        else:
            code, merged, _, stderr = await run_cli_fifo(
                flow,
                tmp_path,
                "merge",
                dict(task_id=task["id"]),
                pad_hex=14000,
                minimum=8192,
            )
            assert code == 0, (merged, stderr)
        assert merged["phase"] == "settled", merged
        assert len([c for c in host.calls if c[:3] == ["gh", "pr", "merge"]]) == 1
        assert merged["ci_rounds"] == result["ci_rounds"]
        intent = next(
            e["proof"] for e in merged["events"] if e["phase"] == "merge_pending"
        )
        assert intent["authenticated_actor"]["login"] == host.actor
        assert intent["repository"] == policy["repository"]
        assert intent["base"] == policy["base"]
        assert intent["head_sha"] == policy["head_sha"]


@pytest.mark.parametrize("capability", ["limited", "available"])
def test_github_policy_compatibility_normal_merge_exact_head(tmp_path, capability):
    host, lifecycle, pr, policy = setup(tmp_path)
    host.capability = capability
    host.protected = capability == "available"
    lifecycle.validate_host_policy(pr, policy, tmp_path)
    intents = []
    result = lifecycle.merge(
        pr, policy, tmp_path, before_merge=lambda: intents.append(pr["head_sha"])
    )
    assert result["merge_sha"] == host.merge_sha
    assert result["merge_verification"]["ancestry"] == "verified_parent_and_ancestor"
    assert intents == [pr["head_sha"]]
    assert len([c for c in host.calls if c[:3] == ["gh", "pr", "merge"]]) == 1
    lifecycle.merge(pr, policy, tmp_path, reconcile_only=True)
    assert len([c for c in host.calls if c[:3] == ["gh", "pr", "merge"]]) == 1


@pytest.mark.parametrize(
    "change",
    [
        "repo",
        "base",
        "head",
        "merge",
        "parent",
        "ancestry",
        "orphaned_merge",
        "missing_ci",
    ],
)
def test_github_policy_compatibility_historical_identity_rejects(tmp_path, change):
    host, lifecycle, pr, policy = setup(tmp_path)
    host.merged = True
    original = host.__call__

    def transport(argv, cwd):
        raw = original(argv, cwd)
        if argv[:2] != ["gh", "api"]:
            return raw
        data = json.loads(raw)
        path = argv[2]
        if path.endswith("/pulls/1"):
            if change == "repo":
                data["base"]["repo"]["full_name"] = "other/repo"
            if change == "base":
                data["base"]["ref"] = "other"
            if change == "head":
                data["head"]["sha"] = "e" * 40
            if change == "merge":
                data["merge_commit_sha"] = "e" * 40
        if change == "parent" and path.endswith("/commits/" + host.merge_sha):
            data["parents"] = []
        if change == "ancestry" and "/compare/" in path:
            data["merge_base_commit"]["sha"] = "e" * 40
        if change == "orphaned_merge" and "/compare/" + host.merge_sha in path:
            data["merge_base_commit"]["sha"] = "e" * 40
        if change == "missing_ci" and "/check-runs?" in path:
            data.update(total_count=0, check_runs=[])
        return json.dumps(data)

    lifecycle.runner = transport
    if change == "missing_ci":
        assert lifecycle.observe_pr_ci(pr, policy, tmp_path) is None
    else:
        with pytest.raises(
            PublicationError, match="identity_or_ancestry|not_in_current_base"
        ):
            lifecycle.inspect(pr, policy, tmp_path)
    assert all(c[:3] != ["gh", "pr", "merge"] for c in host.calls)


@pytest.mark.parametrize(
    "state,commit,ready",
    [
        ("APPROVED", "current", True),
        ("APPROVED", "stale", False),
        ("DISMISSED", "current", False),
        ("CHANGES_REQUESTED", "current", False),
    ],
)
def test_github_policy_compatibility_historical_reviews_are_real(
    tmp_path, state, commit, ready
):
    host, lifecycle, pr, policy = setup(tmp_path)
    host.merged = True
    policy["required_review"] = True
    original = host.__call__

    def transport(argv, cwd):
        if argv[:2] == ["gh", "api"] and "/reviews?" in argv[2]:
            host.calls.append(copy.deepcopy(argv))
            return json.dumps(
                [
                    dict(
                        id=1,
                        user=dict(id=7, login="reviewer-7"),
                        state=state,
                        commit_id=pr["head_sha"] if commit == "current" else "e" * 40,
                    )
                ]
            )
        return original(argv, cwd)

    lifecycle.runner = transport
    result = lifecycle.observe_pr_ci(pr, policy, tmp_path)
    assert (result["event"]["conclusion"] == "passed") is ready


@pytest.mark.parametrize(
    "kind",
    [
        "rules_truncated",
        "status_malformed",
        "identity_malformed",
        "capability_contradiction",
        "rate403",
        "app_floor",
        "policy_changes",
    ],
)
def test_github_policy_compatibility_discovery_negatives(tmp_path, kind):
    host, lifecycle, pr, policy = setup(tmp_path)
    if kind == "rate403":
        host.rules_error = 403, "API rate limit exceeded for actor"
    if kind in {"rules_truncated", "status_malformed", "app_floor"}:
        host.capability = "available"
    if kind == "rules_truncated":
        host.rules = [dict(type="creation")] * 100
    if kind == "capability_contradiction":
        host.protected = True
    original = host.__call__
    reads = [0]

    def transport(argv, cwd):
        raw = original(argv, cwd)
        if argv[:2] != ["gh", "api"]:
            return raw
        path = argv[2]
        data = json.loads(raw)
        if kind == "status_malformed" and path.endswith("/protection"):
            data["required_status_checks"] = "invalid"
        if kind == "identity_malformed" and path.endswith("/branches/main"):
            data["commit"] = []
        if kind == "app_floor" and path.endswith("/protection"):
            data["required_status_checks"]["checks"] = [
                dict(context=host.required[0], app_id=123)
            ]
        if kind == "policy_changes" and path.endswith("/branches/main"):
            reads[0] += 1
            data["commit"]["sha"] = ("b" if reads[0] == 1 else "c") * 40
        return json.dumps(data)

    lifecycle.runner = transport
    with pytest.raises(Exception) as error:
        lifecycle.observe_pr_ci(pr, policy, tmp_path)
    assert hasattr(error.value, "code") or type(error.value).__name__ == "StateError", (
        repr(error.value)
    )
    assert all(c[:3] != ["gh", "pr", "merge"] for c in host.calls)


@pytest.mark.parametrize("entrypoint", ["api", "cli"])
@pytest.mark.parametrize(
    "review_case",
    [
        "one_of_two",
        "two_of_two",
        "codeowners",
        "last_push",
        "forged_count",
        "settlement_count",
        "settlement_specialized",
    ],
)
async def test_github_policy_compatibility_raw_review_floor_journey(
    flow, monkeypatch, tmp_path, entrypoint, review_case
):
    from test_ci_round_memory import setup as managed_setup
    from test_stdio_fifo import run_cli_fifo

    task, _, (_, policy, original) = await managed_setup(flow, monkeypatch)
    host = ManagedPolicyHost(original.head)
    host.required = policy["required_checks"].copy()
    host.capability = "available"
    host.protected = True
    host.require_review = True
    host.remote_review_count = 2
    host.merged = True
    policy.update(method="merge", required_review=True)
    assert "review_count" not in policy and "specialized_review" not in policy
    host.review_rows = [
        dict(
            id=10 + i,
            user=dict(id=20 + i, login=f"reviewer-{20 + i}"),
            state="APPROVED",
            commit_id=host.head,
        )
        for i in range(2 if review_case == "two_of_two" else 1)
    ]
    if review_case == "forged_count":
        policy.update(review_count=0, specialized_review=False)
    if review_case.startswith("settlement_"):
        host.remote_review_count = 1
        factory = flow.service.managed_service.memory

        def memory():
            adapter = factory()
            finish = adapter.finish

            async def after_memory(*args, **kwargs):
                value = await finish(*args, **kwargs)
                if args[-1].get("summary", "").startswith("Task merged;"):
                    if review_case == "settlement_count":
                        host.remote_review_count = 2
                    else:
                        host.review_params = dict(require_code_owner_reviews=True)
                return value

            adapter.finish = after_memory
            return adapter

        flow.service.managed_service.memory = memory
    specialized = review_case in {"codeowners", "last_push"}
    if specialized:
        host.remote_review_count = 1
        host.review_params = {
            "require_code_owner_reviews"
            if review_case == "codeowners"
            else "require_last_push_approval": True
        }
    lifecycle = RepositoryPRLifecycle(host)
    monkeypatch.setattr("oraculo.pr_lifecycle.RepositoryPRLifecycle", lambda: lifecycle)
    original_call = flow.host.call
    settlement_calls = []

    async def call(name, args):
        if name == "t3_thread_organize":
            settlement_calls.append(args)
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
    payload = dict(task_id=task["id"], policy=policy)
    if entrypoint == "api":
        result = await flow.invoke("pr-check", **payload)
    else:
        code, result, _, stderr = await run_cli_fifo(
            flow, tmp_path, "pr-check", payload, pad_hex=14000, minimum=8192
        )
        assert code == (0 if review_case == "two_of_two" else 1), (result, stderr)
    if review_case.startswith("settlement_"):
        assert result["blocked_reason"] == (
            "ci_round_execution_changed_before_settlement"
            if review_case == "settlement_count"
            else "ci_historical_specialized_review_unproven"
        ), result
        assert result["merged"] and not result["settled"]
        assert result["ci_rounds"][0]["memory"]["write_receipt"]["state"] == "confirmed"
        assert settlement_calls == []
    elif specialized:
        assert (
            result["blocked_reason"] == "ci_historical_specialized_review_unproven"
        ), result
        # The raw outer lifecycle must not certify weaker historical review.
        assert result.get("pr_observation") is None
        assert result["ci_rounds"] == []
    elif review_case in {"one_of_two", "forged_count"}:
        assert result["blocked_reason"] == "historical_merge_ci_evidence_required", (
            result
        )
        assert "pr_review_pending" in result["pr_observation"]["receipt"]["blockers"]
    else:
        assert result["phase"] == "settled", result
        assert result["merged"]
        assert result["pr_observation"]["receipt"]["blockers"] == []
        assert (
            result["merge_receipt"]["merge_verification"]["review_floor"][
                "remote_review_count"
            ]
            == 2
        )
    assert all(c[:3] != ["gh", "pr", "merge"] for c in host.calls)
    assert not result.get("merge_attempted")


@pytest.mark.parametrize("entrypoint", ["api", "cli"])
async def test_github_policy_compatibility_sequential_external_merges(
    flow, monkeypatch, tmp_path, entrypoint
):
    from pathlib import Path

    from test_ci_round_memory import publish
    from test_ci_round_memory import setup as managed_setup
    from test_review_product_acceptance import implementation
    from test_stdio_fifo import run_cli_fifo

    first, _, (_, policy1, source1) = await managed_setup(flow, monkeypatch)
    second, evidence2, _ = await implementation(flow, key="second-original")
    flow.host.complete(second["child"]["childThreadId"])
    flow.host.caller = "origin"
    assert (await flow.invoke("resume", task_id=second["id"]))[
        "work_status"
    ] == "succeeded"
    published2, policy2, source2 = await publish(
        flow, monkeypatch, second, evidence2, "public-second", number=13
    )
    assert published2["publication"]["number"] == 13
    hosts = [ManagedPolicyHost(source1.head), ManagedPolicyHost(source2.head)]
    hosts[1].pr_number, hosts[1].run_offset = 13, 10
    hosts[1].merge_parent_base = hosts[0].merge_sha
    hosts[1].merge_sha = "f" * 40
    for host, policy in zip(hosts, (policy1, policy2), strict=True):
        host.required = policy["required_checks"].copy()
        host.merged = True
        host.base = hosts[1].merge_sha  # main already advanced beyond first merge
        host.current_base_head = host.base
        policy["method"] = "merge"
    store = flow.service.managed_service.store
    before = {t["id"]: copy.deepcopy(store.get(t["id"])) for t in (first, second)}
    roots = {
        Path(store.get(t["id"])["publication_spec"]["cwd"]): host
        for t, host in zip((first, second), hosts, strict=True)
    }

    def transport(argv, cwd):
        return roots[Path(cwd)](argv, cwd)

    lifecycle = RepositoryPRLifecycle(transport)
    monkeypatch.setattr("oraculo.pr_lifecycle.RepositoryPRLifecycle", lambda: lifecycle)
    original_call = flow.host.call

    async def call(name, args):
        if name == "t3_thread_organize":
            flow.host.threads[args["threadId"]]["settled"] = True
            return dict(accepted=True)
        value = await original_call(name, args)
        if name == "list_thread_pull_requests":
            number = 12 if args["threadId"] == first["child"]["childThreadId"] else 13
            value["pullRequests"] = [
                dict(
                    host="github.com",
                    repository=policy1["repository"],
                    number=number,
                    state="merged",
                    isDraft=False,
                    baseBranch="main",
                )
            ]
        return value

    flow.host.call = call
    results = []
    for task, policy, host in zip(
        (first, second), (policy1, policy2), hosts, strict=True
    ):
        payload = dict(task_id=task["id"], policy=policy)
        if entrypoint == "api":
            result = await flow.invoke("pr-check", **payload)
        else:
            code, result, _, stderr = await run_cli_fifo(
                flow, tmp_path, "pr-check", payload, pad_hex=14000, minimum=8192
            )
            assert code == 0, (result, stderr)
        assert result["phase"] == "settled", result
        assert result["merge_receipt"]["head_sha"] == host.head
        assert result["merge_receipt"]["merge_sha"] == host.merge_sha
        assert not result.get("merge_attempted")
        round1 = result["ci_rounds"][0]
        assert (
            round1["facts"]["identity"]["observation_timing"] == "after_external_merge"
        )
        assert round1["facts"]["identity"]["merge_sha"] == host.merge_sha
        assert round1["facts"]["identity"]["observed_runs"] == [
            dict(run=str(42 + getattr(host, "run_offset", 0)), attempt=1)
        ]
        for key in ("request", "report", "submitted_report", "memory", "child"):
            assert store.get(task["id"])[key] == before[task["id"]][key]
        results.append(result)
    assert results[0]["id"] != results[1]["id"]
    assert results[0]["ci_rounds"][0]["id"] != results[1]["ci_rounds"][0]["id"]
    assert results[0]["memory"]["id"] != results[1]["memory"]["id"]
    assert all(c[:3] != ["gh", "pr", "merge"] for host in hosts for c in host.calls)


@pytest.mark.parametrize("historical", [False, True])
def test_github_policy_compatibility_metadata_reads_never_persist_source_patches(
    tmp_path, historical
):
    host, lifecycle, pr, policy = setup(tmp_path)
    host.merged = historical
    original = host.__call__
    fragment = "unselected-fixture-source-fragment"

    def metadata_transport(argv, cwd):
        raw = original(argv, cwd)
        if argv[:2] != ["gh", "api"] or not (
            "/commits/" in argv[2] or "/compare/" in argv[2]
        ):
            return raw
        value = json.loads(raw)
        value["files"] = [dict(patch=fragment)]
        if "--jq" in argv:
            # Simulate gh's documented projection at its process boundary.
            if "/compare/" in argv[2]:
                value = {k: value[k] for k in ("status", "merge_base_commit")}
            elif "parents" in value:
                value = {k: value[k] for k in ("sha", "parents")}
            else:
                value = dict(sha=value["sha"])
        return json.dumps(value)

    lifecycle.runner = metadata_transport
    policy_proof = lifecycle.validate_host_policy(pr, policy, tmp_path)
    receipt = lifecycle.inspect(pr, policy, tmp_path)
    assert fragment not in json.dumps([policy_proof, receipt])
    reads = [
        c
        for c in host.calls
        if c[:2] == ["gh", "api"] and ("/commits/" in c[2] or "/compare/" in c[2])
    ]
    assert reads and all("--jq" in c for c in reads)
    assert all("/git/commits/" in c[2] or "/compare/" in c[2] for c in reads)


@pytest.mark.parametrize(
    "status,message,code",
    [
        (403, "API rate limit exceeded for actor", "rate_limited"),
        (403, "You have exceeded a secondary rate limit", "rate_limited"),
        (401, "Bad credentials", "authentication_failed"),
        (500, "Server error", "server_error"),
    ],
)
def test_github_policy_compatibility_projected_error_retains_typed_http_diagnostic(
    tmp_path, status, message, code
):
    from oraculo.github_policy import GitHubReader

    def denied(argv, cwd):
        assert "--jq" in argv
        raise subprocess.CalledProcessError(
            1, argv, output="", stderr=f"gh: {message} (HTTP {status})"
        )

    with pytest.raises(PublicationError, match=code) as error:
        GitHubReader(denied, tmp_path).read("user")
    assert error.value.observations[-1]["stdout"] == ""
    assert error.value.observations[-1]["http_status"] == status


@pytest.mark.parametrize("classic_status", [404, 403])
@pytest.mark.parametrize(
    "case", ["satisfied", "missing_check", "missing_review", "no_rules"]
)
def test_github_policy_compatibility_ruleset_only_floor(tmp_path, classic_status, case):
    host, lifecycle, pr, policy = setup(tmp_path)
    host.protected = True  # GitHub's flag includes active rulesets.
    host.capability = "available"
    host.protection_error = (
        classic_status,
        "Branch not protected" if classic_status == 404 else LIMIT,
    )
    host.rules = [
        dict(
            type="required_status_checks",
            parameters=dict(required_status_checks=[dict(context=host.required[0])]),
        ),
        dict(type="pull_request", parameters=dict(required_approving_review_count=1)),
    ]
    policy["required_review"] = True
    if case == "missing_check":
        policy["required_checks"].remove(host.required[0])
    if case == "missing_review":
        policy["required_review"] = False
    if case == "no_rules":
        host.rules = []
    if case == "satisfied":
        proof = lifecycle.validate_host_policy(pr, policy, tmp_path)
        assert proof["remote_capability"]["state"] == "enforced"
        assert proof["remote_required_checks"] == [host.required[0]]
        assert proof["review_count"] == 1 and proof["remote_required_review"]
    else:
        with pytest.raises(
            PublicationError,
            match="contradictory"
            if case == "no_rules"
            else "actual_host_policy_not_satisfied",
        ):
            lifecycle.validate_host_policy(pr, policy, tmp_path)


@pytest.mark.parametrize(
    "endpoint", ["/protection", "/rules/branches/main?per_page=100"]
)
def test_github_policy_compatibility_successful_null_policy_is_malformed(
    tmp_path, endpoint
):
    host, lifecycle, pr, policy = setup(tmp_path)
    host.capability = "available"
    original = host.__call__

    def null_response(argv, cwd):
        if argv[:2] == ["gh", "api"] and argv[2].endswith(endpoint):
            return "null"
        return original(argv, cwd)

    lifecycle.runner = null_response
    with pytest.raises(PublicationError, match="policy_invalid"):
        lifecycle.validate_host_policy(pr, policy, tmp_path)


@pytest.mark.parametrize("entrypoint", ["api", "cli"])
@pytest.mark.parametrize("classic_status", [403, 404])
async def test_github_policy_compatibility_ruleset_only_journey(
    flow, monkeypatch, tmp_path, entrypoint, classic_status
):
    from test_ci_round_memory import setup as managed_setup
    from test_stdio_fifo import run_cli_fifo

    task, _, (_, policy, original_host) = await managed_setup(flow, monkeypatch)
    host = ManagedPolicyHost(original_host.head)
    host.required = policy["required_checks"].copy()
    host.merged = True
    host.protected = True
    host.capability = "available"
    host.protection_error = (
        classic_status,
        LIMIT if classic_status == 403 else "Branch not protected",
    )
    host.rules = [
        dict(
            type="required_status_checks",
            parameters=dict(
                required_status_checks=[dict(context=c) for c in host.required]
            ),
        )
    ]
    policy["method"] = "merge"
    lifecycle = RepositoryPRLifecycle(host)
    monkeypatch.setattr("oraculo.pr_lifecycle.RepositoryPRLifecycle", lambda: lifecycle)
    original_call = flow.host.call

    async def call(name, args):
        if name == "t3_thread_organize":
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
    if entrypoint == "api":
        result = await flow.invoke("pr-check", task_id=task["id"], policy=policy)
    else:
        code, result, _, stderr = await run_cli_fifo(
            flow,
            tmp_path,
            "pr-check",
            dict(task_id=task["id"], policy=policy),
            pad_hex=14000,
            minimum=8192,
        )
        assert code == 0, (result, stderr)
    assert result["phase"] == "settled", result
    assert (
        result["pr_observation"]["receipt"]["observed_host_policy"][
            "remote_capability"
        ]["state"]
        == "enforced"
    )
    assert result["ci_rounds"][0]["memory"]["write_receipt"]["state"] == "confirmed"
    assert all(c[:3] != ["gh", "pr", "merge"] for c in host.calls)


@pytest.mark.parametrize(
    "permission", ["write", "read", "none", "triage", "wrong_user", "denied"]
)
def test_github_policy_compatibility_historical_approval_requires_repository_write(
    tmp_path, permission
):
    host, lifecycle, pr, policy = setup(tmp_path)
    host.merged = True
    policy["required_review"] = True
    host.review_rows = [
        dict(
            id=1,
            user=dict(id=7, login="example-reviewer"),
            state="APPROVED",
            commit_id=host.head,
        )
    ]
    original = host.__call__

    def transport(argv, cwd):
        if argv[:2] == ["gh", "api"] and "/collaborators/" in argv[2]:
            if permission == "denied":
                raise subprocess.CalledProcessError(
                    1,
                    argv,
                    output=json.dumps(dict(status="403", message="Forbidden")),
                    stderr="gh: Forbidden (HTTP 403)",
                )
            return json.dumps(
                dict(
                    permission="write" if permission == "wrong_user" else permission,
                    user=dict(
                        id=8 if permission == "wrong_user" else 7,
                        login="example-reviewer",
                    ),
                )
            )
        return original(argv, cwd)

    lifecycle.runner = transport
    if permission in {"denied", "wrong_user"}:
        with pytest.raises(PublicationError):
            lifecycle.observe_pr_ci(pr, policy, tmp_path)
    else:
        proof = lifecycle.observe_pr_ci(pr, policy, tmp_path)
        assert (proof["event"]["conclusion"] == "passed") is (permission == "write")
    assert all(c[:3] != ["gh", "pr", "merge"] for c in host.calls)


@pytest.mark.parametrize("entrypoint", ["api", "cli"])
@pytest.mark.parametrize("workflow", ["cancelled", "failure", "pending"])
async def test_github_policy_compatibility_green_checks_do_not_waive_workflow_outcome(
    flow, monkeypatch, tmp_path, entrypoint, workflow
):
    from test_ci_round_memory import setup as managed_setup
    from test_stdio_fifo import run_cli_fifo

    task, _, (_, policy, original_host) = await managed_setup(flow, monkeypatch)
    host = ManagedPolicyHost(original_host.head)
    host.required = policy["required_checks"].copy()
    host.conclusion = "success" if workflow == "pending" else workflow
    policy["method"] = "merge"
    original = host.__call__
    pending = [workflow == "pending"]

    def transport(argv, cwd):
        raw = original(argv, cwd)
        if (
            pending[0]
            and argv[:2] == ["gh", "api"]
            and (
                argv[2].endswith("/actions/runs/42")
                or argv[2].endswith("/actions/runs/43")
            )
        ):
            data = json.loads(raw)
            data.update(status="in_progress", conclusion=None)
            return json.dumps(data)
        return raw

    lifecycle = RepositoryPRLifecycle(transport)
    monkeypatch.setattr("oraculo.pr_lifecycle.RepositoryPRLifecycle", lambda: lifecycle)
    original_task = copy.deepcopy(flow.service.managed_service.store.get(task["id"]))
    payload = dict(task_id=task["id"], policy=policy)
    if entrypoint == "api":
        result = await flow.invoke("merge", **payload)
    else:
        code, result, _, stderr = await run_cli_fifo(
            flow, tmp_path, "merge", payload, pad_hex=14000, minimum=8192
        )
        assert code == 0, (result, stderr)
    assert all(c[:3] != ["gh", "pr", "merge"] for c in host.calls), result
    assert not result["merged"] and not result.get("merge_attempted")
    assert result["phase"] == "pr_waiting", result
    if workflow == "pending":
        assert result["ci_rounds"] == []
    else:
        round1 = flow.service.managed_service.store.get(task["id"])["ci_rounds"][0]
        assert round1["work_status"] == "failed"
        assert all(c["conclusion"] == "SUCCESS" for c in round1["facts"]["checks"])
        assert round1["memory"]["write_receipt"]["state"] == "confirmed"
    for key in ("request", "report", "submitted_report", "memory", "child"):
        assert (
            flow.service.managed_service.store.get(task["id"])[key]
            == original_task[key]
        )


async def test_github_policy_compatibility_review_readiness_does_not_rewrite_successful_ci_round(
    flow, monkeypatch
):
    from test_ci_round_memory import setup as managed_setup

    task, _, (_, policy, source) = await managed_setup(flow, monkeypatch)
    host = ManagedPolicyHost(source.head)
    host.required = policy["required_checks"].copy()
    host.review = "REVIEW_REQUIRED"
    policy.update(method="merge", required_review=True)
    lifecycle = RepositoryPRLifecycle(host)
    monkeypatch.setattr("oraculo.pr_lifecycle.RepositoryPRLifecycle", lambda: lifecycle)
    first = await flow.invoke("pr-check", task_id=task["id"], policy=policy)
    assert first["phase"] == "pr_waiting", first
    saved = copy.deepcopy(
        flow.service.managed_service.store.get(task["id"])["ci_rounds"]
    )
    assert saved[0]["work_status"] == "succeeded"
    assert (
        saved[0]["report"]["evidence"]["execution"]["event"]["conclusion"] == "failed"
    )
    host.review = "APPROVED"
    host.review_rows = [
        dict(
            id=1,
            user=dict(id=7, login="reviewer-7"),
            state="APPROVED",
            commit_id=host.head,
        )
    ]
    original = flow.host.call

    async def call(name, args):
        if name == "t3_thread_organize":
            flow.host.threads[args["threadId"]]["settled"] = True
            return dict(accepted=True)
        value = await original(name, args)
        if name == "list_thread_pull_requests" and host.merged:
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
    result = await flow.invoke("merge", task_id=task["id"])
    assert result["phase"] == "settled", result
    assert flow.service.managed_service.store.get(task["id"])["ci_rounds"] == saved
    assert len([c for c in host.calls if c[:3] == ["gh", "pr", "merge"]]) == 1
