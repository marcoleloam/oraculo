"""Laboratory Azure REST transport on a real disposable Git remote/worktree."""

from urllib.parse import urlsplit

import pytest
from test_azure_repos import azure as azure_fixture
from test_azure_repos import existing, policy

from oraculo.azure_repos import AzureRepos
from oraculo.ci_events import CIBinding
from oraculo.ci_round_memory import CIRoundMemory
from oraculo.pr_lifecycle import RepositoryPRLifecycle
from oraculo.publication import PublicationError
from oraculo.state import StateError

azure = azure_fixture


def test_empty_azure_policy_discovery_allows_explicit_no_checks(azure):
    root, spec, host, adapter = azure
    host.policies = []
    host.statuses = []
    pr = existing(azure)
    explicit = dict(policy(spec), required_checks=[], allow_no_checks=True)
    lifecycle = RepositoryPRLifecycle(azure_factory=lambda _: adapter)
    observed = lifecycle.validate_host_policy(pr, explicit, root)
    assert observed["required_checks"] == []
    assert observed["required_review"] is False
    assert observed["allow_no_checks"] is True
    assert observed["source"] == "authenticated_azure_policy_evaluations"
    assert observed["repository"] == spec["repository"]
    assert observed["head_sha"] == spec["head_sha"]
    assert observed["policy_evaluations"] == []
    assert lifecycle.inspect(pr, explicit, root)["ready"] is True
    assert host.created == host.merges == 0


@pytest.mark.parametrize(
    "settings",
    [
        {"statusName": "unit", "statusGenre": "ci"},
        {"buildDefinitionId": 7},
        {"minimumApproverCount": 1},
        {},
    ],
)
def test_present_azure_policy_does_not_prove_no_checks(azure, settings):
    root, spec, host, adapter = azure
    host.policies = [
        {
            "configuration": {
                "isEnabled": True,
                "isBlocking": True,
                "settings": settings,
            },
            "status": "approved",
        }
    ]
    pr = existing(azure)
    lifecycle = RepositoryPRLifecycle(azure_factory=lambda _: adapter)
    explicit = dict(policy(spec), required_checks=[], allow_no_checks=True)
    with pytest.raises(PublicationError, match="ci_actual_host_policy_not_satisfied"):
        lifecycle.validate_host_policy(pr, explicit, root)
    assert host.created == host.merges == 0


def test_failed_azure_policy_discovery_cannot_look_like_empty_rules(azure):
    root, spec, host, adapter = azure
    pr = existing(azure)

    def transport(method, url, body=None):
        if "/policy/evaluations" in url:
            raise TimeoutError("synthetic policy discovery failure")
        return host(method, url, body)

    lifecycle = RepositoryPRLifecycle(azure_factory=lambda _: AzureRepos(transport))
    explicit = dict(policy(spec), required_checks=[], allow_no_checks=True)
    with pytest.raises((PublicationError, TimeoutError)):
        lifecycle.validate_host_policy(pr, explicit, root)
    assert host.created == host.merges == 0


@pytest.mark.parametrize(
    "field",
    [
        "repository",
        "repository_id",
        "project_id",
        "base",
        "head_sha",
        "project_policy_sha256",
    ],
)
def test_azure_no_ci_memory_scope_rejects_swapped_binding(azure, field):
    root, spec, host, adapter = azure
    host.policies = []
    pr = existing(azure)
    explicit = dict(policy(spec), required_checks=[], allow_no_checks=True)
    observed = RepositoryPRLifecycle(
        azure_factory=lambda _: adapter
    ).validate_host_policy(pr, explicit, root)
    CIRoundMemory.require_same_policy(observed, dict(observed))
    with pytest.raises(StateError, match="ci_host_policy_changed_after_memory"):
        CIRoundMemory.require_same_policy(observed, {**observed, field: "swapped"})


@pytest.mark.parametrize(
    "mutation", [None, "attempt", "head", "policy_build", "cancel"]
)
def test_azure_observer_reuses_authenticated_adapter_and_binds_policy_build(
    azure, mutation
):
    root, spec, host, _ = azure
    host.rows = [host.row()]
    host.policies.append(
        dict(
            configuration=dict(
                id=2,
                isEnabled=True,
                isBlocking=True,
                settings=dict(buildDefinitionId=7),
            ),
            status="approved",
            context=dict(buildId=99),
        )
    )
    if mutation == "policy_build":
        host.policies[-1]["context"]["buildId"] = 98
    build = dict(
        id=99,
        definition=dict(id=7),
        repository=dict(id=host.repo["id"]),
        sourceVersion="f" * 40 if mutation == "head" else spec["head_sha"],
        triggerInfo={"pr.number": "17"},
        status="completed",
        result="canceled" if mutation == "cancel" else "succeeded",
        finishTime="2026-11-03T10:00:00Z",
    )

    def transport(method, url, body=None):
        assert method == "GET"
        path = urlsplit(url).path
        if path.endswith("/build/builds/99"):
            return build, {}
        if path.endswith("/build/builds/99/timeline"):
            return dict(records=[dict(attempt=1 if mutation == "attempt" else 2)]), {}
        return host(method, url, body)

    adapter = AzureRepos(transport)
    lifecycle = RepositoryPRLifecycle(azure_factory=lambda _: adapter)
    binding = CIBinding(
        repository=spec["repository"],
        pr=17,
        head=spec["head_sha"],
        base=spec["head_sha"],
        run="99",
        attempt=2,
    )
    if mutation in {"attempt", "head", "policy_build"}:
        with pytest.raises(StateError):
            lifecycle.observe_ci(binding, root)
    else:
        result = lifecycle.observe_ci(binding, root)
        assert result["event"]["conclusion"] == (
            "cancelled" if mutation == "cancel" else "passed"
        )
        assert result["evidence"]["source"] == "authenticated_azure_rest"
        assert host.created == host.merges == 0
