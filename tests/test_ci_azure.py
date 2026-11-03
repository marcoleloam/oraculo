"""Laboratory Azure REST transport on a real disposable Git remote/worktree."""

from urllib.parse import urlsplit

import pytest
from test_azure_repos import azure as azure_fixture

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
