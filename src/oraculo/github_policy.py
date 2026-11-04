"""Authenticated GitHub policy discovery; explicit project policy never invents rules.

Only exact recognized feature diagnostics permit an unavailable capability. All
other transport/schema/access failures block. Observations belong in the private
managed ledger, never in public fixtures or command error strings.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from urllib.parse import quote

from oraculo.protocol import content_hash
from oraculo.publication import PublicationError

_LIMIT_MESSAGES = {
    "Upgrade to GitHub Pro or make this repository public to enable this feature.",
    "Upgrade to GitHub Pro or make this repository public to enable this feature",
}
_SHA = re.compile(r"[a-f0-9]{40}|[a-f0-9]{64}")


# Filter at gh's process boundary: patches, PR/review bodies, step output and
# commit messages must never enter the observer's receipts or canonical memory.
# Protection/rules responses are policy metadata and retain their exact body.
def _metadata_projection(endpoint):
    if endpoint == "user":
        return "{login,id}"
    if re.fullmatch(r"repos/[^/]+/[^/]+", endpoint):
        return "{id,full_name,private}"
    if re.fullmatch(r"repos/[^/]+/[^/]+/branches/[^/?]+", endpoint):
        return "{name,protected,commit:{sha:.commit.sha}}"
    if "/git/commits/" in endpoint:
        return "{sha,parents:[.parents[]? | {sha}]}"
    if "/compare/" in endpoint:
        return "{status,merge_base_commit:{sha:.merge_base_commit.sha}}"
    if re.search(r"/commits/[^/?]+/pulls\?per_page=100$", endpoint):
        return 'if type == "array" then map({number,head:{sha:.head.sha},base:{ref:.base.ref,repo:{full_name:.base.repo.full_name}},state}) else {invalid_type:type} end'
    if re.search(r"/pulls/\d+$", endpoint):
        return "{number,head:{sha:.head.sha},base:{sha:.base.sha,ref:.base.ref,repo:{full_name:.base.repo.full_name}},state,mergeable,merged,merge_commit_sha,merged_at}"
    if re.search(r"/pulls/\d+/reviews\?", endpoint):
        return "[.[] | {id,user:{id:.user.id,login:.user.login},state,commit_id}]"
    if re.search(r"/collaborators/[^/]+/permission$", endpoint):
        return "{permission,user:{id:.user.id,login:.user.login}}"
    if "/check-runs?" in endpoint:
        return "{total_count,check_runs:[.check_runs[] | {id,name,head_sha,app:{id:.app.id,slug:.app.slug},check_suite:{id:.check_suite.id},status,conclusion}]}"
    if "/actions/runs?" in endpoint:
        return "{total_count,workflow_runs:[.workflow_runs[] | {id,check_suite_id,run_attempt}]}"
    if re.search(r"/actions/runs/\d+$", endpoint):
        return '{id,check_suite_id,head_sha,run_attempt,event,repository:{full_name:.repository.full_name},updated_at,status,conclusion} + (if has("pull_requests") then {pull_requests:(.pull_requests | if type == "array" then map({number}) else {invalid_type:type} end)} else {} end)'
    if "/jobs?" in endpoint:
        return "{total_count,jobs:[.jobs[] | {id,name,status,conclusion,head_sha,run_id,run_attempt,check_run_url} | with_entries(select(.value != null))]}"
    return None


class GitHubPolicyError(PublicationError):
    def __init__(self, code, observations):
        self.observations = observations.copy()
        super().__init__(code)


class GitHubReader:
    """Bounded JSON transport with typed failures and exact response provenance."""

    def __init__(self, runner, cwd):
        self.runner, self.cwd, self.observations = runner, cwd, []

    def fail(self, code):
        raise GitHubPolicyError(code, self.observations)

    def read(self, endpoint, *, capability=False):
        argv = ["gh", "api", endpoint]
        projection = _metadata_projection(endpoint)
        if projection is not None:
            argv += ["--jq", projection]
        status, stderr, returncode = None, "", 0
        try:
            raw = self.runner(argv, self.cwd)
        except subprocess.CalledProcessError as exc:
            raw, stderr, returncode = exc.stdout or "", exc.stderr or "", exc.returncode
            status = None
        except (OSError, subprocess.SubprocessError) as exc:
            self.observations.append(
                dict(endpoint=endpoint, transport_error=type(exc).__name__)
            )
            self.fail("ci_host_policy_transport_unavailable")
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        if (
            not isinstance(raw, str)
            or len(raw.encode()) > 262144
            or not isinstance(stderr, str)
            or len(stderr.encode()) > 262144
        ):
            self.observations.append(
                dict(endpoint=endpoint, diagnostic="response_oversized")
            )
            self.fail("ci_host_policy_response_oversized")
        observation = dict(
            endpoint=endpoint,
            argv=argv,
            projection=projection,
            returncode=returncode,
            stdout=raw,
            stderr=stderr,
            source="authenticated_gh_api",
        )
        self.observations.append(observation)
        try:
            value = json.loads(raw)
        except (ValueError, TypeError):
            if (
                returncode
                and projection is not None
                and re.search(r"\(HTTP \d{3}\)", stderr)
            ):
                value = {}
            else:
                self.fail("ci_host_policy_response_invalid")
        if returncode:
            supplied = value.get("status") if isinstance(value, dict) else None
            match = re.search(r"\(HTTP (\d{3})\)", stderr)
            try:
                status = (
                    int(supplied)
                    if supplied is not None
                    else int(match[1])
                    if match
                    else None
                )
            except (ValueError, TypeError):
                status = None
            if match and status != int(match[1]):
                self.fail("ci_host_policy_response_invalid")
            observation["http_status"] = status
            message = value.get("message") if isinstance(value, dict) else None
            rate_messages = (
                "API rate limit exceeded",
                "You have exceeded a secondary rate limit",
            )
            if status == 403 and (
                (isinstance(message, str) and message.startswith(rate_messages))
                or stderr.startswith(tuple("gh: " + m for m in rate_messages))
            ):
                self.fail("ci_host_policy_rate_limited")
            if capability and status == 403 and message in _LIMIT_MESSAGES:
                return None, "feature_unavailable"
            if capability and status == 404 and message == "Branch not protected":
                return None, "not_protected"
            self.fail(
                {
                    401: "ci_host_policy_authentication_failed",
                    403: "ci_host_policy_access_denied",
                    404: "ci_host_policy_not_found",
                    429: "ci_host_policy_rate_limited",
                }.get(
                    status,
                    "ci_host_policy_server_error"
                    if status and status >= 500
                    else "ci_host_policy_http_error",
                )
            )
        observation["http_status"] = None
        observation["transport_result"] = "success"
        if isinstance(value, dict) and value.get("message") and value.get("status"):
            self.fail("ci_host_policy_response_invalid")
        return value, "available"


def github_policy(runner, repository, base, head, cwd, managed_policy=None):
    reader = GitHubReader(runner, cwd)

    def read(path):
        return reader.read(path)[0]

    actor = read("user")
    repo = read("repos/" + repository)
    branch = read("repos/" + repository + "/branches/" + quote(base, safe=""))
    commit = read("repos/" + repository + "/git/commits/" + head)
    if (
        not isinstance(actor, dict)
        or not isinstance(actor.get("login"), str)
        or not actor["login"]
        or type(actor.get("id")) is not int
        or not isinstance(repo, dict)
        or str(repo.get("full_name", "")).lower() != repository.lower()
        or type(repo.get("id")) is not int
        or type(repo.get("private")) is not bool
        or not isinstance(branch, dict)
        or branch.get("name") != base
        or type(branch.get("protected")) is not bool
        or not isinstance(branch.get("commit"), dict)
        or not _SHA.fullmatch(str(branch["commit"].get("sha", "")))
        or not isinstance(commit, dict)
        or commit.get("sha") != head
    ):
        reader.fail("ci_host_policy_identity_mismatch")
    protected, protection_state = reader.read(
        "repos/" + repository + "/branches/" + quote(base, safe="") + "/protection",
        capability=True,
    )
    # Always read applicable rules, including when classic protection is unavailable.
    rules, rules_state = reader.read(
        "repos/"
        + repository
        + "/rules/branches/"
        + quote(base, safe="")
        + "?per_page=100",
        capability=True,
    )
    if rules_state == "not_protected":
        reader.fail("ci_host_policy_not_found")
    if (
        "feature_unavailable" in {protection_state, rules_state}
        and repo["private"] is not True
    ):
        reader.fail("ci_host_policy_contradictory")
    if protection_state == "available" and (
        not isinstance(protected, dict)
        or not any(
            k in protected
            for k in ("url", "required_status_checks", "required_pull_request_reviews")
        )
    ):
        reader.fail("ci_host_policy_invalid")
    if rules_state == "available" and (
        not isinstance(rules, list) or len(rules) >= 100
    ):
        reader.fail("ci_host_policy_invalid_or_truncated")
    # protected includes rulesets, not just classic branch protection. A
    # conclusive classic absence must still honor every reachable active rule.
    # With neither classic proof nor active rules, a protected flag is ambiguous.
    if branch["protected"] and protected is None and not rules:
        reader.fail("ci_host_policy_contradictory")
    checks, count, sources, specialized_review = [], 0, [], False

    def rows(value):
        if not isinstance(value, list) or any(not isinstance(r, dict) for r in value):
            reader.fail("ci_host_policy_invalid")
        return value

    def review(value):
        if type(value) is not int or value < 0:
            reader.fail("ci_host_policy_invalid")
        return value

    def mapping(value):
        if value is None:
            return {}
        if not isinstance(value, dict):
            reader.fail("ci_host_policy_invalid")
        return value

    def specialized(params, keys):
        for key in keys:
            if key in params and type(params[key]) is not bool:
                reader.fail("ci_host_policy_invalid")
        return any(params.get(k) is True for k in keys)

    if protected is not None:
        status = mapping(protected.get("required_status_checks"))
        reviews = mapping(protected.get("required_pull_request_reviews"))
        if not isinstance(status, dict) or not isinstance(reviews, dict):
            reader.fail("ci_host_policy_invalid")
        contexts = status.get("contexts", [])
        if not isinstance(contexts, list):
            reader.fail("ci_host_policy_invalid")
        checks += contexts
        for row in rows(status.get("checks", [])):
            checks.append(row.get("context"))
            sources.append(dict(name=row.get("context"), app_id=row.get("app_id")))
        count = review(reviews.get("required_approving_review_count", 0))
        specialized_review = specialized(
            reviews, ("require_code_owner_reviews", "require_last_push_approval")
        )
    for rule in rows(rules or []):
        params = mapping(rule.get("parameters"))
        if not isinstance(params, dict) or not isinstance(rule.get("type"), str):
            reader.fail("ci_host_policy_invalid")
        if rule["type"] == "required_status_checks":
            for row in rows(params.get("required_status_checks")):
                checks.append(row.get("context"))
                sources.append(
                    dict(name=row.get("context"), app_id=row.get("integration_id"))
                )
        if rule["type"] == "pull_request":
            count = max(count, review(params.get("required_approving_review_count")))
            specialized_review = specialized_review or specialized(
                params, ("require_code_owner_review", "require_last_push_approval")
            )
        if rule["type"] == "workflows":
            reader.fail("ci_required_workflow_policy_unsupported")
    if any(not isinstance(c, str) or not c.strip() for c in checks):
        reader.fail("ci_host_policy_invalid")
    state = (
        "enforced"
        if protected is not None or rules
        else "feature_unavailable"
        if "feature_unavailable" in {protection_state, rules_state}
        else "unprotected"
    )
    if managed_policy is not None:
        policy = managed_policy
        required = policy.get("required_checks")
        if (
            any(
                policy.get(k) != v
                for k, v in dict(
                    repository=repository, base=base, head_sha=head
                ).items()
            )
            or not isinstance(required, list)
            or not required
            or any(not isinstance(c, str) or not c.strip() for c in required)
            or len(set(required)) != len(required)
            or type(policy.get("required_review")) is not bool
            or (
                not checks
                and (
                    not isinstance(policy.get("policy_source"), str)
                    or not policy["policy_source"].strip()
                )
            )
            or policy.get("allow_no_checks", False) is not False
            or (
                not checks
                and policy.get("observe_only") is not True
                and policy.get("merge_authorized") is not True
            )
        ):
            reader.fail("ci_explicit_project_policy_required")
        if not set(checks) <= set(required) or (
            (count or specialized_review) and not policy["required_review"]
        ):
            reader.fail("ci_actual_host_policy_not_satisfied")
        effective, required_review = required, policy["required_review"]
    else:
        if not checks:
            reader.fail("pr_required_checks_unspecified")
        effective, required_review = checks, count > 0 or specialized_review
    return dict(
        repository=repository,
        base=base,
        head_sha=head,
        method=managed_policy.get("method", "merge") if managed_policy else "merge",
        observe_only=True,
        merge_authorized=False,
        required_checks=sorted(set(effective)),
        required_review=required_review,
        allow_no_checks=False,
        policy_source="authenticated_github_protection_and_rules"
        if state == "enforced"
        else "origin_project_policy_with_authenticated_github_capability",
        project_policy_source=managed_policy.get("policy_source")
        if managed_policy
        else None,
        project_policy_sha256=content_hash(managed_policy) if managed_policy else None,
        remote_required_checks=sorted(set(checks)),
        remote_required_review=count > 0 or specialized_review,
        specialized_review=specialized_review,
        check_sources=sources,
        review_count=count,
        remote_capability=dict(
            state=state,
            protection=protection_state,
            rules=rules_state,
            actor=actor,
            repository_id=repo["id"],
            base_sha=branch["commit"]["sha"],
            observations=reader.observations,
        ),
    )


def verify_github_merge(runner, pr, policy, cwd, receipt, observed_policy):
    """Read-only confirmation of PR identity and actual original-head ancestry."""
    from oraculo.pr_lifecycle import _checks

    if observed_policy["specialized_review"]:
        raise GitHubPolicyError(
            "ci_historical_specialized_review_unproven",
            observed_policy["remote_capability"]["observations"],
        )

    reader = GitHubReader(runner, cwd)
    repo, head, merge = pr["repository"], pr["head_sha"], receipt["merge_sha"]
    pull, _ = reader.read(f"repos/{repo}/pulls/{pr['number']}")
    commit, _ = reader.read(f"repos/{repo}/git/commits/{merge}")
    comparison = None
    if policy["method"] == "merge":
        comparison, _ = reader.read(f"repos/{repo}/compare/{head}...{merge}")
    if (
        not isinstance(pull, dict)
        or pull.get("number") != pr["number"]
        or pull.get("merged") is not True
        or pull.get("state") != "closed"
        or pull.get("merge_commit_sha") != merge
        or not pull.get("merged_at")
        or (pull.get("head") or {}).get("sha") != head
        or (pull.get("base") or {}).get("ref") != pr["base"]
        or str(
            ((pull.get("base") or {}).get("repo") or {}).get("full_name", "")
        ).lower()
        != repo.lower()
        or not isinstance(commit, dict)
        or commit.get("sha") != merge
        or (
            policy["method"] == "merge"
            and (
                not isinstance(comparison, dict)
                or comparison.get("status") not in {"ahead", "identical"}
                or (comparison.get("merge_base_commit") or {}).get("sha") != head
            )
        )
    ):
        reader.fail("pr_merge_identity_or_ancestry_unverified")
    if policy["method"] == "merge" and (
        not isinstance(commit.get("parents"), list)
        or head
        not in [p.get("sha") for p in commit.get("parents", []) if isinstance(p, dict)]
    ):
        reader.fail("pr_merge_identity_or_ancestry_unverified")
    base_head = observed_policy["remote_capability"]["base_sha"]
    current_base, _ = reader.read(f"repos/{repo}/compare/{merge}...{base_head}")
    if (
        not isinstance(current_base, dict)
        or current_base.get("status") not in {"ahead", "identical"}
        or (current_base.get("merge_base_commit") or {}).get("sha") != merge
    ):
        reader.fail("pr_merge_not_in_current_base")
    # A fresh historical snapshot may be absent; never manufacture CI success.
    try:
        raw = runner(
            [
                "gh",
                "pr",
                "view",
                str(pr["number"]),
                "--repo",
                "github.com/" + repo,
                "--json",
                "number,url,headRefOid,baseRefName,state,statusCheckRollup,reviewDecision,mergeCommit",
            ],
            Path(cwd),
        )
    except (OSError, subprocess.SubprocessError):
        reader.fail("pr_host_unavailable")

    try:
        snapshot = json.loads(raw)
    except (TypeError, ValueError):
        reader.fail("pr_host_response_invalid")
    if not isinstance(snapshot, dict) or any(
        snapshot.get(k) != v
        for k, v in dict(
            number=pr["number"],
            url=pr["url"],
            headRefOid=head,
            baseRefName=pr["base"],
            state="MERGED",
            mergeCommit=dict(oid=merge),
        ).items()
    ):
        reader.fail("pr_merge_identity_or_ancestry_unverified")
    checks, blockers = _checks(
        snapshot.get("statusCheckRollup"), policy["required_checks"]
    )
    if snapshot.get("reviewDecision") == "CHANGES_REQUESTED":
        blockers.append("pr_changes_requested")
    if policy["required_review"]:
        reviews, _ = reader.read(
            f"repos/{repo}/pulls/{pr['number']}/reviews?per_page=100"
        )
        if not isinstance(reviews, list) or len(reviews) >= 100:
            reader.fail("pr_reviews_invalid_or_truncated")
        latest = {}
        for review in reviews:
            if (
                not isinstance(review, dict)
                or not isinstance(review.get("user"), dict)
                or type(review["user"].get("id")) is not int
                or type(review.get("id")) is not int
                or review.get("state")
                not in {
                    "APPROVED",
                    "CHANGES_REQUESTED",
                    "DISMISSED",
                    "COMMENTED",
                    "PENDING",
                }
            ):
                reader.fail("pr_reviews_invalid_or_truncated")
            if review["state"] not in {"COMMENTED", "PENDING"}:
                old = latest.get(review["user"]["id"])
                if old is None or old["id"] < review["id"]:
                    latest[review["user"]["id"]] = review
        approvals = [
            r
            for r in latest.values()
            if r["state"] == "APPROVED" and r.get("commit_id") == head
        ]
        eligible = []
        for review in approvals:
            user = review["user"]
            login = user.get("login")
            if not isinstance(login, str) or not login or len(login) > 100:
                reader.fail("pr_review_identity_unverified")
            permission, _ = reader.read(
                f"repos/{repo}/collaborators/{quote(login, safe='')}/permission"
            )
            if (
                not isinstance(permission, dict)
                or permission.get("permission")
                not in {"admin", "write", "maintain", "triage", "read", "none"}
                or not isinstance(permission.get("user"), dict)
                or type(permission["user"].get("id")) is not int
                or permission["user"]["id"] != user["id"]
                or str(permission["user"].get("login", "")).lower() != login.lower()
            ):
                reader.fail("pr_review_identity_unverified")
            if permission["permission"] in {"admin", "write", "maintain"}:
                eligible.append(review)
        if len(eligible) < max(1, observed_policy["review_count"]):
            blockers.append("pr_review_pending")
        if any(r["state"] == "CHANGES_REQUESTED" for r in latest.values()):
            blockers.append("pr_changes_requested")
    return dict(
        receipt,
        checks=checks,
        blockers=blockers,
        observation_timing="after_merge",
        merge_verification=dict(
            source="authenticated_gh_api",
            merge_sha=merge,
            original_head=head,
            ancestry="verified_parent_and_ancestor"
            if policy["method"] == "merge"
            else "host_pr_merge_identity",
            merged_at=pull["merged_at"],
            current_base_head=base_head,
            merge_in_current_base=True,
            review_floor=dict(
                required_review=policy["required_review"],
                remote_review_count=observed_policy["review_count"],
                specialized_review=observed_policy["specialized_review"],
            ),
            observations=reader.observations,
        ),
    )
