"""Laboratory: actual gh/REST adapter code, mocked host transports, isolated Git/socket."""

import copy
import json

import pytest
from test_managed_tasks import git, initialize
from test_service import command

from oraculo.ci_events import CIBinding, CIEvents
from oraculo.ci_host import RepositoryCIObserver
from oraculo.pr_lifecycle import RepositoryPRLifecycle
from oraculo.protocol import content_hash
from oraculo.publication import PublicationError, command_runner


class GitHubCIHost:
    def __init__(self, head):
        self.calls = []
        self.head = head
        self.base = "b" * 40
        self.conclusion = "success"
        self.attempt = 1
        self.required = ["Linux full", "macOS full", "orchestration offline"]
        self.review = "APPROVED"
        self.mergeable = True
        self.require_review = True
        self.truncated = False
        self.remote = "example/repo"

    def __call__(self, argv, cwd):
        self.calls.append(copy.deepcopy(argv))
        if argv[0] != "gh":
            return command_runner(argv, cwd)
        if argv[1:3] == ["pr", "view"]:
            return json.dumps(
                dict(
                    number=1,
                    url="https://github.com/example/repo/pull/1",
                    headRefOid=self.head,
                    baseRefName="main",
                    state="OPEN",
                    isDraft=False,
                    reviewDecision=self.review,
                    mergeable="MERGEABLE" if self.mergeable else "CONFLICTING",
                    mergeStateStatus="CLEAN",
                    statusCheckRollup=[
                        dict(
                            __typename="CheckRun",
                            name=c,
                            status="COMPLETED",
                            conclusion="SUCCESS",
                        )
                        for c in self.required
                    ],
                    mergeCommit=None,
                )
            )
        assert argv[1] == "api", argv
        path = argv[2]
        assert "repos/example/repo" in path or path == "user"
        if path == "user":
            return json.dumps(dict(login="example-actor", id=7))
        if path == "repos/example/repo":
            return json.dumps(dict(full_name="example/repo", id=11, private=False))
        if path.endswith("/branches/main"):
            return json.dumps(
                dict(name="main", protected=True, commit=dict(sha=self.base))
            )
        if path.endswith("/commits/" + self.head):
            return json.dumps(dict(sha=self.head))
        if path.endswith("/pulls/1"):
            value = dict(
                number=1,
                head=dict(sha=self.head),
                base=dict(
                    sha=self.base, ref="main", repo=dict(full_name="example/repo")
                ),
                state="open",
                mergeable=self.mergeable,
            )
        elif path.endswith(
            f"/commits/{self.head}/check-runs?filter=latest&per_page=100"
        ):
            value = dict(
                total_count=len(self.required),
                check_runs=[
                    dict(
                        id=100 + i,
                        name=c,
                        head_sha=self.head,
                        app=dict(slug="github-actions"),
                        check_suite=dict(id=142 if i < 2 else 143),
                        status="completed",
                        conclusion="success",
                    )
                    for i, c in enumerate(self.required)
                ],
            )
        elif path.endswith(f"/actions/runs?head_sha={self.head}&per_page=100"):
            value = dict(
                total_count=2,
                workflow_runs=[
                    dict(id=i, check_suite_id=100 + i, run_attempt=self.attempt)
                    for i in (42, 43)
                ],
            )
        elif path.endswith("/actions/runs/42") or path.endswith("/actions/runs/43"):
            value = dict(
                id=int(path.rsplit("/", 1)[-1]),
                check_suite_id=100 + int(path.rsplit("/", 1)[-1]),
                head_sha=self.head,
                run_attempt=self.attempt,
                repository=dict(full_name="example/repo"),
                pull_requests=[dict(number=1)],
                updated_at="2026-11-03T10:00:00Z",
                status="completed",
                conclusion=self.conclusion,
            )
        elif path.endswith("/protection"):
            value = dict(
                required_status_checks=dict(contexts=self.required, checks=[]),
                required_pull_request_reviews=dict(
                    required_approving_review_count=int(self.require_review)
                ),
            )
        elif path.endswith("/rules/branches/main?per_page=100"):
            value = []
        elif "/attempts/1/jobs?per_page=100" in path:
            rows = [
                dict(
                    name=c,
                    status="completed",
                    conclusion="success",
                    head_sha=self.head,
                    run_id=int(path.split("/runs/")[1].split("/")[0]),
                    check_run_url=f"https://api.github.com/repos/example/repo/check-runs/{100 + i}",
                    run_attempt=self.attempt,
                )
                for i, c in enumerate(self.required)
                if int(path.split("/runs/")[1].split("/")[0]) == (42 if i < 2 else 43)
            ]
            value = dict(total_count=len(rows) + int(self.truncated), jobs=rows)
        else:
            raise AssertionError(argv)
        return json.dumps(value)


def setup(service):
    root = service.config.projects_root / "alpha"
    initialize(root)
    git(root, "remote", "add", "public-safe", "https://github.com/example/repo.git")
    head = git(root, "rev-parse", "HEAD")
    host = GitHubCIHost(head)
    service.ci_observer = RepositoryCIObserver(service, RepositoryPRLifecycle(host))
    binding = CIBinding(
        repository="example/repo", pr=1, head=head, base=host.base, run="42", attempt=1
    )
    obj = service.state.submit_objective("alpha", "Observe exact CI")
    job = service.state.enqueue_job(
        obj["id"], "implementation", {"ci_binding": binding.model_dump()}
    )
    return root, host, binding, job, obj


async def observe(service, job):
    return await command(
        service,
        "ci.observe",
        {"job_id": job["id"]},
        origin={"thread_id": "coordinator", "run_id": "run"},
    )


async def test_service_installs_real_adapter_and_public_observation_opens_exact_gate(
    running_service,
):
    service = running_service
    assert isinstance(service.ci_observer, RepositoryCIObserver)
    _, host, binding, job, _ = setup(service)
    assert service.state.admission_blockers(job)[0]["reason"] == "ci_evidence_pending"
    result = await observe(service, job)
    assert result["ok"] and result["data"]["event"]["conclusion"] == "passed", result
    assert result["data"]["host_evidence"]["source"] == "authenticated_gh_api"
    assert git(service.config.projects_root / "alpha", "remote") == "public-safe"
    assert result["data"]["host_evidence"]["remote_binding"]["remote"] == "public-safe"
    assert len(result["data"]["host_evidence"]["policy"]["required_checks"]) == 3
    assert service.ci_events.passed("alpha", binding.model_dump())
    assert service.state.acquire_grant(
        job["id"], "codex", "current", "coordinator", "run"
    )
    assert all(call[1] in {"api", "pr"} for call in host.calls if call[0] == "gh")
    assert service.state.schema_version() == 7


@pytest.mark.parametrize(
    "change",
    [
        "attempt",
        "head",
        "base",
        "remote",
        "policy",
        "review",
        "conflict",
        "truncated",
        "cancel",
    ],
)
async def test_real_adapter_rejects_stale_bindings_unknown_policy_and_incomplete_ci(
    running_service, change
):
    s = running_service
    root, host, binding, job, _ = setup(s)
    assert (await observe(s, job))["data"]["event"]["conclusion"] == "passed"
    if change == "attempt":
        host.attempt = 2
    if change == "head":
        host.head = "c" * 40
    if change == "base":
        host.base = "c" * 40
    if change == "remote":
        git(
            root,
            "remote",
            "set-url",
            "public-safe",
            "https://github.com/other/repo.git",
        )
    if change == "policy":
        host.required = []
    if change == "review":
        host.review = "REVIEW_REQUIRED"
    if change == "conflict":
        host.mergeable = False
    if change == "truncated":
        host.truncated = True
    if change == "cancel":
        host.conclusion = "cancelled"
    result = await observe(s, job)
    assert (
        not result["ok"]
        or result["data"]["disposition"] != "accepted"
        or result["data"]["event"]["conclusion"] != "passed"
    ), result
    # N-ci-policy-1: review/mergeability at the same run stamp stays pending.
    # It must not demote an accepted cursor to unverified.
    if change in {"review", "conflict"}:
        assert result["ok"], result
        assert result["data"]["event"]["conclusion"] == "passed"
        assert result["data"]["disposition"] == "readiness_pending"
        cursor = s.state.get_meta(CIEvents.key("alpha", binding))
        assert cursor["disposition"] == "readiness_pending"
    assert not s.ci_events.passed("alpha", binding.model_dump())
    assert s.state.acquire_grant(job["id"], "codex", "current", "owner", "run") is None


async def test_n_ci_policy_1_review_pending_then_approval_accepts_same_sequence(
    running_service,
):
    """N-ci-policy-1: approval after a pending review must not need a new run."""
    service = running_service
    _, host, binding, job, _ = setup(service)
    host.review = "REVIEW_REQUIRED"
    first = await observe(service, job)
    assert first["ok"], first
    assert first["data"]["event"]["conclusion"] == "passed"
    assert first["data"]["disposition"] == "readiness_pending"
    assert not service.ci_events.passed("alpha", binding.model_dump())
    assert (
        service.state.admission_blockers(job)[0]["reason"] == "ci_evidence_pending"
    )
    host.review = "APPROVED"
    second = await observe(service, job)
    assert second["ok"], second
    assert second["data"]["disposition"] == "accepted"
    assert second["data"]["event"]["conclusion"] == "passed"
    assert second["data"]["event"]["sequence"] == first["data"]["event"]["sequence"]
    assert second["data"]["event"]["event_id"] != first["data"]["event"]["event_id"]
    assert service.ci_events.passed("alpha", binding.model_dump())
    host.review = "REVIEW_REQUIRED"
    drifted = await observe(service, job)
    assert drifted["data"]["disposition"] == "readiness_pending"
    cursor = service.state.get_meta(CIEvents.key("alpha", binding))
    assert cursor["disposition"] == "readiness_pending"
    assert not service.ci_events.passed("alpha", binding.model_dump())
    host.review = "APPROVED"
    restored = await observe(service, job)
    assert restored["data"]["disposition"] == "accepted"
    assert service.ci_events.passed("alpha", binding.model_dump())


async def test_host_replay_is_idempotent_and_cancelled_job_never_admits(
    running_service,
):
    s = running_service
    _, _, binding, job, obj = setup(s)
    one = await observe(s, job)
    two = await observe(s, job)
    assert (
        one["data"]["event_hash"] == two["data"]["event_hash"] and two["data"]["replay"]
    )
    assert s.state.connection.execute("SELECT count(*) FROM outbox").fetchone()[0] == 1
    s.state.cancel_objective(obj["id"])
    await observe(s, job)
    assert s.state.acquire_grant(job["id"], "codex", "current", "owner", "run") is None


async def test_fresh_host_read_can_refresh_ttl_but_old_events_cannot_reopen_new_cursor(
    running_service,
):
    s = running_service
    _, _, binding, job, _ = setup(s)
    await observe(s, job)
    key = CIEvents.key("alpha", binding)
    old = s.state.get_meta(key)
    s.state.set_meta(key, dict(old, observed_at=0))
    assert not s.ci_events.passed("alpha", binding.model_dump())
    assert (await observe(s, job))["data"]["replay"]
    assert s.ci_events.passed("alpha", binding.model_dump())
    s.state.set_meta(key, dict(old, event_hash="f" * 64))
    assert (await observe(s, job))["data"]["disposition"] == "stale"


async def test_ci_outcome_requires_service_host_proof_at_exact_clean_sha(
    running_service,
):
    s = running_service
    root, host, binding, original_job, _ = setup(s)
    # CI verification is a criterion, rather than a gate preventing the verifier
    # itself from receiving its grant.
    spec = dict(
        id="verify",
        owner="lab",
        brief="Verify observed CI",
        criteria=[
            dict(
                id="ci",
                description="CI passed",
                outcome=dict(
                    version=2,
                    kind="ci",
                    commit=binding.head,
                    ci_binding=binding.model_dump(),
                ),
            )
        ],
    )
    plan = s.plans.submit(
        "alpha", dict(text="CI proof", tasks=[spec]), "ci-outcome", s.config.profiles
    )
    job_id = plan["tasks"][0]["job_id"]
    attempt = s.state.acquire_grant(
        job_id, "codex", "current", "coordinator", "owner-run"
    )
    s.state.claim_grant(
        attempt["id"], attempt["generation"], "executor", "executor-run"
    )
    result = await command(
        s,
        "outcome.verify",
        dict(
            attempt_id=attempt["id"],
            generation=attempt["generation"],
            criterion_id="ci",
            cwd=str(root),
        ),
        origin=dict(thread_id="executor", run_id="executor-run"),
    )
    assert result["ok"] and result["data"]["status"] == "passed", result
    receipt = result["data"]
    assert receipt["verifier"] == "service-ci-v1" and "command" not in receipt
    assert s.outcomes.satisfied(attempt, "ci", content_hash(receipt))
    host.attempt = 2
    await observe(s, original_job)
    assert not s.outcomes.satisfied(attempt, "ci")


def test_observation_only_policy_never_authorizes_merge(tmp_path):
    host = GitHubCIHost("a" * 40)
    lifecycle = RepositoryPRLifecycle(host)
    pr = dict(
        host="github",
        repository="example/repo",
        number=1,
        base="main",
        head_sha="a" * 40,
        url="https://github.com/example/repo/pull/1",
    )
    policy = dict(
        repository="example/repo",
        base="main",
        head_sha="a" * 40,
        required_checks=[],
        required_review=False,
        observe_only=True,
        merge_authorized=False,
        method="merge",
    )
    with pytest.raises(PublicationError, match="not_authorized"):
        lifecycle.merge(pr, policy, tmp_path)
    assert not host.calls


@pytest.mark.parametrize("other", ["wrong", "alias", "wrong_only", "explicit_wrong"])
async def test_real_git_remote_selection_never_guesses_or_changes_remotes(
    running_service, other
):
    s = running_service
    root, host, binding, job, _ = setup(s)
    if other == "wrong_only":
        git(
            root,
            "remote",
            "set-url",
            "public-safe",
            "https://github.com/other/repo.git",
        )
    else:
        git(
            root,
            "remote",
            "add",
            "another",
            "https://github.com/example/repo.git"
            if other == "alias"
            else "https://github.com/other/repo.git",
        )
    before = git(root, "remote", "-v")
    if other == "explicit_wrong":
        policy = dict(
            repository=binding.repository,
            base="main",
            head_sha=binding.head,
            required_checks=host.required,
            required_review=True,
            allow_no_checks=False,
        )
        with pytest.raises(PublicationError, match="repository_mismatch"):
            s.ci_observer.lifecycle.validate_host_policy(
                dict(
                    host="github",
                    repository=binding.repository,
                    base="main",
                    head_sha=binding.head,
                    remote="another",
                ),
                policy,
                root,
            )
    else:
        result = await observe(s, job)
        assert result["ok"] is (other == "wrong"), result
        if other != "wrong":
            assert not s.ci_events.passed("alpha", binding.model_dump())
    assert git(root, "remote", "-v") == before


async def test_host_policy_validates_the_same_unique_public_safe_remote(
    running_service,
):
    s = running_service
    root, host, binding, _, _ = setup(s)
    policy = dict(
        repository=binding.repository,
        base="main",
        head_sha=binding.head,
        required_checks=host.required,
        required_review=True,
        allow_no_checks=False,
    )
    pr = dict(
        host="github", repository=binding.repository, base="main", head_sha=binding.head
    )
    observed = s.ci_observer.lifecycle.validate_host_policy(pr, policy, root)
    assert observed["policy_source"] == "authenticated_github_protection_and_rules"
    git(root, "remote", "add", "alias", "https://github.com/example/repo.git")
    with pytest.raises(PublicationError, match="remote_ambiguous"):
        s.ci_observer.lifecycle.validate_host_policy(pr, policy, root)
