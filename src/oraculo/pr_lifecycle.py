"""Observe and merge a GitHub PR against an explicitly bound delivery policy.

The caller persists an immutable policy and an intent in ``before_merge``, then
uses ``reconcile_only`` after uncertain emission. There is no watcher or polling
loop here: call again on the host's PR notification. A merge queue acknowledgement
is not a verified merge. Only a fresh MERGED observation with the bound head/base
and a merge commit supplies a successful receipt.

Branch protection/rules remain enforced by GitHub; this adapter never uses admin
bypass, auto-merge, or local pushes. The repository facade discovers remote requirements,
which remain a floor for an explicit origin project policy. Recognized feature
unavailability requires a nonempty project policy; unknown discovery errors block.
The low-level adapter also supports an intentional allow_no_checks policy.
The policy digest is returned so its durable owner can verify the binding.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import subprocess
from collections.abc import Callable
from pathlib import Path

from oraculo.publication import PublicationError, Runner, command_runner

_SHA = re.compile(r"(?:[a-f0-9]{40}|[a-f0-9]{64})")
_FIELDS = (
    "number,url,headRefOid,baseRefName,state,isDraft,reviewDecision,mergeable,"
    "mergeStateStatus,statusCheckRollup,mergeCommit"
)


class RepositoryPRLifecycle:
    """REST observation is independent of native host watch capabilities."""

    def __init__(self, runner=command_runner, *, azure_factory=None):
        self.runner, self.azure_factory = runner, azure_factory

    def _adapter(self, pr, cwd):
        if pr.get("host") == "azure":
            from oraculo.azure_repos import AzureRepos

            spec = {"cwd": cwd, "credential": pr.get("credential")}
            return (
                self.azure_factory(spec)
                if self.azure_factory
                else AzureRepos.from_spec(spec, self.runner)
            )
        return GitHubPRLifecycle(self.runner)

    def inspect(self, pr, policy, cwd):
        receipt = self._adapter(pr, cwd).inspect(pr, policy, cwd)
        if pr.get("host") == "github" and receipt["state"] == "merged":
            from oraculo.github_policy import verify_github_merge

            observed_policy = self.validate_host_policy(pr, policy, cwd)
            receipt = verify_github_merge(
                self.runner, pr, policy, cwd, receipt, observed_policy
            )
        return receipt

    def observe_ci(self, binding, cwd):
        from oraculo.ci_host import github_ci

        if binding.repository.count("/") == 2:
            from oraculo.ci_azure import azure_ci

            return azure_ci(self, binding, cwd)
        return github_ci(self, binding, cwd)

    def observe_pr_ci(self, pr, policy, cwd):
        """Discover actual GitHub identities, then reuse the strict CI observer.

        Azure/external statuses currently expose only the authenticated lifecycle
        check snapshot here. No synthetic build ID or attempt is substituted.
        """
        if pr.get("host") != "github":
            return None
        from oraculo.ci_events import CIBinding
        from oraculo.github_policy import GitHubReader

        reader = GitHubReader(self.runner, Path(cwd))

        def read(suffix):
            value, _ = reader.read("repos/" + pr["repository"] + suffix)
            if not isinstance(value, dict):
                reader.fail("ci_host_response_invalid")
            return value

        pull = read(f"/pulls/{pr['number']}")
        checks = read(
            f"/commits/{pr['head_sha']}/check-runs?filter=latest&per_page=100"
        )
        runs = read(f"/actions/runs?head_sha={pr['head_sha']}&per_page=100")
        rows, workflows = checks.get("check_runs"), runs.get("workflow_runs")
        if (
            not isinstance(rows, list)
            or not isinstance(workflows, list)
            or checks.get("total_count") != len(rows)
            or runs.get("total_count") != len(workflows)
        ):
            raise PublicationError("ci_host_checks_or_runs_truncated")
        required = [c for c in rows if c.get("name") in policy["required_checks"]]
        if not required:
            return None
        suite = (required[0].get("check_suite") or {}).get("id")
        candidates = [
            r
            for r in workflows
            if suite is not None and r.get("check_suite_id") == suite
        ]
        if len(candidates) != 1:
            raise PublicationError("ci_check_run_binding_unverified")
        candidate = candidates[0]
        binding = CIBinding(
            repository=pr["repository"],
            pr=pr["number"],
            head=pr["head_sha"],
            base=pull["base"]["sha"],
            run=str(candidate["id"]),
            attempt=candidate["run_attempt"],
        )
        from oraculo.ci_host import github_ci

        return github_ci(self, binding, cwd, policy)

    def validate_host_policy(self, pr, policy, cwd):
        """A caller's source label/boolean cannot reduce actual host rules."""
        from oraculo.publication import discover_repository_remote

        remote = discover_repository_remote(
            cwd,
            repository=pr["repository"],
            host=pr["host"],
            remote=pr.get("remote"),
            runner=self.runner,
        )
        if pr.get("host") == "github":
            from oraculo.ci_host import github_policy

            observed = github_policy(
                self.runner,
                pr["repository"],
                pr["base"],
                pr["head_sha"],
                Path(cwd),
                policy,
            )
        elif pr.get("host") == "azure":
            from oraculo.publication import inspect_repository

            adapter = self._adapter(pr, cwd)
            m = adapter._identity(
                inspect_repository(
                    cwd,
                    remote=remote["remote"],
                    base=pr["base"],
                    runner=adapter.runner,
                )
            )
            _, current = adapter._get(m, pr["number"])
            if current["state"] == "merged":
                return {"source": "historical_merge_observed", "required_checks": None}
            policies = adapter._pages(
                m,
                "policy/evaluations",
                policy=True,
                params={
                    "artifactId": f"vstfs:///CodeReview/CodeReviewId/{m['project_id']}/{pr['number']}",
                    "includeNotApplicable": "true",
                },
            )
            checks, reviews = [], False
            for row in policies:
                config = row.get("configuration") or {}
                if config.get("isEnabled") is True and config.get("isBlocking") is True:
                    settings = config.get("settings") or {}
                    if settings.get("statusName"):
                        checks.append(
                            (
                                settings["statusGenre"] + "/"
                                if settings.get("statusGenre")
                                else ""
                            )
                            + settings["statusName"]
                        )
                    reviews = reviews or settings.get("minimumApproverCount", 0) > 0
            observed = dict(
                required_checks=checks,
                required_review=reviews,
                source="authenticated_azure_policy_evaluations",
            )
        else:
            raise PublicationError("ci_policy_host_unsupported")
        if (
            (not observed["required_checks"] and not observed.get("allow_no_checks"))
            or (policy.get("allow_no_checks") and not observed.get("allow_no_checks"))
            or not set(observed["required_checks"])
            <= set(policy.get("required_checks") or [])
            or (
                observed.get("required_review")
                and policy.get("required_review") is not True
            )
        ):
            raise PublicationError("ci_actual_host_policy_not_satisfied")
        return observed

    def merge(self, pr, policy, cwd, **kwargs):
        if pr.get("host") == "github" and policy.get("allow_no_checks") is True:
            # The facade must fail before effects even outside TaskClosure.
            self.validate_host_policy(pr, policy, cwd)
            before = kwargs.get("before_merge")

            def before_no_ci_merge():
                if before is not None:
                    before()
                self.validate_host_policy(pr, policy, cwd)

            kwargs["before_merge"] = before_no_ci_merge
        receipt = self._adapter(pr, cwd).merge(pr, policy, cwd, **kwargs)
        if pr.get("host") == "github" and receipt["state"] == "merged":
            return self.inspect(pr, policy, cwd)
        return receipt


def _policy(pr: dict, policy: dict) -> tuple[dict, str]:
    if not isinstance(pr, dict) or not isinstance(policy, dict):
        raise PublicationError("pr_merge_policy_invalid")
    policy = copy.deepcopy(policy)
    if (
        policy.get("merge_authorized") is not True
        and policy.get("observe_only") is not True
    ):
        raise PublicationError("pr_merge_not_authorized")
    repo, base, head = (policy.get(k) for k in ("repository", "base", "head_sha"))
    checks = policy.get("required_checks")
    allowed_states = policy.get("allowed_merge_states", ["CLEAN"])
    if (
        not isinstance(repo, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+", repo)
        or repo.split("/")[-1] in {".", ".."}
        or not isinstance(base, str)
        or not base
        or base.startswith("-")
        or not isinstance(head, str)
        or not _SHA.fullmatch(head)
        or policy.get("method") not in ("squash", "merge", "rebase")
        or type(policy.get("required_review")) is not bool
        or type(policy.get("allow_no_checks", False)) is not bool
        or not isinstance(checks, list)
        or any(not isinstance(c, str) or not c.strip() for c in checks)
        or len(set(checks)) != len(checks)
        or not isinstance(allowed_states, list)
        or not allowed_states
        or any(
            s
            not in (
                ("CLEAN", "HAS_HOOKS", "UNSTABLE")
                if checks == [] and policy.get("allow_no_checks") is True
                else ("CLEAN", "HAS_HOOKS")
            )
            for s in allowed_states
        )
    ):
        raise PublicationError("pr_merge_policy_invalid")
    if not checks and (
        not policy.get("allow_no_checks", False)
        or not isinstance(policy.get("policy_source"), str)
        or not policy["policy_source"].strip()
    ):
        raise PublicationError("pr_required_checks_unspecified")
    if (
        pr.get("host") != "github"
        or not isinstance(pr.get("repository"), str)
        or pr["repository"].lower() != repo.lower()
        or pr.get("base") != base
        or pr.get("head_sha") != head
        or type(pr.get("number")) is not int
        or pr["number"] < 1
        or not isinstance(pr.get("url"), str)
        or pr["url"].lower() != f"https://github.com/{repo}/pull/{pr['number']}".lower()
    ):
        raise PublicationError("pr_policy_binding_mismatch")
    try:
        digest = hashlib.sha256(
            json.dumps(policy, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    except (ValueError, TypeError):
        raise PublicationError("pr_merge_policy_invalid") from None
    return policy, digest


def _checks(
    rows: object, required: list[str], *, ignore_optional=False
) -> tuple[list[dict], list[str]]:
    # gh represents the absence of status rollup as either null or an empty list.
    if rows is None:
        rows = []
    if not isinstance(rows, list):
        raise PublicationError("pr_host_response_invalid")
    checks, blockers, names = [], [], set()
    for row in rows:
        if not isinstance(row, dict):
            raise PublicationError("pr_host_response_invalid")
        kind = row.get("__typename")
        if kind == "CheckRun":
            name, status, conclusion = (
                row.get(k) for k in ("name", "status", "conclusion")
            )
            if status not in (
                "QUEUED",
                "IN_PROGRESS",
                "COMPLETED",
                "WAITING",
                "REQUESTED",
                "PENDING",
            ):
                raise PublicationError("pr_host_response_invalid")
            complete = status == "COMPLETED"
            result = conclusion if complete else "PENDING"
        elif kind == "StatusContext":
            name, result = row.get("context"), row.get("state")
            if result not in ("EXPECTED", "ERROR", "FAILURE", "PENDING", "SUCCESS"):
                raise PublicationError("pr_host_response_invalid")
            complete = result not in {"EXPECTED", "PENDING"}
        else:
            raise PublicationError("pr_host_response_invalid")
        if not isinstance(name, str) or not name or not isinstance(result, str):
            raise PublicationError("pr_host_response_invalid")
        if name in names:
            blockers.append("pr_check_context_ambiguous")
        names.add(name)
        checks.append({"name": name, "completed": complete, "conclusion": result})
        if ignore_optional and name not in required:
            continue
        if not complete:
            blockers.append("pr_checks_pending")
        elif result not in {"SUCCESS", "NEUTRAL", "SKIPPED"}:
            blockers.append("pr_checks_failed")
        elif name in required and result != "SUCCESS":
            blockers.append("pr_required_check_not_successful")
    if set(required) - names:
        blockers.append("pr_required_checks_missing")
    return checks, blockers


class GitHubPRLifecycle:
    def __init__(self, runner: Runner = command_runner):
        self.runner = runner

    def inspect(self, pr: dict, policy: dict, cwd: str | Path) -> dict:
        """Return readiness/blockers; invalid identity/schema raises a safe error.

        Pending checks/review/conflicts are ordinary observations with ready=False.
        A merged PR is reconcilable even if historical checks are no longer exposed.
        """
        policy, digest = _policy(pr, policy)
        try:
            raw = self.runner(
                [
                    "gh",
                    "pr",
                    "view",
                    str(pr["number"]),
                    "--repo",
                    "github.com/" + policy["repository"],
                    "--json",
                    _FIELDS,
                ],
                Path(cwd).resolve(),
            )
        except (OSError, subprocess.SubprocessError):
            raise PublicationError("pr_host_unavailable") from None
        try:
            observed = json.loads(raw)
        except (ValueError, TypeError):
            raise PublicationError("pr_host_response_invalid") from None
        if not isinstance(observed, dict):
            raise PublicationError("pr_host_response_invalid")
        if (
            type(observed.get("number")) is not int
            or observed["number"] != pr["number"]
            or not isinstance(observed.get("url"), str)
            or observed["url"].lower() != pr["url"].lower()
            or observed.get("baseRefName") != policy["base"]
        ):
            raise PublicationError("pr_remote_identity_mismatch")
        if observed.get("headRefOid") != policy["head_sha"]:
            raise PublicationError("pr_head_changed")
        state = observed.get("state")
        if state not in ("OPEN", "CLOSED", "MERGED"):
            raise PublicationError("pr_host_response_invalid")
        receipt = {
            "host": "github",
            "repository": policy["repository"],
            "number": pr["number"],
            "url": observed["url"],
            "head_sha": observed["headRefOid"],
            "base": observed["baseRefName"],
            "state": state.lower(),
            "merge_sha": None,
            "ready": False,
            "blockers": [],
            "checks": [],
            "review_decision": observed.get("reviewDecision"),
            "policy_sha256": digest,
        }
        if state == "MERGED":
            merge = observed.get("mergeCommit")
            if (
                not isinstance(merge, dict)
                or not isinstance(merge.get("oid"), str)
                or not _SHA.fullmatch(merge["oid"])
            ):
                raise PublicationError("pr_merge_commit_unverified")
            receipt["merge_sha"] = merge["oid"]
            return receipt
        if state == "CLOSED":
            receipt["blockers"] = ["pr_closed_without_merge"]
            return receipt
        if (
            type(observed.get("isDraft")) is not bool
            or "statusCheckRollup" not in observed
            or "reviewDecision" not in observed
            or observed.get("mergeable") not in ("MERGEABLE", "CONFLICTING", "UNKNOWN")
            or observed.get("mergeStateStatus")
            not in (
                "BEHIND",
                "BLOCKED",
                "CLEAN",
                "DIRTY",
                "DRAFT",
                "HAS_HOOKS",
                "UNKNOWN",
                "UNSTABLE",
            )
            or observed.get("reviewDecision")
            not in (None, "", "APPROVED", "CHANGES_REQUESTED", "REVIEW_REQUIRED")
        ):
            raise PublicationError("pr_host_response_invalid")
        checks, blockers = _checks(
            observed["statusCheckRollup"],
            policy["required_checks"],
            ignore_optional=policy.get("allow_no_checks") is True,
        )
        if observed["isDraft"]:
            blockers.append("pr_is_draft")
        if observed["mergeable"] == "CONFLICTING":
            blockers.append("pr_merge_conflicts")
        elif observed["mergeable"] != "MERGEABLE":
            blockers.append("pr_mergeability_pending")
        if observed["mergeStateStatus"] not in policy.get(
            "allowed_merge_states",
            ["CLEAN", "UNSTABLE"]
            if policy["required_checks"] == [] and policy.get("allow_no_checks") is True
            else ["CLEAN"],
        ):
            blockers.append("pr_merge_state_blocked")
        if observed["reviewDecision"] == "CHANGES_REQUESTED":
            blockers.append("pr_changes_requested")
        elif policy["required_review"] and observed["reviewDecision"] != "APPROVED":
            blockers.append("pr_review_pending")
        receipt.update(
            checks=checks, blockers=list(dict.fromkeys(blockers)), ready=not blockers
        )
        return receipt

    def merge(
        self,
        pr: dict,
        policy: dict,
        cwd: str | Path,
        *,
        before_merge: Callable[[], None] | None = None,
        reconcile_only: bool = False,
    ) -> dict:
        """Merge once or reconcile; only a verified MERGED receipt is success.

        The owner must preserve any previous intent even if a later preflight
        error has emitted=False. Server rejection/queueing/timeouts after the
        command remain uncertain and must never cause an automatic second send.
        """
        policy = copy.deepcopy(policy)
        pr = copy.deepcopy(pr)
        if policy.get("merge_authorized") is not True:
            raise PublicationError("pr_merge_not_authorized")
        if type(reconcile_only) is not bool or (
            before_merge is not None and not callable(before_merge)
        ):
            raise PublicationError("pr_merge_policy_invalid")
        observed = self.inspect(pr, policy, cwd)
        if observed["state"] == "merged":
            return observed
        if reconcile_only:
            raise PublicationError("pr_merge_uncertain")
        if not observed["ready"]:
            raise PublicationError(observed["blockers"][0])
        if before_merge is not None:
            try:
                before_merge()
            except Exception:
                raise PublicationError("pr_merge_intent_failed") from None
        try:
            self.runner(
                [
                    "gh",
                    "pr",
                    "merge",
                    str(pr["number"]),
                    "--repo",
                    "github.com/" + policy["repository"],
                    "--" + policy["method"],
                    "--match-head-commit",
                    policy["head_sha"],
                ],
                Path(cwd).resolve(),
            )
        except (OSError, subprocess.SubprocessError):
            # A nonzero/timeout response can follow an accepted merge or queue
            # request. Verify once; the host owns subsequent notifications.
            pass
        try:
            observed = self.inspect(pr, policy, cwd)
        except PublicationError as exc:
            raise PublicationError(exc.code, emitted=True) from None
        if observed["state"] != "merged":
            raise PublicationError("pr_merge_uncertain", emitted=True)
        return observed
