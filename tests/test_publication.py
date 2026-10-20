import copy
import hashlib
import json
import subprocess
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

from oraculo.publication import (
    GitHubPublisher,
    PublicationError,
    command_runner,
    inspect_repository,
)


def git(cwd, *args):
    return command_runner(["git", *args], cwd)


def new_repository(path, remote=True):
    path.mkdir()
    git(path, "init", "-b", "main")
    git(
        path,
        "-c",
        "user.name=Example",
        "-c",
        "user.email=example@example.org",
        "commit",
        "--allow-empty",
        "-m",
        "Initial",
    )
    git(path, "switch", "-c", "task/example")
    if remote:
        git(
            path,
            "remote",
            "add",
            "origin",
            "https://github.com/example/public-project.git",
        )
    return path


@pytest.fixture
def repo(tmp_path):
    return new_repository(tmp_path / "unrelated-folder-name")


def specification(repo):
    return {
        "cwd": repo,
        "remote": "origin",
        "base": "main",
        "head_sha": git(repo, "rev-parse", "HEAD"),
        "branch": "task/example",
        "task_id": "private-task-reference",
        "title": "Implement example",
        "body": "Generic reviewed change.\n\nValidation: isolated suite.",
    }


class Host:
    """Real local Git metadata; fake GitHub transport with explicit calls."""

    def __init__(self, spec):
        self.spec = spec
        self.calls = []
        self.rows = []
        self.created = 0
        self.lose_response = False
        self.visible = True
        self.remote_sha = spec["head_sha"]
        self.identity = "example/public-project"
        self.body = None
        self.read_failure = False

    def row(self, number=12, state="open", marked=True):
        marker = (
            "<!-- oraculo-task:"
            + hashlib.sha256(self.spec["task_id"].encode()).hexdigest()
            + " -->"
        )
        return {
            "number": number,
            "html_url": f"https://github.com/example/public-project/pull/{number}",
            "head": {
                "ref": self.spec["branch"],
                "sha": self.spec["head_sha"],
                "repo": {"full_name": "example/public-project"},
            },
            "base": {
                "ref": self.spec["base"],
                "repo": {"full_name": "example/public-project"},
            },
            "state": state,
            "body": marker if marked else "Existing PR",
            "merged_at": None,
        }

    def __call__(self, argv, cwd):
        self.calls.append((argv, cwd))
        if argv[0] == "git":
            return command_runner(argv, cwd)
        if argv[:3] == ["gh", "repo", "view"]:
            assert argv[3] == "github.com/example/public-project"
            return json.dumps(
                {
                    "nameWithOwner": self.identity,
                    "url": f"https://github.com/{self.identity}",
                }
            )
        if argv[:2] == ["gh", "api"]:
            assert argv[2:4] == ["--hostname", "github.com"]
            endpoint = argv[-1]
            if "/pulls?" in endpoint:
                assert "--paginate" in argv and "--slurp" in argv
                query = parse_qs(urlsplit(endpoint).query)
                assert query["state"] == ["all"]
                assert query["head"] == ["example:task/example"]
                if self.read_failure and self.created:
                    raise subprocess.CalledProcessError(
                        1, argv, stderr="private transport detail"
                    )
                return json.dumps([self.rows if self.visible else []])
            branch = unquote(endpoint.split("/git/ref/heads/")[1])
            return json.dumps(
                {
                    "ref": "refs/heads/" + branch,
                    "object": {
                        "type": "commit",
                        "sha": self.remote_sha,
                    },
                }
            )
        if argv[:3] == ["gh", "pr", "create"]:
            assert "--head" in argv and "--base" in argv and "--body-file" in argv
            self.body = Path(argv[argv.index("--body-file") + 1]).read_text()
            self.created += 1
            self.rows.append(self.row())
            if self.lose_response:
                raise subprocess.TimeoutExpired(
                    argv, 60, output="private transport detail"
                )
            return "https://github.com/example/public-project/pull/12"
        raise AssertionError(argv)


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/example/public-project.git",
        "git@github.com:example/public-project.git",
        "ssh://git@github.com/example/public-project.git",
    ],
)
def test_repository_identity_comes_from_remote_not_folder(repo, url):
    git(repo, "remote", "set-url", "origin", url)
    result = inspect_repository(repo)
    assert result["repository"] == "example/public-project"
    assert result["remote_url"] == "https://github.com/example/public-project.git"
    assert result["branch"] == "task/example"


@pytest.mark.parametrize(
    ("url", "code"),
    [
        (
            "https://dev.azure.com/example/project/_git/repo",
            "publication_host_unsupported",
        ),
        (
            "https://token@github.com/example/public-project.git",
            "publication_remote_invalid",
        ),
        (
            "https://github.com/example/public-project?secret=value",
            "publication_remote_invalid",
        ),
        ("https://github.com/example/../project", "publication_remote_invalid"),
        ("git@github.com:example/project;command", "publication_remote_invalid"),
    ],
)
def test_unsupported_or_unsafe_remote_is_blocked(repo, url, code):
    git(repo, "remote", "set-url", "origin", url)
    with pytest.raises(PublicationError, match=code):
        inspect_repository(repo)


def test_missing_remote_is_not_guessed(repo):
    git(repo, "remote", "remove", "origin")
    with pytest.raises(PublicationError, match="publication_remote_missing"):
        inspect_repository(repo)


def test_multiple_urls_are_ambiguous(repo):
    git(
        repo,
        "remote",
        "set-url",
        "--add",
        "origin",
        "https://github.com/example/other.git",
    )
    with pytest.raises(PublicationError, match="publication_remote_ambiguous"):
        inspect_repository(repo)


def test_explicit_nondefault_remote(repo):
    git(repo, "remote", "rename", "origin", "publication")
    assert (
        inspect_repository(repo, remote="publication")["repository"]
        == "example/public-project"
    )


def test_create_and_reconcile_receipt(repo):
    spec = specification(repo)
    host = Host(spec)
    result = GitHubPublisher(host).ensure_pr(spec)
    assert result == {
        "url": "https://github.com/example/public-project/pull/12",
        "head_sha": spec["head_sha"],
        "base": "main",
        "state": "open",
        "repository": "example/public-project",
        "number": 12,
        "host": "github",
    }
    assert host.created == 1
    assert spec["task_id"] not in host.body
    assert spec["body"] in host.body
    assert all("push" not in argv and "merge" not in argv for argv, _ in host.calls)
    assert GitHubPublisher(host).ensure_pr({**spec, "reconcile_only": True}) == result
    assert host.created == 1
    body_file = next(
        argv[-1] for argv, _ in host.calls if argv[:3] == ["gh", "pr", "create"]
    )
    assert not Path(body_file).exists()


@pytest.mark.parametrize("state", ["open", "closed", "merged"])
def test_existing_pr_all_states_no_create(repo, state):
    spec = specification(repo)
    host = Host(spec)
    row = host.row(state="closed" if state == "merged" else state)
    if state == "merged":
        row["merged_at"] = "2026-01-01T00:00:00Z"
    host.rows = [row]
    assert GitHubPublisher(host).ensure_pr(spec)["state"] == state
    assert host.created == 0
    assert not any("/git/ref/" in argv[-1] for argv, _ in host.calls)


def test_lost_create_response_reconciles_without_second_create(repo):
    spec = specification(repo)
    host = Host(spec)
    host.lose_response = True
    assert GitHubPublisher(host).ensure_pr(spec)["number"] == 12
    assert (
        GitHubPublisher(host).ensure_pr({**spec, "reconcile_only": True})["number"]
        == 12
    )
    assert host.created == 1


def test_delayed_visibility_after_restart_never_recreates(repo):
    spec = specification(repo)
    host = Host(spec)
    host.lose_response = True
    host.visible = False
    with pytest.raises(PublicationError, match="publication_uncertain"):
        GitHubPublisher(host).ensure_pr(spec)
    with pytest.raises(PublicationError, match="publication_uncertain"):
        GitHubPublisher(host).ensure_pr({**spec, "reconcile_only": True})
    host.visible = True
    assert (
        GitHubPublisher(host).ensure_pr({**spec, "reconcile_only": True})["number"]
        == 12
    )
    assert host.created == 1


def test_failed_observation_after_write_is_uncertain_and_sanitized(repo):
    spec = specification(repo)
    host = Host(spec)
    host.read_failure = True
    with pytest.raises(PublicationError) as caught:
        GitHubPublisher(host).ensure_pr(spec)
    assert str(caught.value) == "publication_uncertain"
    assert host.created == 1


@pytest.mark.parametrize(
    "problem", ["dirty", "head", "branch", "remote_sha", "identity", "repository"]
)
def test_preflight_failures_prevent_create(repo, problem):
    spec = specification(repo)
    host = Host(spec)
    if problem == "dirty":
        (repo / "untracked.txt").write_text("uncommitted")
    elif problem == "head":
        spec["head_sha"] = "a" * 40
    elif problem == "branch":
        spec["branch"] = "different"
    elif problem == "remote_sha":
        host.remote_sha = "a" * 40
    elif problem == "identity":
        host.identity = "example/other"
    elif problem == "repository":
        spec["repository"] = "example/unrelated-folder-name"
    with pytest.raises(PublicationError):
        GitHubPublisher(host).ensure_pr(spec)
    assert host.created == 0


def test_ambiguous_prs_require_matching_task_marker(repo):
    spec = specification(repo)
    host = Host(spec)
    host.rows = [host.row(number=12, marked=False), host.row(number=13, marked=False)]
    with pytest.raises(PublicationError, match="publication_pr_ambiguous"):
        GitHubPublisher(host).ensure_pr(spec)
    host.rows[1] = host.row(number=13)
    assert GitHubPublisher(host).ensure_pr(spec)["number"] == 13
    host.rows[0] = host.row(number=12)
    with pytest.raises(PublicationError, match="publication_pr_ambiguous"):
        GitHubPublisher(host).ensure_pr(spec)
    assert host.created == 0


@pytest.mark.parametrize("problem", ["sha", "url", "task"])
def test_existing_pr_mismatch_is_not_adopted(repo, problem):
    spec = specification(repo)
    host = Host(spec)
    row = host.row()
    if problem == "sha":
        row["head"]["sha"] = "a" * 40
    elif problem == "url":
        row["html_url"] = "https://example.org/untrusted"
    else:
        row["body"] = "<!-- oraculo-task:another -->"
    host.rows = [row]
    with pytest.raises(PublicationError):
        GitHubPublisher(host).ensure_pr(spec)
    assert host.created == 0


def test_explicit_public_checkout_binding_does_not_require_source_remote(
    repo, tmp_path
):
    private = new_repository(tmp_path / "private-source", remote=False)
    source_before = git(private, "status", "--porcelain")
    spec = specification(repo)
    spec.update(
        {
            "repository": "example/public-project",
            "source_project_id": "source-project",
            "source_commit": git(private, "rev-parse", "HEAD"),
        }
    )
    host = Host(spec)
    receipt = GitHubPublisher(host).ensure_pr(spec)
    assert receipt["source_project_id"] == "source-project"
    assert receipt["source_commit"] == spec["source_commit"]
    assert all(cwd == repo for _, cwd in host.calls)
    assert git(private, "remote") == ""
    assert git(private, "status", "--porcelain") == source_before


def test_invalid_host_payload_blocks_without_leaking_output(repo):
    spec = specification(repo)
    host = Host(spec)

    def runner(argv, cwd):
        if argv[:2] == ["gh", "api"]:
            return "private malformed response"
        return host(argv, cwd)

    with pytest.raises(PublicationError) as caught:
        GitHubPublisher(runner).ensure_pr(spec)
    assert str(caught.value) == "publication_host_response_invalid"
    assert host.created == 0


def test_foreign_fork_is_not_adopted(repo):
    spec = specification(repo)
    host = Host(spec)
    row = copy.deepcopy(host.row())
    row["head"]["repo"]["full_name"] = "another-owner/public-project"
    host.rows = [row]
    with pytest.raises(PublicationError, match="publication_uncertain"):
        GitHubPublisher(host).ensure_pr({**spec, "reconcile_only": True})
    assert host.created == 0


def test_inspect_linked_worktree_and_symlink(repo, tmp_path):
    linked = tmp_path / "isolated-worktree"
    git(repo, "worktree", "add", "-b", "task/child", str(linked), "main")
    alias = tmp_path / "alias"
    alias.symlink_to(linked, target_is_directory=True)
    result = inspect_repository(alias)
    assert result["branch"] == "task/child"
    assert result["repository"] == "example/public-project"


def test_missing_host_tool_has_safe_blocking_code(repo):
    spec = specification(repo)

    def runner(argv, cwd):
        if argv[0] == "gh":
            raise FileNotFoundError("private local detail")
        return command_runner(argv, cwd)

    with pytest.raises(PublicationError) as caught:
        GitHubPublisher(runner).ensure_pr(spec)
    assert str(caught.value) == "publication_host_unavailable"


def test_missing_base_prevents_create(repo):
    spec = specification(repo)
    host = Host(spec)

    def runner(argv, cwd):
        if argv[-1].endswith("/git/ref/heads/main"):
            raise subprocess.CalledProcessError(1, argv, stderr="Not found")
        return host(argv, cwd)

    with pytest.raises(PublicationError, match="publication_host_unavailable"):
        GitHubPublisher(runner).ensure_pr(spec)
    assert host.created == 0


@pytest.mark.parametrize("field", ["body", "merged_at"])
def test_invalid_pr_response_schema_blocks(repo, field):
    spec = specification(repo)
    host = Host(spec)
    row = host.row()
    row[field] = 123
    host.rows = [row]
    with pytest.raises(PublicationError, match="publication_host_response_invalid"):
        GitHubPublisher(host).ensure_pr(spec)
    assert host.created == 0


def test_intent_callback_runs_after_preflight_immediately_before_create(repo):
    spec = specification(repo)
    host = Host(spec)
    events = []

    def runner(argv, cwd):
        events.append(tuple(argv[:3]))
        if argv[:3] == ["gh", "pr", "create"]:
            assert events[-2] == "intent"
        return host(argv, cwd)

    def persist():
        assert host.created == 0
        assert host.calls[-1][0][-1].endswith("/git/ref/heads/main")
        events.append("intent")

    GitHubPublisher(runner).ensure_pr(spec, before_create=persist)
    assert events.count("intent") == 1
    assert host.created == 1


@pytest.mark.parametrize("condition", ["dirty", "existing", "reconcile"])
def test_preflight_or_existing_pr_never_persists_new_intent(repo, condition):
    spec = specification(repo)
    host = Host(spec)
    calls = []
    if condition == "dirty":
        (repo / "uncommitted").write_text("work in progress")
    elif condition == "existing":
        host.rows = [host.row()]
    else:
        spec["reconcile_only"] = True
    if condition == "existing":
        assert GitHubPublisher(host).ensure_pr(
            spec, before_create=lambda: calls.append(1)
        )
    else:
        with pytest.raises(PublicationError) as caught:
            GitHubPublisher(host).ensure_pr(spec, before_create=lambda: calls.append(1))
        assert caught.value.emitted is False
    assert calls == []
    assert host.created == 0


def test_intent_callback_failure_blocks_create_and_sanitizes(repo):
    spec = specification(repo)
    host = Host(spec)

    def fail_intent():
        raise RuntimeError("private persistence detail")

    with pytest.raises(PublicationError) as caught:
        GitHubPublisher(host).ensure_pr(spec, before_create=fail_intent)
    assert str(caught.value) == "publication_intent_failed"
    assert caught.value.emitted is False
    assert host.created == 0


@pytest.mark.parametrize("condition", ["visibility", "query_failure"])
def test_post_create_failure_marks_possible_emission(repo, condition):
    spec = specification(repo)
    host = Host(spec)
    host.lose_response = True
    if condition == "visibility":
        host.visible = False
    else:
        host.read_failure = True
    persisted = []
    with pytest.raises(PublicationError) as caught:
        GitHubPublisher(host).ensure_pr(spec, before_create=lambda: persisted.append(1))
    assert str(caught.value) == "publication_uncertain"
    assert caught.value.emitted is True
    assert persisted == [1]
    assert host.created == 1


def test_runner_rejects_git_environment_redirection(repo, tmp_path, monkeypatch):
    other = new_repository(tmp_path / "other-checkout")
    git(other, "remote", "set-url", "origin", "https://github.com/example/wrong.git")
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "remote.origin.url")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "https://github.com/example/injected.git")
    result = inspect_repository(repo)
    assert result["repository"] == "example/public-project"
    assert result["branch"] == "task/example"


def test_runner_sanitizes_environment_for_gh_subprocess_too(repo, monkeypatch):
    monkeypatch.setenv("GIT_DIR", "/untrusted/git")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/untrusted/config")

    def run(argv, **kwargs):
        env = kwargs["env"]
        assert "GIT_DIR" not in env
        assert env["GIT_CONFIG_GLOBAL"] != "/untrusted/config"
        assert env["GIT_CONFIG_NOSYSTEM"] == "1"
        assert kwargs["cwd"] == repo
        return subprocess.CompletedProcess(argv, 0, stdout="{}")

    monkeypatch.setattr(subprocess, "run", run)
    assert command_runner(["gh", "api", "user"], repo) == "{}"
