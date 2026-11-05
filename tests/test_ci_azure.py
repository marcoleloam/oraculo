"""Laboratory Azure REST transport on a real disposable Git remote/worktree."""

from urllib.parse import urlsplit

import pytest
from test_azure_repos import azure as azure_fixture
from test_publication import git

from oraculo.azure_repos import AzureRepos
from oraculo.ci_events import CIBinding
from oraculo.pr_lifecycle import RepositoryPRLifecycle
from oraculo.state import StateError

azure = azure_fixture


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


def test_n_ci_policy_2_develop_target_observed_from_default_branch(azure):
    """N-ci-policy-2: CI read uses the PR target, not local main/HEAD."""
    root, spec, host, _ = azure
    git(
        root,
        "-c",
        "user.name=Example",
        "-c",
        "user.email=example@example.org",
        "commit",
        "--allow-empty",
        "-m",
        "Feature",
    )
    feature = git(root, "rev-parse", "HEAD")
    git(root, "switch", "main")
    assert git(root, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert git(root, "rev-parse", "HEAD") != feature
    develop = "c" * 40
    host.refs["refs/heads/task/example"] = feature
    host.refs["refs/heads/develop"] = develop
    row = host.row()
    row["sourceRefName"] = "refs/heads/task/example"
    row["targetRefName"] = "refs/heads/develop"
    row["lastMergeSourceCommit"] = {"commitId": feature}
    row["lastMergeTargetCommit"] = {"commitId": develop}
    host.rows = [row]
    host.iterations = [
        {
            "id": 1,
            "sourceRefCommit": {"commitId": feature},
            "targetRefCommit": {"commitId": develop},
        }
    ]
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
    build = dict(
        id=99,
        definition=dict(id=7),
        repository=dict(id=host.repo["id"]),
        sourceVersion=feature,
        triggerInfo={"pr.number": "17"},
        status="completed",
        result="succeeded",
        finishTime="2026-11-03T10:00:00Z",
    )

    def transport(method, url, body=None):
        assert method == "GET"
        path = urlsplit(url).path
        if path.endswith("/build/builds/99"):
            return build, {}
        if path.endswith("/build/builds/99/timeline"):
            return dict(records=[dict(attempt=2)]), {}
        return host(method, url, body)

    adapter = AzureRepos(transport)
    lifecycle = RepositoryPRLifecycle(azure_factory=lambda _: adapter)
    binding = CIBinding(
        repository=spec["repository"],
        pr=17,
        head=feature,
        base=develop,
        run="99",
        attempt=2,
    )
    result = lifecycle.observe_ci(binding, root)
    assert result["event"]["conclusion"] == "passed"
    assert result["evidence"]["policy"]["base"] == "develop"
    assert result["evidence"]["lifecycle"]["head_sha"] == feature
    assert host.created == host.merges == 0
