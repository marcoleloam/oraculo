"""Observe and merge a GitHub PR against an explicitly bound delivery policy.

The caller persists an immutable policy and an intent in ``before_merge``, then
uses ``reconcile_only`` after uncertain emission. There is no watcher or polling
loop here: call again on the host's PR notification. A merge queue acknowledgement
is not a verified merge. Only a fresh MERGED observation with the bound head/base
and a merge commit supplies a successful receipt.

Branch protection/rules remain enforced by GitHub; this adapter never uses admin
bypass, auto-merge, or local pushes. It does not discover repository rules. The
caller must derive required_checks and required_review from the remote policy,
and must block if that discovery is unavailable rather than inventing an empty
policy. An intentional repository without CI requires allow_no_checks=True.
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
        return self._adapter(pr, cwd).inspect(pr, policy, cwd)

    def merge(self, pr, policy, cwd, **kwargs):
        return self._adapter(pr, cwd).merge(pr, policy, cwd, **kwargs)


def _policy(pr: dict, policy: dict) -> tuple[dict, str]:
    if not isinstance(pr, dict) or not isinstance(policy, dict):
        raise PublicationError("pr_merge_policy_invalid")
    policy = copy.deepcopy(policy)
    if policy.get("merge_authorized") is not True:
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
        or any(s not in ("CLEAN", "HAS_HOOKS") for s in allowed_states)
    ):
        raise PublicationError("pr_merge_policy_invalid")
    if not checks and not policy.get("allow_no_checks", False):
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


def _checks(rows: object, required: list[str]) -> tuple[list[dict], list[str]]:
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
            observed["statusCheckRollup"], policy["required_checks"]
        )
        if observed["isDraft"]:
            blockers.append("pr_is_draft")
        if observed["mergeable"] == "CONFLICTING":
            blockers.append("pr_merge_conflicts")
        elif observed["mergeable"] != "MERGEABLE":
            blockers.append("pr_mergeability_pending")
        if observed["mergeStateStatus"] not in policy.get(
            "allowed_merge_states", ["CLEAN"]
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
