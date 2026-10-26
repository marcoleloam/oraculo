"""Azure Repos REST observation and guarded mutations (Git 7.1).

Policy evaluations use Microsoft's documented 7.1-preview.1 endpoint. No
credentials, response bodies or server-provided error messages escape this port.
No redirect, shell, automatic retry, bypass, push or watcher is used here.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import stat
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote, unquote, urlencode, urlsplit

from oraculo.publication import (
    PublicationError,
    _run,
    command_runner,
    explicit_pr,
    inspect_repository,
    integration_binding,
)

_SHA = re.compile(r"[a-f0-9]{40}")
_UUID = re.compile(r"[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}")
_PAGE = 100


def azure_repository(remote: str) -> dict | None:
    """HTTPS Azure Services identity, with canonical escaped path components."""
    try:
        p = urlsplit(remote)
        if p.hostname != "dev.azure.com":
            return None
        if (
            p.scheme != "https"
            or p.password is not None
            or p.port is not None
            or p.query
            or p.fragment
        ):
            raise PublicationError("publication_remote_invalid")
        parts = p.path.strip("/").split("/")
        if len(parts) != 4 or parts[2] != "_git":
            raise PublicationError("publication_remote_invalid")
        org, project, _, repo = [unquote(v, errors="strict") for v in parts]
        # Azure clone URLs commonly include a login hint. It is not an API
        # credential and must never enter receipts or outgoing REST URLs.
        if p.username is not None:
            username = unquote(p.username, errors="strict")
            if (
                not username
                or len(username) > 512
                or any(ord(c) < 32 for c in username)
            ):
                raise PublicationError("publication_remote_invalid")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*", org):
            raise PublicationError("publication_remote_invalid")
        for value in (project, repo):
            if (
                not value
                or value in {".", ".."}
                or any(ord(c) < 32 or c in "/\\?#%" for c in value)
            ):
                raise PublicationError("publication_remote_invalid")
        root = "https://dev.azure.com/" + "/".join(
            quote(v, safe="") for v in (org, project)
        )
        return {
            "host": "azure",
            "organization": org,
            "project": project,
            "repo_name": repo,
            "repository": "/".join((org, project, repo)),
            "remote_url": root + "/_git/" + quote(repo, safe=""),
        }
    except (ValueError, UnicodeError):
        raise PublicationError("publication_remote_invalid") from None


def _pat(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,512}", value):
        raise PublicationError("azure_credential_invalid")
    return value


def load_pat(cwd: Path, credential=None, *, backend=None):
    """Only the selected checkout's literal assignment or an explicit Keychain ref.

    T3 OAuth's existing Keychain namespace is unrelated and is never probed. No
    guessed service/account name, parent .env, environment scan or host secret API.
    """
    if credential is not None:
        if (
            not isinstance(credential, dict)
            or set(credential) != {"keychain_service", "keychain_account"}
            or any(not isinstance(v, str) or not v.strip() for v in credential.values())
        ):
            raise PublicationError("azure_credential_reference_invalid")
        try:
            if backend is None:
                from keyring.backends.macOS import Keyring

                backend = Keyring()
            value = backend.get_password(
                credential["keychain_service"], credential["keychain_account"]
            )
        except Exception:
            raise PublicationError("azure_credential_unavailable") from None
        if value is None:
            raise PublicationError("azure_credential_unavailable")
        return _pat(value)
    try:
        fd = os.open(Path(cwd) / ".env", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise PublicationError("azure_credential_invalid")
            text = stream.read(65537)
        if len(text) > 65536:
            raise PublicationError("azure_credential_invalid")
    except FileNotFoundError:
        raise PublicationError("azure_credential_unavailable") from None
    except (OSError, UnicodeError):
        raise PublicationError("azure_credential_unavailable") from None
    values = []
    for line in text.splitlines():
        match = re.fullmatch(r"\s*(?:export\s+)?SYS__AZDO_PAT\s*=\s*(.*?)\s*", line)
        if match:
            value = match[1]
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            values.append(value)
    if not values:
        raise PublicationError("azure_credential_unavailable")
    if len(values) != 1:
        raise PublicationError("azure_credential_invalid")
    return _pat(values[0])


def credential_reference(credential):
    """Validate references before the managed owner persists any intent."""
    if credential is not None and (
        not isinstance(credential, dict)
        or set(credential) != {"keychain_service", "keychain_account"}
        or any(
            not isinstance(v, str) or not v.strip() or len(v) > 200
            for v in credential.values()
        )
    ):
        raise PublicationError("azure_credential_reference_invalid")
    return credential


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class AzureHTTP:
    """Bounded HTTPS-only transport; injected opener enables local HTTP tests."""

    def __init__(self, pat, *, opener=None):
        self._authorization = (
            "Basic " + base64.b64encode((":" + _pat(pat)).encode()).decode()
        )
        self._opener = opener or urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect()
        )

    def __call__(self, method, url, body=None):
        p = urlsplit(url)
        if (
            p.scheme != "https"
            or p.hostname != "dev.azure.com"
            or p.username
            or p.password
            or p.port
        ):
            raise PublicationError("azure_endpoint_invalid")
        try:
            request = urllib.request.Request(
                url,
                data=json.dumps(body).encode() if body is not None else None,
                headers={
                    "Authorization": self._authorization,
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                method=method,
            )
            with self._opener.open(request, timeout=30) as response:
                if response.status not in (200, 201):
                    raise PublicationError("azure_host_unavailable")
                raw = response.read(4 * 1024 * 1024 + 1)
                if len(raw) > 4 * 1024 * 1024:
                    raise PublicationError("azure_response_limit")
                data = json.loads(raw)
                headers = {
                    "x-ms-continuationtoken": response.headers.get(
                        "x-ms-continuationtoken"
                    )
                }
        except urllib.error.HTTPError as exc:
            code = (
                "azure_auth_required"
                if exc.code in (401, 403)
                else "azure_redirect_refused"
                if 300 <= exc.code < 400
                else "azure_host_unavailable"
            )
            raise PublicationError(code) from None
        except PublicationError:
            raise
        except Exception:
            raise PublicationError("azure_host_unavailable") from None
        return data, headers


class AzureRepos:
    def __init__(self, transport, runner=command_runner):
        self.transport, self.runner = transport, runner

    @classmethod
    def from_spec(cls, spec, runner=command_runner):
        return cls(
            AzureHTTP(load_pat(Path(spec["cwd"]).resolve(), spec.get("credential"))),
            runner,
        )

    def _request(
        self, metadata, suffix, *, method="GET", body=None, params=None, policy=False
    ):
        root = "https://dev.azure.com/" + "/".join(
            quote(metadata[k], safe="") for k in ("organization", "project")
        )
        query = {"api-version": "7.1-preview.1" if policy else "7.1", **(params or {})}
        url = root + "/_apis/" + suffix + "?" + urlencode(query)
        try:
            data, headers = self.transport(method, url, body)
        except PublicationError:
            raise
        except Exception:
            raise PublicationError("azure_host_unavailable") from None
        if not isinstance(data, dict) or not isinstance(headers, dict):
            raise PublicationError("azure_response_invalid")
        return data, headers

    def _pages(self, m, suffix, *, params=None, mode="offset", policy=False):
        rows, seen, token = [], set(), None
        for _ in range(100):
            query = dict(params or {})
            if mode == "offset":
                query.update({"$top": _PAGE, "$skip": len(rows)})
            elif mode == "token":
                query["$top"] = _PAGE
                if token:
                    query["continuationToken"] = token
            data, headers = self._request(m, suffix, params=query, policy=policy)
            page = data.get("value")
            if (
                not isinstance(page, list)
                or any(not isinstance(r, dict) for r in page)
                or type(data.get("count")) is not int
                or data["count"] != len(page)
            ):
                raise PublicationError("azure_response_invalid")
            fingerprint = hashlib.sha256(
                json.dumps(page, sort_keys=True).encode()
            ).hexdigest()
            if page and fingerprint in seen:
                raise PublicationError("azure_pagination_invalid")
            seen.add(fingerprint)
            rows.extend(page)
            token = headers.get("x-ms-continuationtoken")
            if mode == "single":
                if token:
                    raise PublicationError("azure_pagination_invalid")
                return rows
            if mode == "token":
                if not token:
                    return rows
                if not isinstance(token, str) or token in seen or not page:
                    raise PublicationError("azure_pagination_invalid")
                seen.add(token)
            elif len(page) < _PAGE:
                return rows
        raise PublicationError("azure_pagination_limit")

    @staticmethod
    def _repo_path(m):
        return "git/repositories/" + quote(
            m.get("repository_id", m["repo_name"]), safe=""
        )

    def _identity(self, m):
        row, _ = self._request(m, self._repo_path(m))
        self._validate_repo(m, row)
        return {**m, "repository_id": row["id"], "project_id": row["project"]["id"]}

    @staticmethod
    def _validate_repo(m, row):
        try:
            valid = (
                isinstance(row, dict)
                and _UUID.fullmatch(row["id"])
                and _UUID.fullmatch(row["project"]["id"])
                and row["name"] == m["repo_name"]
                and row["project"]["name"] == m["project"]
                and azure_repository(row["remoteUrl"])
                == {k: m[k] for k in azure_repository(m["remote_url"])}
                and (not m.get("repository_id") or m["repository_id"] == row["id"])
                and (not m.get("project_id") or m["project_id"] == row["project"]["id"])
            )
        except (KeyError, TypeError, AttributeError, PublicationError):
            valid = False
        if not valid:
            raise PublicationError("azure_repository_mismatch")

    def _receipt(self, m, row):
        self._validate_repo(m, row.get("repository") if isinstance(row, dict) else None)
        try:
            number = row["pullRequestId"]
            source, target, status, merge_status = (
                row[k]
                for k in ("sourceRefName", "targetRefName", "status", "mergeStatus")
            )
            head, base = (
                row[k]["commitId"]
                for k in ("lastMergeSourceCommit", "lastMergeTargetCommit")
            )
            valid = (
                type(number) is int
                and number > 0
                and status in {"active", "completed", "abandoned"}
                and merge_status
                in {
                    "notSet",
                    "queued",
                    "conflicts",
                    "succeeded",
                    "rejectedByPolicy",
                    "failure",
                }
                and _SHA.fullmatch(head)
                and _SHA.fullmatch(base)
                and source == "refs/heads/" + m["branch"]
                and target == "refs/heads/" + m["base"]
                and not row.get("forkSource")
                and type(row["isDraft"]) is bool
            )
        except (KeyError, TypeError, AttributeError):
            valid = False
        if not valid:
            raise PublicationError("azure_pr_identity_invalid")
        if head != m["head_sha"]:
            raise PublicationError("pr_head_changed")
        merge_record = row.get("lastMergeCommit")
        if merge_record is not None and not isinstance(merge_record, dict):
            raise PublicationError("azure_response_invalid")
        merge = (merge_record or {}).get("commitId")
        if merge is not None and (
            not isinstance(merge, str) or not _SHA.fullmatch(merge)
        ):
            raise PublicationError("azure_response_invalid")
        if status == "completed" and (
            merge_status != "succeeded" or not merge or row["isDraft"]
        ):
            raise PublicationError("pr_merge_commit_unverified")
        return {
            k: m[k]
            for k in (
                "host",
                "repository",
                "organization",
                "project",
                "repo_name",
                "remote_url",
                "repository_id",
                "project_id",
            )
        } | {
            "number": number,
            "pullRequestId": number,
            "url": m["remote_url"] + "/pullrequest/" + str(number),
            "branch": m["branch"],
            "remote": m["remote"],
            "base": m["base"],
            "head_sha": head,
            "base_sha": base,
            "status": status,
            "mergeStatus": merge_status,
            "lastMergeSourceCommit": {"commitId": head},
            "lastMergeTargetCommit": {"commitId": base},
            "lastMergeCommit": {"commitId": merge} if merge else None,
            "state": {"active": "open", "completed": "merged", "abandoned": "closed"}[
                status
            ],
            "merge_sha": merge if status == "completed" else None,
            "isDraft": row["isDraft"],
            "source": "azure_devops_rest",
            "observed_at": datetime.now(UTC).isoformat(),
        }

    def _get(self, m, number):
        row, _ = self._request(m, self._repo_path(m) + "/pullrequests/" + str(number))
        receipt = self._receipt(m, row)
        if receipt["number"] != number:
            raise PublicationError("azure_pr_identity_invalid")
        return row, receipt

    def _lookup(self, m, spec):
        number = explicit_pr(spec, m["remote_url"] + "/pullrequest/")
        if number is not None:
            return self._get(m, number)[1]
        rows = self._pages(
            m,
            self._repo_path(m) + "/pullrequests",
            params={
                "searchCriteria.status": "all",
                "searchCriteria.sourceRefName": "refs/heads/" + m["branch"],
                "searchCriteria.targetRefName": "refs/heads/" + m["base"],
            },
        )
        if len(rows) > 1:
            raise PublicationError("publication_pr_ambiguous")
        if not rows:
            return None
        number = rows[0].get("pullRequestId")
        if type(number) is not int or number < 1:
            raise PublicationError("azure_response_invalid")
        # List responses truncate description; retrieve the full record.
        row, receipt = self._get(m, number)
        marker = (
            "<!-- oraculo-task:"
            + hashlib.sha256(spec["task_id"].encode()).hexdigest()
            + " -->"
        )
        description = row.get("description") or ""
        if not isinstance(description, str):
            raise PublicationError("azure_response_invalid")
        if "<!-- oraculo-task:" in description and marker not in description:
            raise PublicationError("publication_pr_task_mismatch")
        return receipt

    def _ref(self, m, branch):
        rows = self._pages(
            m,
            self._repo_path(m) + "/refs",
            params={"filter": "heads/" + branch},
            mode="token",
        )
        exact = [r for r in rows if r.get("name") == "refs/heads/" + branch]
        if (
            len(exact) != 1
            or not isinstance(exact[0].get("objectId"), str)
            or not _SHA.fullmatch(exact[0]["objectId"])
        ):
            raise PublicationError("publication_remote_ref_mismatch")
        return exact[0]["objectId"]

    def ensure_pr(self, spec, *, before_create=None):
        if (
            type(spec.get("reconcile_only", False)) is not bool
            or any(
                not isinstance(spec.get(k), str) or not spec[k]
                for k in ("base", "branch", "head_sha", "remote", "task_id", "title")
            )
            or not isinstance(spec.get("body"), str)
        ):
            raise PublicationError("publication_spec_invalid")
        m = inspect_repository(
            spec["cwd"], remote=spec["remote"], base=spec["base"], runner=self.runner
        )
        if (
            m["host"] != "azure"
            or spec.get("repository", m["repository"]) != m["repository"]
        ):
            raise PublicationError("publication_repository_mismatch")
        if (
            m["head_sha"] != spec["head_sha"]
            or m["branch"] != spec["branch"]
            or m["branch"] == m["base"]
        ):
            raise PublicationError("publication_workspace_changed")
        if _run(
            self.runner,
            ["git", "status", "--porcelain", "--untracked-files=all"],
            Path(spec["cwd"]),
            "publication_workspace_unreadable",
        ):
            raise PublicationError("publication_workspace_dirty")
        binding = integration_binding(spec, self.runner)
        if "source_commit" in spec or "source_project_id" in spec:
            if (
                not isinstance(spec.get("source_project_id"), str)
                or not spec["source_project_id"]
                or not isinstance(spec.get("source_commit"), str)
                or not _SHA.fullmatch(spec["source_commit"])
            ):
                raise PublicationError("publication_source_binding_invalid")
            binding.update({k: spec[k] for k in ("source_commit", "source_project_id")})
        m = self._identity(m)
        existing = self._lookup(m, spec)
        if existing:
            return {**existing, **binding}
        if spec.get("reconcile_only"):
            raise PublicationError("publication_uncertain")
        if self._ref(m, m["branch"]) != m["head_sha"]:
            raise PublicationError("publication_remote_head_mismatch")
        self._ref(m, m["base"])
        marker = (
            "<!-- oraculo-task:"
            + hashlib.sha256(spec["task_id"].encode()).hexdigest()
            + " -->"
        )
        if before_create:
            try:
                before_create()
            except Exception:
                raise PublicationError("publication_intent_failed") from None
        try:
            self._request(
                m,
                self._repo_path(m) + "/pullrequests",
                method="POST",
                body={
                    "sourceRefName": "refs/heads/" + m["branch"],
                    "targetRefName": "refs/heads/" + m["base"],
                    "title": spec["title"],
                    "description": spec["body"] + "\n\n" + marker,
                    "isDraft": False,
                },
            )
        except PublicationError:
            pass
        try:
            receipt = self._lookup(m, spec)
        except PublicationError:
            raise PublicationError("publication_uncertain", emitted=True) from None
        if receipt is None:
            raise PublicationError("publication_uncertain", emitted=True)
        return {**receipt, **binding}

    def inspect(self, pr, policy, cwd):
        if not isinstance(policy, dict) or not isinstance(pr, dict):
            raise PublicationError("pr_merge_policy_invalid")
        checks = policy.get("required_checks")
        if (
            policy.get("merge_authorized") is not True
            and policy.get("observe_only") is not True
            or policy.get("repository") != pr.get("repository")
            or policy.get("head_sha") != pr.get("head_sha")
            or policy.get("base") != pr.get("base")
            or type(policy.get("required_review")) is not bool
            or type(policy.get("merge_authorized", False)) is not bool
            or type(policy.get("observe_only", False)) is not bool
            or type(policy.get("allow_no_checks", False)) is not bool
            or not isinstance(checks, list)
            or any(not isinstance(c, str) or not c for c in checks)
            or len(set(checks)) != len(checks)
            or policy.get("method") not in {"merge", "squash", "rebase"}
        ):
            raise PublicationError("pr_merge_policy_invalid")
        if not checks and not policy.get("allow_no_checks"):
            raise PublicationError("pr_required_checks_unspecified")
        m = inspect_repository(
            cwd,
            remote=pr.get("remote", "origin"),
            base=policy["base"],
            runner=self.runner,
        )
        if (
            policy.get("remote", m["remote"]) != m["remote"]
            or m["host"] != "azure"
            or any(
                m.get(k) != pr.get(k)
                for k in ("repository", "branch", "head_sha", "remote_url")
            )
        ):
            raise PublicationError("pr_policy_binding_mismatch")
        m = self._identity(m)
        if any(m[k] != pr.get(k) for k in ("repository_id", "project_id")):
            raise PublicationError("pr_remote_identity_mismatch")
        row, receipt = self._get(m, pr["number"])
        if receipt["url"] != pr.get("url") or receipt["pullRequestId"] != pr.get(
            "pullRequestId"
        ):
            raise PublicationError("pr_remote_identity_mismatch")
        receipt.update(
            ready=False,
            blockers=[],
            checks=[],
            policies=[],
            review_decision=None,
            policy_sha256=hashlib.sha256(
                json.dumps(policy, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
        )
        if receipt["state"] == "merged":
            return receipt
        if receipt["state"] == "closed":
            receipt["blockers"] = ["pr_closed_without_merge"]
            return receipt
        blockers = []
        if self._ref(m, m["branch"]) != m["head_sha"]:
            raise PublicationError("pr_head_changed")
        if self._ref(m, m["base"]) != receipt["base_sha"]:
            blockers.append("pr_target_changed")
        if receipt["isDraft"]:
            blockers.append("pr_is_draft")
        if receipt["mergeStatus"] != "succeeded":
            blockers.append(
                "pr_merge_conflicts"
                if receipt["mergeStatus"] == "conflicts"
                else "pr_mergeability_pending"
            )
        policies = self._pages(
            m,
            "policy/evaluations",
            policy=True,
            params={
                "artifactId": f"vstfs:///CodeReview/CodeReviewId/{m['project_id']}/{pr['number']}",
                "includeNotApplicable": "true",
            },
        )
        ids = set()
        for evaluation in policies:
            config, state = evaluation.get("configuration"), evaluation.get("status")
            if (
                not isinstance(config, dict)
                or type(config.get("id")) is not int
                or type(config.get("isBlocking")) is not bool
                or type(config.get("isEnabled")) is not bool
                or state
                not in {
                    "queued",
                    "running",
                    "approved",
                    "rejected",
                    "notApplicable",
                    "broken",
                }
                or config["id"] in ids
            ):
                raise PublicationError("azure_policy_response_invalid")
            ids.add(config["id"])
            receipt["policies"].append(
                {
                    "id": config["id"],
                    "blocking": config["isBlocking"],
                    "enabled": config["isEnabled"],
                    "status": state,
                }
            )
            if (
                config["isEnabled"]
                and config["isBlocking"]
                and state not in {"approved", "notApplicable"}
            ):
                blockers.append(
                    "pr_policy_pending"
                    if state in {"queued", "running"}
                    else "pr_policy_failed"
                )
        path = self._repo_path(m) + "/pullrequests/" + str(pr["number"])
        iterations = self._pages(m, path + "/iterations", mode="single")
        if (
            not iterations
            or any(type(i.get("id")) is not int or i["id"] < 1 for i in iterations)
            or len({i["id"] for i in iterations}) != len(iterations)
        ):
            raise PublicationError("azure_iteration_invalid")
        iteration = max(iterations, key=lambda i: i["id"])
        if (iteration.get("sourceRefCommit") or {}).get("commitId") != m[
            "head_sha"
        ] or (iteration.get("targetRefCommit") or {}).get("commitId") != receipt[
            "base_sha"
        ]:
            raise PublicationError("azure_iteration_head_mismatch")
        statuses = self._pages(m, path + "/statuses", mode="single")
        names, stale_required = set(), set()
        for status in statuses:
            context, state = status.get("context"), status.get("state")
            if (
                not isinstance(context, dict)
                or not isinstance(context.get("name"), str)
                or not context["name"]
                or not isinstance(context.get("genre", ""), str)
                or state
                not in {
                    "notSet",
                    "pending",
                    "succeeded",
                    "failed",
                    "error",
                    "notApplicable",
                }
            ):
                raise PublicationError("azure_status_response_invalid")
            name = (
                context.get("genre", "") + "/" if context.get("genre") else ""
            ) + context["name"]
            if status.get("iterationId") != iteration["id"]:
                if name in checks:
                    stale_required.add(name)
                continue
            if name in names:
                blockers.append("pr_check_context_ambiguous")
            names.add(name)
            receipt["checks"].append(
                {"name": name, "state": state, "iterationId": iteration["id"]}
            )
            if state != "succeeded":
                blockers.append(
                    "pr_checks_pending"
                    if state in {"notSet", "pending"}
                    else "pr_checks_failed"
                )
        # The endpoint returns statuses across iterations. Old results cannot
        # satisfy a current requirement or invalidate an exact-current result.
        if stale_required - names:
            blockers.append("pr_check_head_unverified")
        if set(checks) - names:
            blockers.append("pr_required_checks_missing")
        reviewers = row.get("reviewers")
        if not isinstance(reviewers, list) or any(
            not isinstance(r, dict)
            or not isinstance(r.get("id"), str)
            or type(r.get("vote")) is not int
            or r["vote"] not in {-10, -5, 0, 5, 10}
            or type(r.get("isRequired", False)) is not bool
            for r in reviewers
        ):
            raise PublicationError("azure_review_response_invalid")
        if any(r["vote"] < 0 for r in reviewers):
            blockers.append("pr_changes_requested")
        creator = (row.get("createdBy") or {}).get("id")
        approved = any(r["vote"] >= 5 and r["id"] != creator for r in reviewers)
        if (
            any(r.get("isRequired") and r["vote"] < 5 for r in reviewers)
            or policy["required_review"]
            and (not creator or not approved)
        ):
            blockers.append("pr_review_pending")
        receipt["review_decision"] = "APPROVED" if approved else "REVIEW_REQUIRED"
        fresh, second = self._get(m, pr["number"])
        if (
            any(
                second[k] != receipt[k]
                for k in ("head_sha", "base_sha", "status", "mergeStatus", "isDraft")
            )
            or fresh.get("reviewers") != reviewers
        ):
            raise PublicationError("pr_changed_during_check")
        receipt.update(blockers=list(dict.fromkeys(blockers)), ready=not blockers)
        return receipt

    def merge(self, pr, policy, cwd, *, before_merge=None, reconcile_only=False):
        if type(reconcile_only) is not bool:
            raise PublicationError("pr_merge_policy_invalid")
        observed = self.inspect(pr, policy, cwd)
        if observed["state"] == "merged":
            return observed
        if reconcile_only:
            raise PublicationError("pr_merge_uncertain")
        if policy.get("merge_authorized") is not True or policy.get("observe_only"):
            raise PublicationError("pr_merge_not_authorized")
        if not observed["ready"]:
            raise PublicationError(observed["blockers"][0])
        if before_merge:
            try:
                before_merge()
            except Exception:
                raise PublicationError("pr_merge_intent_failed") from None
        try:
            self._request(
                observed,
                self._repo_path(observed) + "/pullrequests/" + str(pr["number"]),
                method="PATCH",
                body={
                    "status": "completed",
                    "lastMergeSourceCommit": {"commitId": observed["head_sha"]},
                    "completionOptions": {
                        "mergeStrategy": {
                            "merge": "noFastForward",
                            "squash": "squash",
                            "rebase": "rebase",
                        }[policy["method"]],
                        "bypassPolicy": False,
                        "deleteSourceBranch": False,
                    },
                },
            )
        except PublicationError:
            pass
        try:
            final = self.inspect(pr, policy, cwd)
        except PublicationError as exc:
            raise PublicationError(exc.code, emitted=True) from None
        if final["state"] != "merged" or final["base_sha"] != observed["base_sha"]:
            raise PublicationError("pr_merge_uncertain", emitted=True)
        return final
