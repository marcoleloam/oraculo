"""Read-only authenticated repository CI adapters. No caller-signed payloads.

gh owns authentication; Azure reuses the repository lifecycle credential adapter.
Tests may inject a transport, which is laboratory evidence, never a live receipt.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from oraculo.ci_events import CIEvent
from oraculo.pr_lifecycle import RepositoryPRLifecycle
from oraculo.protocol import content_hash
from oraculo.publication import PublicationError
from oraculo.state import StateError


def _stamp(value):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        raise StateError("ci_host_timestamp_invalid") from None


class RepositoryCIObserver:
    identity = "authenticated-repository-ci-v1"

    def __init__(self, service, lifecycle=None):
        self.service = service
        self.lifecycle = lifecycle or RepositoryPRLifecycle()
        self.evidence = {}

    async def read(self, binding, project):
        cwd = Path(self.service.project(project)["path"])
        if self.service.state.connection.in_transaction:
            raise StateError("ci_io_inside_transaction")
        result = await asyncio.to_thread(self.lifecycle.observe_ci, binding, cwd)
        self.evidence = result["evidence"]
        return CIEvent.model_validate(result["event"])


def github_policy(runner, repository, base, head, cwd):
    """Read both branch protection and applicable rules. Missing access blocks."""

    def read(suffix):
        try:
            value = json.loads(
                runner(["gh", "api", "repos/" + repository + suffix], cwd)
            )
        except (OSError, ValueError):
            raise PublicationError("ci_host_policy_unavailable") from None
        return value

    branch = quote(base, safe="")
    protected = read("/branches/" + branch + "/protection")
    rules = read("/rules/branches/" + branch)
    if not isinstance(protected, dict) or not isinstance(rules, list):
        raise PublicationError("ci_host_policy_invalid")
    status = protected.get("required_status_checks") or {}
    checks = status.get("contexts", []) + [
        r.get("context") for r in status.get("checks", [])
    ]
    reviews = protected.get("required_pull_request_reviews") or {}
    count = reviews.get("required_approving_review_count", 0)
    if type(count) is not int or count < 0:
        raise PublicationError("ci_host_policy_invalid")
    for rule in rules:
        if not isinstance(rule, dict):
            raise PublicationError("ci_host_policy_invalid")
        params = rule.get("parameters") or {}
        if rule.get("type") == "required_status_checks":
            checks += [
                r.get("context") for r in params.get("required_status_checks", [])
            ]
        if rule.get("type") == "pull_request":
            required = params.get("required_approving_review_count")
            if type(required) is not int or required < 0:
                raise PublicationError("ci_host_policy_invalid")
            count = max(count, required)
        # A required workflow cannot be substituted by a similarly named check.
        if rule.get("type") == "workflows":
            raise PublicationError("ci_required_workflow_policy_unsupported")
    if not checks or any(not isinstance(c, str) or not c for c in checks):
        raise PublicationError("pr_required_checks_unspecified")
    return dict(
        repository=repository,
        base=base,
        head_sha=head,
        method="merge",
        observe_only=True,
        merge_authorized=False,
        required_checks=sorted(set(checks)),
        required_review=count > 0,
        allow_no_checks=False,
        policy_source="authenticated_github_protection_and_rules",
        review_count=count,
    )


def github_ci(lifecycle, binding, cwd):
    if (
        not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+", binding.repository
        )
        or not binding.run.isdecimal()
    ):
        raise StateError("ci_repository_or_run_invalid")
    runner = lifecycle.runner
    repo = binding.repository
    from oraculo.publication import discover_repository_remote

    remote = discover_repository_remote(
        cwd, repository=repo, host="github", runner=runner
    )

    def read(suffix):
        try:
            result = json.loads(runner(["gh", "api", "repos/" + repo + suffix], cwd))
        except (OSError, ValueError):
            raise StateError("ci_host_unavailable") from None
        if not isinstance(result, dict):
            raise StateError("ci_host_response_invalid")
        return result

    pull = read(f"/pulls/{binding.pr}")
    run = read(f"/actions/runs/{binding.run}")
    if (
        pull.get("number") != binding.pr
        or (pull.get("head") or {}).get("sha") != binding.head
        or (pull.get("base") or {}).get("sha") != binding.base
        or (pull.get("base", {}).get("repo") or {}).get("full_name", "").lower()
        != repo.lower()
        or str(run.get("id")) != binding.run
        or run.get("head_sha") != binding.head
        or run.get("run_attempt") != binding.attempt
        or (run.get("repository") or {}).get("full_name", "").lower() != repo.lower()
        or not any(p.get("number") == binding.pr for p in run.get("pull_requests", []))
    ):
        raise StateError("ci_host_binding_mismatch")
    base = pull["base"]["ref"]
    policy = github_policy(runner, repo, base, binding.head, cwd)
    pr = dict(
        host="github",
        repository=repo,
        base=base,
        head_sha=binding.head,
        number=binding.pr,
        url=f"https://github.com/{repo}/pull/{binding.pr}",
    )
    receipt = lifecycle.inspect(pr, policy, cwd)
    # Required checks may belong to multiple workflows (the public Oraculo
    # contract has Linux/macOS in CI and a separate orchestration workflow).
    checks = read(f"/commits/{binding.head}/check-runs?filter=latest&per_page=100")
    runs = read(f"/actions/runs?head_sha={binding.head}&per_page=100")
    check_rows, run_rows = checks.get("check_runs"), runs.get("workflow_runs")
    if (
        not isinstance(check_rows, list)
        or checks.get("total_count") != len(check_rows)
        or not isinstance(run_rows, list)
        or runs.get("total_count") != len(run_rows)
    ):
        raise StateError("ci_host_checks_or_runs_truncated")
    required = [c for c in check_rows if c.get("name") in policy["required_checks"]]
    if len({c.get("name") for c in required}) != len(required):
        raise StateError("ci_required_check_ambiguous")
    linked = {binding.run: run}
    for check in required:
        if (
            check.get("head_sha") != binding.head
            or (check.get("app") or {}).get("slug") != "github-actions"
        ):
            raise StateError("ci_check_source_unverified")
        suite = (check.get("check_suite") or {}).get("id")
        matches = [r for r in run_rows if r.get("check_suite_id") == suite]
        if suite is None or len(matches) != 1:
            raise StateError("ci_check_run_binding_unverified")
        candidate = matches[0]
        key = str(candidate["id"])
        fresh_run = run if key == binding.run else read(f"/actions/runs/{key}")
        if (
            fresh_run.get("head_sha") != binding.head
            or fresh_run.get("check_suite_id") != suite
            or (fresh_run.get("repository") or {}).get("full_name", "").lower()
            != repo.lower()
            or not any(
                p.get("number") == binding.pr
                for p in fresh_run.get("pull_requests", [])
            )
            or type(fresh_run.get("run_attempt")) is not int
            or fresh_run.get("run_attempt") != candidate.get("run_attempt")
        ):
            raise StateError("ci_host_binding_mismatch")
        linked[key] = fresh_run
    observed_jobs, check_bindings = [], []
    passed = set(c["name"] for c in required) == set(policy["required_checks"])
    for run_id, linked_run in linked.items():
        attempt = linked_run["run_attempt"]
        jobs = read(f"/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100")
        rows = jobs.get("jobs")
        if (
            not isinstance(rows, list)
            or jobs.get("total_count") != len(rows)
            or len({r.get("name") for r in rows}) != len(rows)
            or any(
                r.get("head_sha") != binding.head
                or str(r.get("run_id")) != run_id
                or r.get("run_attempt", attempt) != attempt
                for r in rows
            )
        ):
            raise StateError("ci_host_jobs_invalid_or_truncated")
        for check in required:
            if (check.get("check_suite") or {}).get("id") != linked_run.get(
                "check_suite_id"
            ):
                continue
            matched = [
                r
                for r in rows
                if r.get("name") == check["name"]
                and r.get("check_run_url")
                == f"https://api.github.com/repos/{repo}/check-runs/{check['id']}"
            ]
            if len(matched) == 1:
                # Semantic proof per required check, excluding unrelated jobs,
                # display fields and local observer clocks. Whole-endpoint hashes
                # remain in the audit evidence, not round identity/contradictions.
                job = matched[0]
                check_bindings.append(
                    dict(
                        name=check["name"],
                        run=run_id,
                        attempt=attempt,
                        head=binding.head,
                        check_id=check["id"],
                        suite=linked_run.get("check_suite_id"),
                        check_status=check.get("status"),
                        check_conclusion=check.get("conclusion"),
                        job={
                            k: job.get(k)
                            for k in (
                                "name",
                                "status",
                                "conclusion",
                                "head_sha",
                                "run_id",
                                "run_attempt",
                                "check_run_url",
                            )
                        },
                    )
                )
            passed = (
                passed
                and len(matched) == 1
                and matched[0].get("status") == "completed"
                and matched[0].get("conclusion") == "success"
            )
            passed = (
                passed
                and check.get("status") == "completed"
                and check.get("conclusion") == "success"
            )
        passed = (
            passed
            and linked_run.get("status") == "completed"
            and linked_run.get("conclusion") == "success"
        )
        observed_jobs.append(
            dict(run=run_id, attempt=attempt, jobs_hash=content_hash(jobs))
        )
    passed = (
        passed
        and receipt.get("ready") is True
        and pull.get("state") == "open"
        and pull.get("mergeable") is True
    )
    # A bounded final read catches source/base/run/policy changes during I/O.
    if (
        read(f"/pulls/{binding.pr}") != pull
        or discover_repository_remote(
            cwd, repository=repo, host="github", runner=runner
        )
        != remote
        or any(read(f"/actions/runs/{k}") != v for k, v in linked.items())
        or read(f"/commits/{binding.head}/check-runs?filter=latest&per_page=100")
        != checks
        or github_policy(runner, repo, base, binding.head, cwd) != policy
    ):
        raise StateError("ci_snapshot_changed")
    conclusion = "pending"
    if run.get("conclusion") == "cancelled":
        conclusion = "cancelled"
    elif run.get("status") == "completed":
        conclusion = (
            "passed" if run.get("conclusion") == "success" and passed else "failed"
        )
    observed = max(_stamp(r.get("updated_at")) for r in linked.values())
    evidence = dict(
        policy=policy,
        lifecycle=receipt,
        jobs_hash=content_hash(observed_jobs),
        required_checks_hash=content_hash(checks),
        runs=observed_jobs,
        check_bindings=sorted(check_bindings, key=lambda c: c["name"]),
        run_hash=content_hash(run),
        pull_hash=content_hash(pull),
        source="authenticated_gh_api",
        remote_binding=remote,
    )
    # Same upstream snapshot has the same identity; no synthesized local clock
    # may turn an old event into a fresh host event.
    event = binding.model_dump(mode="json") | dict(
        # Keep the existing upstream event identity: check_bindings is an
        # additive semantic projection of these same authenticated endpoints.
        event_id=content_hash(
            [
                binding.model_dump(),
                {k: v for k, v in evidence.items() if k != "check_bindings"},
                conclusion,
            ]
        ),
        sequence=max(1, int(observed * 1000000)),
        occurred_at=observed,
        conclusion=conclusion,
        diagnostic="test failure runner"
        if conclusion == "failed"
        and any(r.get("conclusion") == "failure" for r in linked.values())
        else "",
    )
    return dict(event=event, evidence=evidence)
