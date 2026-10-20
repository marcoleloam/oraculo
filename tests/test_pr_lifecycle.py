"""Fake GitHub observations only; no real host mutation or local Git writes."""

import copy
import json
import subprocess

import pytest

from oraculo.pr_lifecycle import GitHubPRLifecycle
from oraculo.publication import PublicationError


def pr():
    return {
        "host": "github",
        "repository": "example/project",
        "number": 17,
        "url": "https://github.com/example/project/pull/17",
        "base": "main",
        "head_sha": "a" * 40,
        "state": "open",
    }


def policy(**overrides):
    return {
        "repository": "example/project",
        "base": "main",
        "head_sha": "a" * 40,
        "merge_authorized": True,
        "required_checks": ["tests", "lint"],
        "required_review": True,
        "method": "squash",
        **overrides,
    }


def check(name, *, status="COMPLETED", conclusion="SUCCESS"):
    return {
        "__typename": "CheckRun",
        "name": name,
        "status": status,
        "conclusion": conclusion,
    }


class Host:
    def __init__(self):
        self.observed = {
            "number": 17,
            "url": pr()["url"],
            "headRefOid": "a" * 40,
            "baseRefName": "main",
            "state": "OPEN",
            "isDraft": False,
            "reviewDecision": "APPROVED",
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "CLEAN",
            "statusCheckRollup": [check("tests"), check("lint")],
            "mergeCommit": None,
        }
        self.calls = []
        self.merge_calls = 0
        self.accept_merge = True
        self.lose_response = False
        self.fail_read_after_merge = False
        self.changed_head_after_merge = False

    def __call__(self, argv, cwd):
        self.calls.append((copy.deepcopy(argv), cwd))
        assert argv[argv.index("--repo") + 1] == "github.com/example/project"
        assert argv[3] == "17"
        if argv[:3] == ["gh", "pr", "view"]:
            if self.fail_read_after_merge and self.merge_calls:
                raise subprocess.CalledProcessError(
                    1, argv, stderr="private host detail"
                )
            return json.dumps(self.observed)
        assert argv[:3] == ["gh", "pr", "merge"]
        assert (
            "--admin" not in argv
            and "--auto" not in argv
            and "--delete-branch" not in argv
        )
        assert argv[argv.index("--match-head-commit") + 1] == "a" * 40
        self.merge_calls += 1
        if self.accept_merge:
            self.observed.update(state="MERGED", mergeCommit={"oid": "b" * 40})
        if self.changed_head_after_merge:
            self.observed["headRefOid"] = "c" * 40
        if self.lose_response:
            raise subprocess.TimeoutExpired(argv, 60, output="private host detail")
        return "request accepted"


def test_inspect_reports_bound_readiness_without_mutation(tmp_path):
    host = Host()
    observed = GitHubPRLifecycle(host).inspect(pr(), policy(), tmp_path)
    assert observed["ready"] is True and observed["blockers"] == []
    assert observed["state"] == "open"
    assert observed["head_sha"] == "a" * 40 and observed["base"] == "main"
    assert observed["merge_sha"] is None
    assert len(observed["policy_sha256"]) == 64
    assert len(observed["checks"]) == 2
    assert host.merge_calls == 0


@pytest.mark.parametrize("method", ["squash", "merge", "rebase"])
def test_authorized_merge_persists_intent_and_observes_merged_commit(tmp_path, method):
    host = Host()
    events = []

    def runner(argv, cwd):
        if argv[:3] == ["gh", "pr", "merge"]:
            assert events == ["intent"]
        return host(argv, cwd)

    receipt = GitHubPRLifecycle(runner).merge(
        pr(),
        policy(method=method),
        tmp_path,
        before_merge=lambda: events.append("intent"),
    )
    assert receipt["state"] == "merged" and receipt["merge_sha"] == "b" * 40
    assert receipt["head_sha"] == "a" * 40 and receipt["base"] == "main"
    assert host.merge_calls == 1
    command = next(a for a, _ in host.calls if a[:3] == ["gh", "pr", "merge"])
    assert "--" + method in command
    assert host.calls[-1][0][:3] == ["gh", "pr", "view"]
    assert all(cwd == tmp_path for _, cwd in host.calls)


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("merge_authorized", False, "pr_merge_not_authorized"),
        ("merge_authorized", "true", "pr_merge_not_authorized"),
        ("required_checks", [], "pr_required_checks_unspecified"),
        ("required_checks", ["tests", "tests"], "pr_merge_policy_invalid"),
        ("required_checks", None, "pr_merge_policy_invalid"),
        ("required_checks", [123], "pr_merge_policy_invalid"),
        ("required_review", None, "pr_merge_policy_invalid"),
        ("method", "admin", "pr_merge_policy_invalid"),
        ("allowed_merge_states", ["DIRTY"], "pr_merge_policy_invalid"),
        ("allowed_merge_states", [], "pr_merge_policy_invalid"),
        ("repository", "https://token@example.org/project", "pr_merge_policy_invalid"),
        ("head_sha", "HEAD", "pr_merge_policy_invalid"),
    ],
)
def test_invalid_policy_does_not_observe_or_emit(tmp_path, field, value, code):
    host = Host()
    with pytest.raises(PublicationError, match=code) as caught:
        GitHubPRLifecycle(host).merge(pr(), policy(**{field: value}), tmp_path)
    assert caught.value.emitted is False
    assert host.calls == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("repository", "another/project"),
        ("base", "release"),
        ("head_sha", "c" * 40),
        ("url", "https://example.org/17"),
        ("number", 18),
        ("host", "azure"),
    ],
)
def test_pr_receipt_must_match_authorized_binding(tmp_path, field, value):
    host = Host()
    bound = {**pr(), field: value}
    with pytest.raises(PublicationError, match="pr_policy_binding_mismatch"):
        GitHubPRLifecycle(host).merge(bound, policy(), tmp_path)
    assert host.calls == []


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("number", 18, "pr_remote_identity_mismatch"),
        (
            "url",
            "https://github.com/another/project/pull/17",
            "pr_remote_identity_mismatch",
        ),
        ("baseRefName", "release", "pr_remote_identity_mismatch"),
        ("headRefOid", "c" * 40, "pr_head_changed"),
    ],
)
def test_remote_identity_or_new_head_blocks_merge(tmp_path, field, value, code):
    host = Host()
    host.observed[field] = value
    with pytest.raises(PublicationError, match=code):
        GitHubPRLifecycle(host).merge(pr(), policy(), tmp_path)
    assert host.merge_calls == 0


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("isDraft", True, "pr_is_draft"),
        ("mergeable", "CONFLICTING", "pr_merge_conflicts"),
        ("mergeable", "UNKNOWN", "pr_mergeability_pending"),
        ("mergeStateStatus", "BLOCKED", "pr_merge_state_blocked"),
        ("mergeStateStatus", "BEHIND", "pr_merge_state_blocked"),
        ("reviewDecision", "REVIEW_REQUIRED", "pr_review_pending"),
        ("reviewDecision", "CHANGES_REQUESTED", "pr_changes_requested"),
        ("state", "CLOSED", "pr_closed_without_merge"),
    ],
)
def test_pending_or_blocked_conditions_return_observation_without_merge(
    tmp_path, field, value, code
):
    host = Host()
    host.observed[field] = value
    lifecycle = GitHubPRLifecycle(host)
    receipt = lifecycle.inspect(pr(), policy(), tmp_path)
    assert receipt["ready"] is False and code in receipt["blockers"]
    callbacks = []
    with pytest.raises(PublicationError, match=code) as caught:
        lifecycle.merge(
            pr(), policy(), tmp_path, before_merge=lambda: callbacks.append(1)
        )
    assert caught.value.emitted is False
    assert callbacks == [] and host.merge_calls == 0


@pytest.mark.parametrize(
    "row,code",
    [
        (check("extra", status="QUEUED", conclusion=""), "pr_checks_pending"),
        (check("extra", status="IN_PROGRESS", conclusion=""), "pr_checks_pending"),
        (check("extra", conclusion="FAILURE"), "pr_checks_failed"),
        (check("extra", conclusion="CANCELLED"), "pr_checks_failed"),
        (
            {"__typename": "StatusContext", "context": "extra", "state": "PENDING"},
            "pr_checks_pending",
        ),
        (
            {"__typename": "StatusContext", "context": "extra", "state": "ERROR"},
            "pr_checks_failed",
        ),
        (check("tests"), "pr_check_context_ambiguous"),
    ],
)
def test_extra_pending_or_failed_checks_and_ambiguous_names_block(tmp_path, row, code):
    host = Host()
    host.observed["statusCheckRollup"].append(row)
    observed = GitHubPRLifecycle(host).inspect(pr(), policy(), tmp_path)
    assert observed["ready"] is False and code in observed["blockers"]


@pytest.mark.parametrize("conclusion", ["NEUTRAL", "SKIPPED"])
def test_required_checks_need_success_even_if_optional_conclusion_is_benign(
    tmp_path, conclusion
):
    host = Host()
    host.observed["statusCheckRollup"][0] = check("tests", conclusion=conclusion)
    observed = GitHubPRLifecycle(host).inspect(pr(), policy(), tmp_path)
    assert "pr_required_check_not_successful" in observed["blockers"]
    host.observed["statusCheckRollup"] = [
        check("tests"),
        check("lint"),
        check("optional", conclusion=conclusion),
    ]
    assert GitHubPRLifecycle(host).inspect(pr(), policy(), tmp_path)["ready"]


def test_legacy_status_context_success_satisfies_required_check(tmp_path):
    host = Host()
    host.observed["statusCheckRollup"][0] = {
        "__typename": "StatusContext",
        "context": "tests",
        "state": "SUCCESS",
    }
    assert GitHubPRLifecycle(host).inspect(pr(), policy(), tmp_path)["ready"]


def test_no_checks_requires_explicit_policy_and_missing_required_never_passes(tmp_path):
    host = Host()
    host.observed["statusCheckRollup"] = None
    observed = GitHubPRLifecycle(host).inspect(pr(), policy(), tmp_path)
    assert observed["blockers"] == ["pr_required_checks_missing"]
    permitted = policy(required_checks=[], allow_no_checks=True)
    assert GitHubPRLifecycle(host).inspect(pr(), permitted, tmp_path)["ready"]


def test_hook_merge_state_requires_explicit_policy(tmp_path):
    host = Host()
    host.observed["mergeStateStatus"] = "HAS_HOOKS"
    assert not GitHubPRLifecycle(host).inspect(pr(), policy(), tmp_path)["ready"]
    assert GitHubPRLifecycle(host).inspect(
        pr(), policy(allowed_merge_states=["CLEAN", "HAS_HOOKS"]), tmp_path
    )["ready"]


def test_optional_review_does_not_ignore_changes_requested(tmp_path):
    host = Host()
    host.observed["reviewDecision"] = ""
    assert GitHubPRLifecycle(host).inspect(
        pr(), policy(required_review=False), tmp_path
    )["ready"]
    host.observed["reviewDecision"] = "CHANGES_REQUESTED"
    assert not GitHubPRLifecycle(host).inspect(
        pr(), policy(required_review=False), tmp_path
    )["ready"]


def test_lost_merge_response_observes_success_and_retry_never_emits_again(tmp_path):
    host = Host()
    host.lose_response = True
    lifecycle = GitHubPRLifecycle(host)
    first = lifecycle.merge(pr(), policy(), tmp_path)
    assert first["state"] == "merged"
    assert lifecycle.merge(pr(), policy(), tmp_path, reconcile_only=True) == first
    assert host.merge_calls == 1


def test_unobserved_merge_or_queue_receipt_stays_uncertain_without_repeat(tmp_path):
    host = Host()
    host.accept_merge = False
    lifecycle = GitHubPRLifecycle(host)
    with pytest.raises(PublicationError, match="pr_merge_uncertain") as caught:
        lifecycle.merge(pr(), policy(), tmp_path)
    assert caught.value.emitted is True
    with pytest.raises(PublicationError, match="pr_merge_uncertain"):
        GitHubPRLifecycle(host).merge(pr(), policy(), tmp_path, reconcile_only=True)
    assert host.merge_calls == 1
    host.observed.update(state="MERGED", mergeCommit={"oid": "b" * 40})
    assert (
        lifecycle.merge(pr(), policy(), tmp_path, reconcile_only=True)["merge_sha"]
        == "b" * 40
    )
    assert host.merge_calls == 1


def test_already_merged_reconciles_without_historical_checks_or_review(tmp_path):
    host = Host()
    host.observed.update(state="MERGED", mergeCommit={"oid": "b" * 40})
    for key in (
        "statusCheckRollup",
        "reviewDecision",
        "mergeable",
        "mergeStateStatus",
        "isDraft",
    ):
        del host.observed[key]
    callbacks = []
    result = GitHubPRLifecycle(host).merge(
        pr(), policy(), tmp_path, before_merge=lambda: callbacks.append(1)
    )
    assert result["merge_sha"] == "b" * 40
    assert host.merge_calls == 0 and callbacks == []


@pytest.mark.parametrize("merge_commit", [None, {}, {"oid": "not-a-sha"}])
def test_merged_without_verified_commit_is_not_success(tmp_path, merge_commit):
    host = Host()
    host.observed.update(state="MERGED", mergeCommit=merge_commit)
    with pytest.raises(PublicationError, match="pr_merge_commit_unverified"):
        GitHubPRLifecycle(host).merge(pr(), policy(), tmp_path, reconcile_only=True)


def test_post_emission_observation_failure_is_sanitized_and_flagged(tmp_path):
    host = Host()
    host.fail_read_after_merge = True
    with pytest.raises(PublicationError) as caught:
        GitHubPRLifecycle(host).merge(pr(), policy(), tmp_path)
    assert caught.value.emitted is True
    assert str(caught.value) == "pr_host_unavailable"
    assert host.merge_calls == 1


def test_changed_head_after_emission_is_never_accepted(tmp_path):
    host = Host()
    host.changed_head_after_merge = True
    with pytest.raises(PublicationError, match="pr_head_changed") as caught:
        GitHubPRLifecycle(host).merge(pr(), policy(), tmp_path)
    assert caught.value.emitted is True


def test_callback_failure_stops_before_mutation(tmp_path):
    host = Host()

    def fail():
        raise RuntimeError("private persistence detail")

    with pytest.raises(PublicationError, match="pr_merge_intent_failed") as caught:
        GitHubPRLifecycle(host).merge(pr(), policy(), tmp_path, before_merge=fail)
    assert caught.value.emitted is False
    assert host.merge_calls == 0


def test_callback_cannot_mutate_bound_head_or_policy_method(tmp_path):
    host = Host()
    bound, rule = pr(), policy()

    def mutate_caller_objects():
        bound["head_sha"] = "f" * 40
        rule["head_sha"] = "f" * 40
        rule["method"] = "merge"

    result = GitHubPRLifecycle(host).merge(
        bound, rule, tmp_path, before_merge=mutate_caller_objects
    )
    assert result["head_sha"] == "a" * 40
    command = next(a for a, _ in host.calls if a[:3] == ["gh", "pr", "merge"])
    assert "--squash" in command


@pytest.mark.parametrize(
    "field,value",
    [
        ("state", []),
        ("mergeable", None),
        ("mergeStateStatus", []),
        ("statusCheckRollup", "untrusted"),
        ("reviewDecision", []),
        ("isDraft", "false"),
    ],
)
def test_invalid_host_schema_is_safe_block(tmp_path, field, value):
    host = Host()
    host.observed[field] = value
    with pytest.raises(PublicationError, match="pr_host_response_invalid"):
        GitHubPRLifecycle(host).merge(pr(), policy(), tmp_path)
    assert host.merge_calls == 0


@pytest.mark.parametrize("field", ["statusCheckRollup", "reviewDecision"])
def test_absent_open_pr_evidence_cannot_imply_success(tmp_path, field):
    host = Host()
    del host.observed[field]
    with pytest.raises(PublicationError, match="pr_host_response_invalid"):
        GitHubPRLifecycle(host).merge(pr(), policy(), tmp_path)
    assert host.merge_calls == 0
