"""Azure CI observation for source-SHA builds with blocking status policies."""

from oraculo.ci_events import READINESS_BLOCKERS
from oraculo.ci_host import _stamp
from oraculo.protocol import content_hash
from oraculo.publication import discover_repository_remote, inspect_repository
from oraculo.state import StateError


def _head_branch(ref):
    prefix = "refs/heads/"
    if (
        not isinstance(ref, str)
        or not ref.startswith(prefix)
        or not ref[len(prefix) :]
        or ref[len(prefix) :].startswith("-")
    ):
        raise StateError("ci_host_binding_mismatch")
    return ref[len(prefix) :]


def azure_ci(lifecycle, binding, cwd):
    # Identity comes from the actual remote plus authenticated repository lookup.
    # The PR's target branch is the base. The local checkout is often the
    # default branch, not the PR head, so it does not bind this read.
    remote = discover_repository_remote(
        cwd, repository=binding.repository, host="azure", runner=lifecycle.runner
    )
    probed = inspect_repository(
        cwd, remote=remote["remote"], runner=lifecycle.runner
    )
    if probed.get("repository") != binding.repository or probed.get("host") != "azure":
        raise StateError("ci_repository_binding_mismatch")
    spec = {"cwd": str(cwd)}
    from oraculo.azure_repos import AzureRepos

    adapter = (
        lifecycle.azure_factory(spec)
        if lifecycle.azure_factory
        else AzureRepos.from_spec(spec, lifecycle.runner)
    )
    identity = adapter._identity(probed)
    raw, _ = adapter._request(
        identity,
        adapter._repo_path(identity) + "/pullrequests/" + str(binding.pr),
    )
    if not isinstance(raw, dict):
        raise StateError("ci_host_binding_mismatch")
    base = _head_branch(raw.get("targetRefName"))
    branch = _head_branch(raw.get("sourceRefName"))
    head = (raw.get("lastMergeSourceCommit") or {}).get("commitId")
    if not isinstance(head, str):
        raise StateError("ci_host_binding_mismatch")
    located = inspect_repository(
        cwd, remote=remote["remote"], base=base, runner=lifecycle.runner
    )
    if (
        located.get("repository") != binding.repository
        or located.get("host") != "azure"
        or located.get("remote") != identity.get("remote")
    ):
        raise StateError("ci_repository_binding_mismatch")
    # Keep the repository ids from the first lookup. A second lookup by id
    # is a different URL and is not required once the target ref is known.
    m = {
        **identity,
        "base": base,
        "branch": branch,
        "head_sha": head,
    }
    row, pr = adapter._get(m, binding.pr)
    if pr["head_sha"] != binding.head or pr["base_sha"] != binding.base:
        raise StateError("ci_host_binding_mismatch")
    if not binding.run.isdecimal():
        raise StateError("ci_run_invalid")
    build, _ = adapter._request(m, "build/builds/" + binding.run)
    timeline, _ = adapter._request(m, "build/builds/" + binding.run + "/timeline")
    records = timeline.get("records")
    if (
        str(build.get("id")) != binding.run
        or build.get("sourceVersion") != binding.head
        or (build.get("repository") or {}).get("id") != m["repository_id"]
        or str((build.get("triggerInfo") or {}).get("pr.number")) != str(binding.pr)
        or not isinstance(records, list)
        or not records
        or any(type(r.get("attempt")) is not int for r in records)
        or max(r["attempt"] for r in records) != binding.attempt
    ):
        raise StateError("ci_build_attempt_binding_unavailable")
    policies = adapter._pages(
        m,
        "policy/evaluations",
        policy=True,
        params={
            "artifactId": f"vstfs:///CodeReview/CodeReviewId/{m['project_id']}/{binding.pr}",
            "includeNotApplicable": "true",
        },
    )
    checks, review, bound_build = [], False, False
    for evaluation in policies:
        config = evaluation.get("configuration") or {}
        if config.get("isEnabled") is not True or config.get("isBlocking") is not True:
            continue
        settings = config.get("settings") or {}
        if (
            settings.get("buildDefinitionId")
            == (build.get("definition") or {}).get("id")
            and settings.get("buildDefinitionId") is not None
        ):
            bound_build = (
                str((evaluation.get("context") or {}).get("buildId")) == binding.run
            )
        if settings.get("statusName"):
            checks.append(
                (settings["statusGenre"] + "/" if settings.get("statusGenre") else "")
                + settings["statusName"]
            )
        review = review or settings.get("minimumApproverCount", 0) > 0
    if not checks or not bound_build:
        raise StateError("ci_azure_blocking_status_and_build_binding_required")
    policy = dict(
        repository=pr["repository"],
        base=pr["base"],
        head_sha=binding.head,
        remote=pr["remote"],
        method="merge",
        observe_only=True,
        merge_authorized=False,
        required_checks=sorted(set(checks)),
        required_review=review,
        allow_no_checks=False,
        policy_source="authenticated_azure_policy_evaluations",
    )
    receipt = adapter.inspect(pr, policy, cwd, bind_local_checkout=False)
    blockers = set(receipt.get("blockers") or [])
    ci_blocked = bool(blockers - READINESS_BLOCKERS)
    conclusion = "pending"
    if build.get("status") == "completed":
        if build.get("result") == "canceled":
            conclusion = "cancelled"
        else:
            conclusion = (
                "passed"
                if build.get("result") == "succeeded" and not ci_blocked
                else "failed"
            )
    stamp = _stamp(
        build.get("finishTime") or build.get("startTime") or build.get("queueTime")
    )
    evidence = dict(
        source="authenticated_azure_rest",
        policy=policy,
        lifecycle=receipt,
        build_hash=content_hash(build),
        timeline_hash=content_hash(timeline),
    )
    event = binding.model_dump(mode="json") | dict(
        event_id=content_hash([binding.model_dump(), evidence, conclusion]),
        sequence=max(1, int(stamp * 1000000)),
        occurred_at=stamp,
        conclusion=conclusion,
        diagnostic="",
    )
    return dict(event=event, evidence=evidence)
