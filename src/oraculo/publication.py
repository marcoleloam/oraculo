"""GitHub publication with explicit repository identity and observable receipts.

The runner is ``runner(argv: list[str], cwd: Path) -> str`` and must raise on
nonzero exit or timeout. It never invokes a shell. This adapter never pushes,
merges, links T3 threads, or writes memory. The caller owns those separate gates.

The caller MUST serialize publication per task, persist an intent via the
``before_create`` callback, and use ``reconcile_only=True`` on replay after
uncertain emission. Preflight failures do not invoke the callback. An existing
intent must be preserved even when a replay fails before issuing any command.
GitHub creation has no idempotency key: an empty query after a timeout does not
authorize a second creation. An uncertain attempt requires later observation
or explicit operator resolution, not blind retry.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit

Runner = Callable[[list[str], Path], str]


class PublicationError(RuntimeError):
    """Stable safe code only; never propagate raw command output or credentials."""

    def __init__(self, code: str, *, emitted: bool = False):
        self.code = code
        # True means this call may have emitted a create, not that GitHub
        # confirmed it. False cannot erase an earlier durable intent.
        self.emitted = emitted
        super().__init__(code)


def command_runner(argv: list[str], cwd: Path) -> str:
    env = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, LC_ALL="C")
    env["GIT_NO_REPLACE_OBJECTS"] = "1"
    env["GIT_GRAFT_FILE"] = os.devnull
    return subprocess.run(
        argv, cwd=cwd, env=env, capture_output=True, text=True, check=True, timeout=60
    ).stdout.strip()


def _run(runner: Runner, argv: list[str], cwd: Path, code: str) -> str:
    try:
        return runner(argv, cwd).strip()
    except (OSError, subprocess.SubprocessError):
        raise PublicationError(code) from None


def _github_repository(remote_url: str) -> str:
    if remote_url.startswith("git@github.com:"):
        path = remote_url.removeprefix("git@github.com:")
    else:
        try:
            parsed = urlsplit(remote_url)
            if parsed.hostname != "github.com":
                raise PublicationError("publication_host_unsupported")
            if (
                parsed.scheme not in {"https", "ssh"}
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
                or parsed.port is not None
                or (parsed.scheme == "https" and parsed.username is not None)
                or (parsed.scheme == "ssh" and parsed.username != "git")
            ):
                raise PublicationError("publication_remote_invalid")
            path = parsed.path.removeprefix("/")
        except ValueError:
            raise PublicationError("publication_remote_invalid") from None
    path = path.removesuffix(".git")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+", path):
        raise PublicationError("publication_remote_invalid")
    if path.split("/")[1] in {".", ".."}:
        raise PublicationError("publication_remote_invalid")
    return path


def inspect_repository(
    cwd: str | Path,
    *,
    remote: str = "origin",
    base: str = "main",
    runner: Runner = command_runner,
) -> dict:
    """Inspect the selected remote; never infer a repository from a folder name."""
    cwd = Path(cwd).resolve()
    if not isinstance(remote, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", remote):
        raise PublicationError("publication_remote_invalid")
    if (
        remote.startswith("-")
        or not isinstance(base, str)
        or not base
        or base.startswith("-")
    ):
        raise PublicationError("publication_spec_invalid")
    _run(runner, ["git", "rev-parse", "--show-toplevel"], cwd, "publication_not_git")
    _run(
        runner,
        ["git", "check-ref-format", "refs/heads/" + base],
        cwd,
        "publication_base_invalid",
    )
    urls = _run(
        runner,
        ["git", "remote", "get-url", "--all", remote],
        cwd,
        "publication_remote_missing",
    ).splitlines()
    if len(urls) != 1:
        raise PublicationError("publication_remote_ambiguous")
    from oraculo.azure_repos import azure_repository

    azure = azure_repository(urls[0])
    repository = azure["repository"] if azure else _github_repository(urls[0])
    branch = _run(
        runner,
        ["git", "symbolic-ref", "--quiet", "--short", "HEAD"],
        cwd,
        "publication_detached_head",
    )
    head = _run(
        runner,
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd,
        "publication_head_missing",
    )
    if not re.fullmatch(r"(?:[a-f0-9]{40}|[a-f0-9]{64})", head):
        raise PublicationError("publication_head_invalid")
    return {
        **(azure or {"host": "github"}),
        "repository": repository,
        "remote": remote,
        "remote_url": azure["remote_url"]
        if azure
        else f"https://github.com/{repository}.git",
        "base": base,
        "head_sha": head,
        "branch": branch,
    }


def integration_binding(spec: dict, runner: Runner = command_runner) -> dict:
    """Prove every declared commit using local Git objects, never tree equality.

    No fetch is attempted: unavailable objects require explicit operator recovery.
    The managed caller separately binds source_commit to the immutable report.
    """
    integration = spec.get("integration")
    if integration is None:
        if (
            (spec.get("pr_id") or spec.get("pr_url"))
            and spec.get("source_commit")
            and spec["source_commit"] != spec.get("head_sha")
        ):
            raise PublicationError("publication_integration_required")
        return {}
    sha = r"(?:[a-f0-9]{40}|[a-f0-9]{64})"
    if (
        not isinstance(integration, dict)
        or set(integration) != {"head_sha", "contains"}
        or integration.get("head_sha") != spec.get("head_sha")
        or not isinstance(integration.get("head_sha"), str)
        or not re.fullmatch(sha, integration["head_sha"])
        or not isinstance(integration.get("contains"), list)
        or not integration["contains"]
        or len(integration["contains"]) > 1000
        or any(
            not isinstance(c, str) or not re.fullmatch(sha, c)
            for c in integration["contains"]
        )
        or len(set(integration["contains"])) != len(integration["contains"])
        or spec.get("source_commit") not in integration["contains"]
        or spec.get("reconcile_only") is not True
        or not (spec.get("pr_id") or spec.get("pr_url"))
    ):
        raise PublicationError("publication_integration_invalid")
    cwd = Path(spec["cwd"]).resolve()
    for commit in [integration["head_sha"], *integration["contains"]]:
        resolved = _run(
            runner,
            ["git", "rev-parse", "--verify", commit + "^{commit}"],
            cwd,
            "publication_integration_commit_missing",
        )
        if resolved != commit:
            raise PublicationError("publication_integration_commit_mismatch")
    for commit in integration["contains"]:
        _run(
            runner,
            ["git", "merge-base", "--is-ancestor", commit, integration["head_sha"]],
            cwd,
            "publication_integration_not_ancestor",
        )
    return {
        "integration": {
            "head_sha": integration["head_sha"],
            "contains": list(integration["contains"]),
        }
    }


class RepositoryPublisher:
    """Select the adapter from the selected Git remote, never a caller host hint."""

    def __init__(self, runner: Runner = command_runner, *, azure_factory=None):
        self.runner, self.azure_factory = runner, azure_factory

    def ensure_pr(self, spec, *, before_create=None):
        from oraculo.azure_repos import AzureRepos

        metadata = inspect_repository(
            spec["cwd"], remote=spec["remote"], base=spec["base"], runner=self.runner
        )
        if metadata["host"] == "azure":
            adapter = (
                self.azure_factory(spec)
                if self.azure_factory
                else AzureRepos.from_spec(spec, self.runner)
            )
        else:
            adapter = GitHubPublisher(self.runner)
        receipt = adapter.ensure_pr(spec, before_create=before_create)
        if metadata["host"] == "azure" and spec.get("credential") is not None:
            from oraculo.azure_repos import credential_reference

            receipt["credential"] = credential_reference(spec["credential"])
        return receipt


class GitHubPublisher:
    def __init__(self, runner: Runner = command_runner):
        self.runner = runner

    def _json(self, argv: list[str], cwd: Path) -> object:
        raw = _run(self.runner, argv, cwd, "publication_host_unavailable")
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            raise PublicationError("publication_host_response_invalid") from None

    def _lookup(
        self, metadata: dict, marker: str, cwd: Path, explicit=None
    ) -> dict | None:
        repo = metadata["repository"]
        query = urlencode(
            {
                "state": "all",
                "head": repo.split("/")[0] + ":" + metadata["branch"],
                "base": metadata["base"],
                "per_page": 100,
            }
        )
        pages = (
            [
                [
                    self._json(
                        [
                            "gh",
                            "api",
                            "--hostname",
                            "github.com",
                            f"repos/{repo}/pulls/{explicit}",
                        ],
                        cwd,
                    )
                ]
            ]
            if explicit
            else self._json(
                [
                    "gh",
                    "api",
                    "--hostname",
                    "github.com",
                    "--paginate",
                    "--slurp",
                    f"repos/{repo}/pulls?{query}",
                ],
                cwd,
            )
        )
        if not isinstance(pages, list) or any(not isinstance(p, list) for p in pages):
            raise PublicationError("publication_host_response_invalid")
        rows = [row for page in pages for row in page]
        candidates = []
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("body") or "", str):
                raise PublicationError("publication_host_response_invalid")
            try:
                matches = (
                    row["head"]["ref"] == metadata["branch"]
                    and row["base"]["ref"] == metadata["base"]
                    and row["head"]["repo"]["full_name"].lower() == repo.lower()
                    and row["base"]["repo"]["full_name"].lower() == repo.lower()
                )
            except (KeyError, TypeError, AttributeError):
                raise PublicationError("publication_host_response_invalid") from None
            if matches:
                candidates.append(row)
        marked = [r for r in candidates if marker in (r.get("body") or "")]
        if marked:
            candidates = marked
        if len(candidates) > 1:
            raise PublicationError("publication_pr_ambiguous")
        if not candidates:
            return None
        row = candidates[0]
        if row["head"].get("sha") != metadata["head_sha"]:
            raise PublicationError("publication_pr_head_mismatch")
        if (
            not explicit
            and "<!-- oraculo-task:" in (row.get("body") or "")
            and not marked
        ):
            raise PublicationError("publication_pr_task_mismatch")
        number = row.get("number")
        url = row.get("html_url")
        if (
            type(number) is not int
            or number < 1
            or (explicit is not None and number != explicit)
            or not isinstance(url, str)
            or url.lower() != f"https://github.com/{repo}/pull/{number}".lower()
            or row.get("state") not in {"open", "closed"}
            or (
                row.get("merged_at") is not None
                and (not isinstance(row["merged_at"], str) or row["state"] != "closed")
            )
        ):
            raise PublicationError("publication_host_response_invalid")
        state = "merged" if row.get("merged_at") else row["state"]
        return {
            "url": url,
            "head_sha": row["head"]["sha"],
            "base": row["base"]["ref"],
            "state": state,
            "repository": repo,
            "number": number,
            "host": "github",
        }

    def ensure_pr(
        self, spec: dict, *, before_create: Callable[[], None] | None = None
    ) -> dict:
        """Return an observed PR receipt, or a safe blocking error.

        Required: cwd, remote, base, head_sha, branch, title, body, task_id.
        ``reconcile_only`` forbids creation after an earlier durable intent.
        ``before_create`` persists the caller's intent immediately before the
        mutation, after all checks and body preparation. If it raises, no PR
        command is issued. Existing PRs do not invoke it.
        The task marker is a digest, never a raw internal identifier.
        """
        fields = ("remote", "base", "head_sha", "branch", "title", "task_id")
        if (
            any(not isinstance(spec.get(k), str) or not spec[k] for k in fields)
            or not isinstance(spec.get("cwd"), (str, Path))
            or not str(spec["cwd"])
            or not isinstance(spec.get("body"), str)
            or type(spec.get("reconcile_only", False)) is not bool
            or (before_create is not None and not callable(before_create))
        ):
            raise PublicationError("publication_spec_invalid")
        source_binding = {}
        if "source_project_id" in spec or "source_commit" in spec:
            if (
                not isinstance(spec.get("source_project_id"), str)
                or not spec["source_project_id"]
                or not isinstance(spec.get("source_commit"), str)
                or not re.fullmatch(
                    r"(?:[a-f0-9]{40}|[a-f0-9]{64})", spec["source_commit"]
                )
            ):
                raise PublicationError("publication_source_binding_invalid")
            # Caller-supplied provenance, not a claim that this adapter copied
            # or reviewed a private source tree. Only cwd is ever inspected.
            source_binding = {
                k: spec[k] for k in ("source_project_id", "source_commit")
            }
        cwd = Path(spec["cwd"]).resolve()
        metadata = inspect_repository(
            cwd,
            remote=spec["remote"],
            base=spec["base"],
            runner=self.runner,
        )
        if metadata["host"] != "github":
            raise PublicationError("publication_host_unsupported")
        integration = integration_binding(spec, self.runner)
        explicit = explicit_pr(
            spec, f"https://github.com/{metadata['repository']}/pull/"
        )
        if "repository" in spec and (
            not isinstance(spec["repository"], str)
            or spec["repository"].lower() != metadata["repository"].lower()
        ):
            raise PublicationError("publication_repository_mismatch")
        if (
            metadata["head_sha"] != spec["head_sha"]
            or metadata["branch"] != spec["branch"]
        ):
            raise PublicationError("publication_workspace_changed")
        if metadata["branch"] == metadata["base"]:
            raise PublicationError("publication_branch_is_base")
        if _run(
            self.runner,
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd,
            "publication_workspace_unreadable",
        ):
            raise PublicationError("publication_workspace_dirty")
        repo = metadata["repository"]
        identity = self._json(
            [
                "gh",
                "repo",
                "view",
                f"github.com/{repo}",
                "--json",
                "nameWithOwner,url",
            ],
            cwd,
        )
        if (
            not isinstance(identity, dict)
            or str(identity.get("nameWithOwner", "")).lower() != repo.lower()
            or str(identity.get("url", "")).lower()
            != f"https://github.com/{repo}".lower()
        ):
            raise PublicationError("publication_repository_mismatch")
        marker = (
            "<!-- oraculo-task:"
            + hashlib.sha256(str(spec["task_id"]).encode()).hexdigest()
            + " -->"
        )
        # Existing closed/merged PRs remain reconcilable after branch deletion.
        existing = self._lookup(metadata, marker, cwd, explicit)
        if existing:
            return {**existing, **source_binding, **integration}
        if spec.get("reconcile_only", False):
            raise PublicationError("publication_uncertain")
        for branch, expected in (
            (metadata["branch"], metadata["head_sha"]),
            (metadata["base"], None),
        ):
            ref = self._json(
                [
                    "gh",
                    "api",
                    "--hostname",
                    "github.com",
                    f"repos/{repo}/git/ref/heads/{quote(branch, safe='')}",
                ],
                cwd,
            )
            if not isinstance(ref, dict) or not isinstance(ref.get("object"), dict):
                raise PublicationError("publication_host_response_invalid")
            if (
                ref.get("ref") != "refs/heads/" + branch
                or ref["object"].get("type") != "commit"
            ):
                raise PublicationError("publication_remote_ref_mismatch")
            if not re.fullmatch(
                r"(?:[a-f0-9]{40}|[a-f0-9]{64})", str(ref["object"].get("sha", ""))
            ):
                raise PublicationError("publication_host_response_invalid")
            if expected and ref["object"].get("sha") != expected:
                raise PublicationError("publication_remote_head_mismatch")
        # --head explicitly disables gh's branch-pushing/forking prompts. The
        # branch has already been verified on this exact repository above.
        emitted = False
        try:
            with tempfile.TemporaryDirectory(prefix="oraculo-pr-") as folder:
                body_path = Path(folder) / "body.md"
                body_path.write_text(spec["body"] + "\n\n" + marker + "\n")
                if before_create is not None:
                    try:
                        before_create()
                    except Exception:
                        raise PublicationError("publication_intent_failed") from None
                emitted = True
                try:
                    _run(
                        self.runner,
                        [
                            "gh",
                            "pr",
                            "create",
                            "--repo",
                            f"github.com/{repo}",
                            "--head",
                            metadata["branch"],
                            "--base",
                            metadata["base"],
                            "--title",
                            spec["title"],
                            "--body-file",
                            str(body_path),
                        ],
                        cwd,
                        "publication_uncertain",
                    )
                except PublicationError:
                    # A lost response may follow a successful write. Observe
                    # once; the durable owner can reconcile again later.
                    pass
        except OSError:
            raise PublicationError(
                "publication_uncertain"
                if emitted
                else "publication_preparation_failed",
                emitted=emitted,
            ) from None
        try:
            receipt = self._lookup(metadata, marker, cwd)
        except PublicationError:
            raise PublicationError("publication_uncertain", emitted=True) from None
        if receipt is None:
            raise PublicationError("publication_uncertain", emitted=True)
        return {**receipt, **source_binding}


def explicit_pr(spec: dict, prefix: str) -> int | None:
    """Explicit external associations are observation-only and identity-bound."""
    number, url = spec.get("pr_id"), spec.get("pr_url")
    if number is None and url is None:
        return None
    if spec.get("reconcile_only") is not True:
        raise PublicationError("publication_existing_requires_reconcile_only")
    if number is not None and (type(number) is not int or number < 1):
        raise PublicationError("publication_pr_identity_invalid")
    if url is not None:
        if (
            not isinstance(url, str)
            or not url.startswith(prefix)
            or not re.fullmatch(r"[1-9][0-9]*", url[len(prefix) :])
        ):
            raise PublicationError("publication_pr_identity_invalid")
        parsed = int(url[len(prefix) :])
        if number is not None and parsed != number:
            raise PublicationError("publication_pr_identity_invalid")
        number = parsed
    return number
