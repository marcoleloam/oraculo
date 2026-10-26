"""Real disposable Git; synthetic Azure REST data, never external support proof."""

import copy
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
from test_publication import git, new_repository, specification

from oraculo.azure_repos import AzureHTTP, AzureRepos, azure_repository, load_pat
from oraculo.pr_lifecycle import RepositoryPRLifecycle
from oraculo.publication import (
    PublicationError,
    RepositoryPublisher,
    inspect_repository,
    integration_binding,
)

REMOTE = "https://dev.azure.com/example/Example%20Project/_git/example-repo"
REPOSITORY_ID = "11111111-1111-1111-1111-111111111111"
PROJECT_ID = "22222222-2222-2222-2222-222222222222"


class AzureHost:
    def __init__(self, spec):
        self.spec = spec
        self.calls, self.rows = [], []
        self.created, self.merges = 0, 0
        self.lose_create, self.lose_merge, self.invisible = False, False, False
        self.repo = {
            "id": REPOSITORY_ID,
            "name": "example-repo",
            "remoteUrl": REMOTE,
            "project": {"name": "Example Project", "id": PROJECT_ID},
        }
        self.refs = {
            "refs/heads/" + spec["branch"]: spec["head_sha"],
            "refs/heads/main": spec["head_sha"],
        }
        self.policies = [
            {
                "configuration": {"id": 1, "isBlocking": True, "isEnabled": True},
                "status": "approved",
            }
        ]
        self.statuses = [
            {
                "context": {"name": "unit", "genre": "ci"},
                "state": "succeeded",
                "iterationId": 1,
            }
        ]
        self.iterations = [
            {
                "id": 1,
                "sourceRefCommit": {"commitId": spec["head_sha"]},
                "targetRefCommit": {"commitId": spec["head_sha"]},
            }
        ]

    def row(self, status="active", number=17):
        return {
            "pullRequestId": number,
            "repository": copy.deepcopy(self.repo),
            "status": status,
            "mergeStatus": "succeeded",
            "lastMergeSourceCommit": {"commitId": self.spec["head_sha"]},
            "lastMergeTargetCommit": {"commitId": self.spec["head_sha"]},
            "lastMergeCommit": {"commitId": "b" * 40},
            "sourceRefName": "refs/heads/" + self.spec["branch"],
            "targetRefName": "refs/heads/main",
            "isDraft": False,
            "createdBy": {"id": "author"},
            "reviewers": [{"id": "reviewer", "vote": 10, "isRequired": True}],
            "description": "Existing external integration PR",
        }

    def __call__(self, method, url, body=None):
        parsed = urlsplit(url)
        assert parsed.hostname == "dev.azure.com"
        assert parsed.username is None
        path, query = unquote(parsed.path), parse_qs(parsed.query)
        assert query["api-version"] == (
            ["7.1-preview.1"] if "/policy/" in path else ["7.1"]
        )
        self.calls.append((method, path, query, copy.deepcopy(body)))
        if method == "POST":
            self.created += 1
            self.rows.append(self.row())
            self.rows[-1]["description"] = body["description"]
            if self.lose_create:
                raise TimeoutError("synthetic lost acknowledgement")
            return copy.deepcopy(self.rows[-1]), {}
        if method == "PATCH":
            self.merges += 1
            assert body["lastMergeSourceCommit"]["commitId"] == self.spec["head_sha"]
            assert body["completionOptions"]["bypassPolicy"] is False
            assert "autoCompleteSetBy" not in body
            self.rows[0]["status"] = "completed"
            if self.lose_merge:
                raise TimeoutError("synthetic lost acknowledgement")
            return copy.deepcopy(self.rows[0]), {}
        if path.endswith("/repositories/example-repo"):
            return copy.deepcopy(self.repo), {}
        if path.endswith("/pullrequests"):
            rows = [] if self.invisible else self.rows
        elif path.endswith("/refs"):
            rows = [
                {"name": name, "objectId": sha}
                for name, sha in self.refs.items()
                if name.startswith("refs/" + query["filter"][0])
            ]
        elif path.endswith("/evaluations"):
            assert query["artifactId"] == [
                f"vstfs:///CodeReview/CodeReviewId/{PROJECT_ID}/17"
            ]
            rows = self.policies
        elif path.endswith("/statuses"):
            rows = self.statuses
        elif path.endswith("/iterations"):
            rows = self.iterations
        else:
            number = int(path.rsplit("/", 1)[-1])
            return copy.deepcopy(
                next(r for r in self.rows if r["pullRequestId"] == number)
            ), {}
        offset, top = (
            int(query.get("$skip", [0])[0]),
            int(query.get("$top", [10000])[0]),
        )
        rows = copy.deepcopy(rows[offset : offset + top])
        return {"count": len(rows), "value": rows}, {}


@pytest.fixture
def azure(tmp_path):
    repo = new_repository(tmp_path / "repo")
    git(repo, "remote", "set-url", "origin", REMOTE)
    spec = {**specification(repo), "repository": "example/Example Project/example-repo"}
    host = AzureHost(spec)
    return repo, spec, host, AzureRepos(host)


def policy(spec):
    return {
        "repository": spec["repository"],
        "head_sha": spec["head_sha"],
        "base": spec["base"],
        "merge_authorized": True,
        "method": "merge",
        "required_checks": ["ci/unit"],
        "required_review": True,
        "policy_source": "synthetic policy fixture",
    }


def existing(azure, status="active"):
    _, spec, host, adapter = azure
    host.rows = [host.row(status)]
    return adapter.ensure_pr({**spec, "reconcile_only": True, "pr_id": 17})


@pytest.mark.parametrize("old_state", ["failed", "pending", "succeeded"])
def test_current_iteration_check_supersedes_prior_iteration_status(azure, old_state):
    repo, spec, host, adapter = azure
    pr = existing(azure)
    host.iterations.append({**host.iterations[0], "id": 2})
    host.statuses = [
        {**host.statuses[0], "state": old_state},
        {**host.statuses[0], "iterationId": 2},
    ]
    result = adapter.inspect(pr, policy(spec), repo)
    assert result["ready"], result["blockers"]
    assert result["checks"] == [
        {"name": "ci/unit", "state": "succeeded", "iterationId": 2}
    ]


def test_remote_identity_and_escaping(azure):
    repo, _, _, _ = azure
    m = inspect_repository(repo)
    assert (
        m["host"] == "azure"
        and m["repository"] == "example/Example Project/example-repo"
    )
    assert m["remote_url"] == REMOTE
    assert azure_repository("https://github.com/example/repo") is None
    escaped = azure_repository(
        "https://dev.azure.com/example/P%20%26%20Q/_git/repo%20name"
    )
    assert escaped["project"] == "P & Q" and escaped["remote_url"].endswith(
        "P%20%26%20Q/_git/repo%20name"
    )


@pytest.mark.parametrize(
    "remote",
    [
        "http://dev.azure.com/example/project/_git/repo",
        "https://user:synthetic-password@dev.azure.com/example/p/_git/r",
        "https://dev.azure.com:443/example/p/_git/r",
        "https://dev.azure.com/example/p/_git/r?token=value",
        "https://dev.azure.com/example/p/_git/r#fragment",
        "https://dev.azure.com/example/p/_git/a%2Fb",
        "https://dev.azure.com/example/p/_git/%2e%2e",
        "https://dev.azure.com/example/p/_git/%252f",
        "https://dev.azure.com/example/p/_git/r/extra",
        "https://dev.azure.com/example/p/_git/%00",
    ],
)
def test_invalid_remotes(remote):
    with pytest.raises(PublicationError, match="publication_remote_invalid"):
        azure_repository(remote)


@pytest.mark.parametrize(
    "username", ["example", "user%40example.org", "synthetic-token"]
)
def test_azure_login_hint_is_removed_from_repository_and_rest_identity(azure, username):
    repo, spec, host, adapter = azure
    git(repo, "remote", "set-url", "origin", REMOTE.replace("://", f"://{username}@"))
    metadata = inspect_repository(repo)
    assert metadata["remote_url"] == REMOTE
    assert azure_repository(REMOTE) == azure_repository(
        REMOTE.replace("://", f"://{username}@")
    )
    receipt = adapter.ensure_pr(spec)
    assert receipt["remote_url"] == REMOTE
    assert receipt["url"] == REMOTE + "/pullrequest/17"
    assert username not in str(receipt) or username == "example"


@pytest.mark.parametrize("status", ["active", "completed"])
@pytest.mark.parametrize("identifier", ["id", "url", "both"])
def test_existing_observation_never_creates(azure, status, identifier):
    _, spec, host, adapter = azure
    host.rows = [host.row(status)]
    selection = (
        {"pr_id": 17} if identifier == "id" else {"pr_url": REMOTE + "/pullrequest/17"}
    )
    if identifier == "both":
        selection["pr_id"] = 17
    receipt = adapter.ensure_pr(
        {**spec, "reconcile_only": True, **selection},
        before_create=lambda: pytest.fail("no intent"),
    )
    assert receipt["state"] == ("merged" if status == "completed" else "open")
    assert receipt["source"] == "azure_devops_rest" and receipt["observed_at"]
    assert receipt["pullRequestId"] == 17 and receipt["repository_id"] == REPOSITORY_ID
    assert host.created == 0


def test_creation_and_lost_ack_observed_once(azure):
    _, spec, host, adapter = azure
    host.lose_create = True
    intents = []
    receipt = adapter.ensure_pr(spec, before_create=lambda: intents.append(True))
    assert receipt["state"] == "open" and host.created == 1 and intents == [True]
    replay = adapter.ensure_pr({**spec, "reconcile_only": True})
    assert replay["number"] == receipt["number"] and host.created == 1


def test_uncertain_create_cannot_reemit(azure):
    _, spec, host, adapter = azure
    host.invisible = True
    with pytest.raises(PublicationError) as error:
        adapter.ensure_pr(spec)
    assert error.value.code == "publication_uncertain" and error.value.emitted
    with pytest.raises(PublicationError, match="publication_uncertain"):
        adapter.ensure_pr({**spec, "reconcile_only": True})
    assert host.created == 1


@pytest.mark.parametrize(
    "change,code",
    [
        (lambda h: h.repo.update(name="wrong"), "azure_repository_mismatch"),
        (
            lambda h: h.rows[0]["repository"].update(
                id="33333333-3333-3333-3333-333333333333"
            ),
            "azure_repository_mismatch",
        ),
        (
            lambda h: h.rows[0].update(sourceRefName="refs/heads/wrong"),
            "azure_pr_identity_invalid",
        ),
        (
            lambda h: h.rows[0].update(targetRefName="refs/heads/develop"),
            "azure_pr_identity_invalid",
        ),
        (
            lambda h: h.rows[0].update(lastMergeSourceCommit={"commitId": "c" * 40}),
            "pr_head_changed",
        ),
        (
            lambda h: h.rows[0].update(status="completed", lastMergeCommit=None),
            "pr_merge_commit_unverified",
        ),
        (
            lambda h: h.rows[0].update(status="completed", mergeStatus="conflicts"),
            "pr_merge_commit_unverified",
        ),
    ],
)
def test_identity_and_schema_fail_closed(azure, change, code):
    _, spec, host, adapter = azure
    host.rows = [host.row()]
    change(host)
    with pytest.raises(PublicationError, match=code):
        adapter.ensure_pr({**spec, "pr_id": 17, "reconcile_only": True})
    assert host.created == 0


@pytest.mark.parametrize(
    "change,blocker",
    [
        (lambda h: h.policies[0].update(status="running"), "pr_policy_pending"),
        (lambda h: h.policies[0].update(status="rejected"), "pr_policy_failed"),
        (lambda h: h.statuses[0].update(state="pending"), "pr_checks_pending"),
        (lambda h: h.statuses[0].update(state="failed"), "pr_checks_failed"),
        (lambda h: h.statuses[0].update(iterationId=2), "pr_check_head_unverified"),
        (lambda h: h.rows[0]["reviewers"][0].update(vote=0), "pr_review_pending"),
        (lambda h: h.rows[0]["reviewers"][0].update(vote=-10), "pr_changes_requested"),
        (lambda h: h.rows[0].update(mergeStatus="conflicts"), "pr_merge_conflicts"),
        (lambda h: h.refs.update({"refs/heads/main": "d" * 40}), "pr_target_changed"),
    ],
)
def test_checks_policies_review_exact_head(azure, change, blocker):
    repo, spec, host, adapter = azure
    pr = existing(azure)
    change(host)
    receipt = adapter.inspect(pr, policy(spec), repo)
    assert not receipt["ready"] and blocker in receipt["blockers"]
    with pytest.raises(PublicationError):
        adapter.merge(pr, policy(spec), repo)
    assert host.merges == 0


def test_ready_and_merge_only_confirmed_by_get(azure):
    repo, spec, host, adapter = azure
    pr = existing(azure)
    assert adapter.inspect(pr, policy(spec), repo)["ready"]
    host.lose_merge = True
    intents = []
    result = adapter.merge(
        pr, policy(spec), repo, before_merge=lambda: intents.append(True)
    )
    assert (
        result["state"] == "merged"
        and result["merge_sha"] == "b" * 40
        and intents == [True]
    )
    result = adapter.merge(pr, policy(spec), repo, reconcile_only=True)
    assert host.merges == 1


def test_offset_and_token_pagination(azure):
    _, _, host, adapter = azure
    m = adapter._identity(inspect_repository(azure[0]))
    host.policies = [{"id": i} for i in range(201)]
    rows = adapter._pages(
        m,
        "policy/evaluations",
        policy=True,
        params={"artifactId": f"vstfs:///CodeReview/CodeReviewId/{PROJECT_ID}/17"},
    )
    assert len(rows) == 201
    assert [
        call[2]["$skip"] for call in host.calls if call[1].endswith("evaluations")
    ] == [["0"], ["100"], ["200"]]
    calls = []

    def transport(method, url, body):
        q = parse_qs(urlsplit(url).query)
        calls.append(q)
        if "continuationToken" not in q:
            return {"count": 1, "value": [{"id": 1}]}, {
                "x-ms-continuationtoken": "opaque & value"
            }
        assert q["continuationToken"] == ["opaque & value"]
        return {"count": 1, "value": [{"id": 2}]}, {}

    assert len(AzureRepos(transport)._pages(m, "git/refs", mode="token")) == 2
    assert len(calls) == 2


def test_shared_integration_proves_all_five_commits(azure):
    repo, spec, host, adapter = azure
    commits = []
    for index in range(5):
        git(
            repo,
            "-c",
            "user.name=Example",
            "-c",
            "user.email=example@example.org",
            "commit",
            "--allow-empty",
            "-m",
            f"Part {index}",
        )
        commits.append(git(repo, "rev-parse", "HEAD"))
    spec["head_sha"] = commits[-1]
    host.rows = [host.row("completed")]
    for index, commit in enumerate(commits):
        receipt = adapter.ensure_pr(
            {
                **spec,
                "source_commit": commit,
                "source_project_id": "example",
                "task_id": f"task-{index}",
                "pr_id": 17,
                "reconcile_only": True,
                "integration": {"head_sha": commits[-1], "contains": commits},
            }
        )
        assert (
            receipt["source_commit"] == commit
            and receipt["integration"]["contains"] == commits
        )
    assert host.created == host.merges == 0


def test_integration_rejects_same_tree_nonancestor_and_missing_commits(azure):
    repo, spec, _, _ = azure
    original = spec["head_sha"]
    git(repo, "switch", "--orphan", "unrelated")
    git(
        repo,
        "-c",
        "user.name=Example",
        "-c",
        "user.email=example@example.org",
        "commit",
        "--allow-empty",
        "-m",
        "Same empty tree",
    )
    unrelated = git(repo, "rev-parse", "HEAD")
    assert git(repo, "rev-parse", original + "^{tree}") == git(
        repo, "rev-parse", unrelated + "^{tree}"
    )
    git(repo, "switch", spec["branch"])
    bound = {
        **spec,
        "source_commit": original,
        "reconcile_only": True,
        "pr_id": 17,
        "integration": {"head_sha": original, "contains": [original, unrelated]},
    }
    with pytest.raises(PublicationError, match="not_ancestor"):
        integration_binding(bound)
    bound["integration"]["contains"][-1] = "f" * 40
    with pytest.raises(PublicationError, match="commit_missing"):
        integration_binding(bound)


def test_routing_uses_actual_remote(azure):
    repo, spec, host, adapter = azure
    host.rows = [host.row()]
    publisher = RepositoryPublisher(azure_factory=lambda _: adapter)
    pr = publisher.ensure_pr({**spec, "host": "github"})
    assert pr["host"] == "azure"
    lifecycle = RepositoryPRLifecycle(azure_factory=lambda _: adapter)
    assert lifecycle.inspect(pr, policy(spec), repo)["ready"]


@pytest.mark.parametrize(
    "value",
    [
        "$(touch should-not-exist)",
        "`command`",
        "a b",
        "x\nSYS__AZDO_PAT=y",
        "",
        "a;command",
    ],
)
def test_env_never_executes_or_echoes_bad_values(tmp_path, value):
    (tmp_path / ".env").write_text("SYS__AZDO_PAT=" + value + "\n")
    with pytest.raises(PublicationError) as error:
        load_pat(tmp_path)
    assert error.value.code in {
        "azure_credential_invalid",
        "azure_credential_unavailable",
    }
    assert not (tmp_path / "should-not-exist").exists()


def test_literal_env_and_explicit_existing_keychain_only(tmp_path):
    (tmp_path / ".env").write_text(
        "UNRELATED=$(not-executed)\nexport SYS__AZDO_PAT='synthetic-pat'\n"
    )
    assert load_pat(tmp_path) == "synthetic-pat"

    class Backend:
        def get_password(self, service, account):
            assert (service, account) == ("existing-service", "existing-account")
            return "synthetic-keychain"

    assert (
        load_pat(
            tmp_path,
            {
                "keychain_service": "existing-service",
                "keychain_account": "existing-account",
            },
            backend=Backend(),
        )
        == "synthetic-keychain"
    )
    (tmp_path / ".env").unlink()
    with pytest.raises(PublicationError, match="unavailable"):
        load_pat(tmp_path)


def test_http_redacts_transport_exceptions_and_refuses_other_hosts():
    class Opener:
        def open(self, request, timeout):
            assert timeout == 30
            assert request.get_header("Authorization").startswith("Basic ")
            raise RuntimeError("synthetic-secret-must-not-escape")

    transport = AzureHTTP("synthetic-secret-must-not-escape", opener=Opener())
    with pytest.raises(PublicationError) as error:
        transport("GET", REMOTE)
    assert str(error.value) == "azure_host_unavailable"
    assert error.value.__suppress_context__
    with pytest.raises(PublicationError, match="endpoint_invalid"):
        transport("GET", "https://elsewhere.example/redirect")


def test_selected_nondefault_remote_remains_bound_through_lifecycle(azure):
    repo, spec, host, adapter = azure
    git(repo, "remote", "rename", "origin", "selected")
    spec["remote"] = "selected"
    host.rows = [host.row()]
    pr = adapter.ensure_pr(spec)
    assert pr["remote"] == "selected"
    assert adapter.inspect(pr, policy(spec), repo)["ready"]
    with pytest.raises(PublicationError, match="binding_mismatch"):
        adapter.inspect(pr, {**policy(spec), "remote": "origin"}, repo)


@pytest.mark.parametrize(
    "change",
    [
        lambda s: s["integration"].update(contains=[]),
        lambda s: s["integration"].update(contains=["c" * 40]),
        lambda s: s["integration"]["contains"].append(s["source_commit"]),
        lambda s: s["integration"].update(head_sha="c" * 40),
        lambda s: s.update(reconcile_only=False),
        lambda s: s.pop("pr_id"),
    ],
)
def test_declared_integration_shape_cannot_bypass_binding(azure, change):
    _, spec, _, _ = azure
    s = {
        **spec,
        "source_commit": spec["head_sha"],
        "pr_id": 17,
        "reconcile_only": True,
        "integration": {"head_sha": spec["head_sha"], "contains": [spec["head_sha"]]},
    }
    change(s)
    with pytest.raises(PublicationError, match="integration_invalid"):
        integration_binding(s)


@pytest.mark.parametrize("forgery", ["replace", "graft"])
def test_git_replacements_cannot_forge_ancestry(azure, forgery):
    repo, spec, _, _ = azure
    original = spec["head_sha"]
    git(repo, "switch", "--orphan", "other")
    git(
        repo,
        "-c",
        "user.name=Example",
        "-c",
        "user.email=example@example.org",
        "commit",
        "--allow-empty",
        "-m",
        "Unrelated",
    )
    unrelated = git(repo, "rev-parse", "HEAD")
    git(repo, "switch", spec["branch"])
    if forgery == "replace":
        git(
            repo,
            "-c",
            "user.name=Example",
            "-c",
            "user.email=example@example.org",
            "replace",
            "--graft",
            original,
            unrelated,
        )
    else:
        path = repo / ".git" / "info" / "grafts"
        path.write_text(original + " " + unrelated + "\n")
    with pytest.raises(PublicationError, match="not_ancestor"):
        integration_binding(
            {
                **spec,
                "source_commit": original,
                "pr_id": 17,
                "reconcile_only": True,
                "integration": {
                    "head_sha": original,
                    "contains": [original, unrelated],
                },
            }
        )
