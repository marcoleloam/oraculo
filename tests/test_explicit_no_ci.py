"""Explicit no-CI policy through real facade/service/CLI/canonical memory."""

import copy
import json

import pytest
from test_github_policy_compatibility import ManagedPolicyHost, PolicyHost
from test_report_prevalidation import flow as flow_fixture

from oraculo.pr_lifecycle import RepositoryPRLifecycle

flow = flow_fixture


def no_ci_response(argv, raw):
    if argv[:2] == ["gh", "api"] and argv[2].endswith("/protection"):
        value = json.loads(raw)
        value["required_status_checks"] = None
        return json.dumps(value)
    return raw


class NoCIPolicyHost(PolicyHost):
    def __call__(self, argv, cwd):
        raw = super().__call__(argv, cwd)
        return no_ci_response(argv, raw) if self.policy_checks is None else raw


def available_policy(host):
    host.__class__ = NoCIPolicyHost
    host.capability = "available"
    host.protected = True


class NoCIManagedHost(ManagedPolicyHost):
    def __init__(self, head):
        super().__init__(head)
        self.capability = "available"
        self.protected = True

    def __call__(self, argv, cwd):
        raw = super().__call__(argv, cwd)
        return no_ci_response(argv, raw) if self.policy_checks is None else raw


@pytest.mark.parametrize("entrypoint", ["api", "cli"])
@pytest.mark.parametrize("historical", [False, True])
@pytest.mark.parametrize(
    "case",
    [
        "clean",
        "unstable",
        "transition",
        "retry",
        "after_write",
        "settlement_change",
        "work_failed",
        "functional_child_failed",
    ],
)
async def test_no_ci_journey(flow, monkeypatch, tmp_path, entrypoint, historical, case):
    from test_ci_round_memory import setup as managed_setup
    from test_stdio_fifo import run_cli_fifo

    from oraculo.task_transport import SocketMemory

    task, _, (_, policy, original) = await managed_setup(flow, monkeypatch)
    host = NoCIManagedHost(original.head)
    host.required = policy["required_checks"].copy()
    host.merged = historical
    policy.update(method="merge", required_checks=[], allow_no_checks=True)
    host.conclusion = "cancelled"

    # A read-only observation uses the existing immutable policy; no new merge intent.
    def transport(argv, cwd):
        raw = host(argv, cwd)
        if case == "unstable" and argv[:3] == ["gh", "pr", "view"]:
            value = json.loads(raw)
            value["mergeStateStatus"] = "UNSTABLE"
            value["statusCheckRollup"] = [
                dict(
                    __typename="CheckRun",
                    name="optional cancelled CI",
                    status="COMPLETED",
                    conclusion="CANCELLED",
                )
            ]
            return json.dumps(value)
        return raw

    lifecycle = RepositoryPRLifecycle(transport)
    monkeypatch.setattr("oraculo.pr_lifecycle.RepositoryPRLifecycle", lambda: lifecycle)
    store = flow.service.managed_service.store
    before = copy.deepcopy(store.get(task["id"]))
    original_call = flow.host.call

    async def call(name, args):
        if name == "t3_thread_organize":
            flow.host.threads[args["threadId"]]["settled"] = True
            return dict(accepted=True)
        result = await original_call(name, args)
        if name == "list_thread_pull_requests" and host.merged:
            result["pullRequests"] = [
                dict(
                    host="github.com",
                    repository=policy["repository"],
                    number=12,
                    state="merged",
                    isDraft=False,
                    baseBranch="main",
                )
            ]
        return result

    flow.host.call = call

    async def check(policy_payload=None):
        payload = dict(
            task_id=task["id"], **({"policy": policy_payload} if policy_payload else {})
        )
        if entrypoint == "api":
            return await flow.invoke("pr-check", **payload)
        code, result, _, stderr = await run_cli_fifo(
            flow, tmp_path, "pr-check", payload, pad_hex=14000, minimum=8192
        )
        assert code == 0, (result, stderr)
        return result

    # Preserve a real adapter failure, then recover the same task with fresh reads.
    host.protection_error = 403, "Forbidden"
    if entrypoint == "api":
        blocked = await flow.invoke("pr-check", task_id=task["id"], policy=policy)
    else:
        code, blocked, _, _ = await run_cli_fifo(
            flow,
            tmp_path,
            "pr-check",
            dict(task_id=task["id"], policy=policy),
            pad_hex=14000,
            minimum=8192,
        )
        assert code == 1
    assert blocked["blocked_reason"] == "ci_host_policy_access_denied"
    host.protection_error = None
    historical_rounds = []
    if case == "transition":
        original_policy = dict(
            policy, required_checks=host.required.copy(), allow_no_checks=False
        )
        await flow.invoke("pr-check", task_id=task["id"], policy=original_policy)
        historical_rounds = copy.deepcopy(store.get(task["id"])["ci_rounds"])
        assert historical_rounds[-1]["work_status"] == "failed"
        await flow.invoke("pr-check", task_id=task["id"], policy=policy)
    if case == "retry":
        factory = flow.service.managed_service.memory
        failed = []

        def memory():
            adapter = factory()
            finish = adapter.finish

            async def uncertain(*args, **kwargs):
                result = await finish(*args, **kwargs)
                if not failed and "not_required_by_explicit_policy" in args[-1].get(
                    "summary", ""
                ):
                    failed.append(True)
                    raise RuntimeError("simulated lost canonical write response")
                return result

            adapter.finish = uncertain
            return adapter

        flow.service.managed_service.memory = memory
        blocked = await flow.invoke("pr-check", task_id=task["id"])
        assert blocked["blocked_reason"] == "ci_round_memory_write_unconfirmed"
        pending_id = store.get(task["id"])["ci_rounds"][-1]["id"]
    if case not in {"clean", "unstable", "transition", "retry"}:
        if case == "work_failed":
            current = store.get(task["id"])
            report = copy.deepcopy(current["report"])
            report["checks"] = [dict(command="functional test", status="failed")]
            store.update(
                task["id"],
                current["phase"],
                {"fixture": "negative original validation"},
                report=report,
            )
        if case == "functional_child_failed":
            flow.host.threads["failed-functional"] = dict(
                flow.host.threads[task["child"]["childThreadId"]],
                threadId="failed-functional",
                projectId="alpha",
                parentThreadId=task["child"]["childThreadId"],
                status="failed",
                activeRunId=None,
                latestRunId="failed-functional-run",
                pendingRequestCount=0,
                title="Mandatory functional validation",
            )
        if case in {"after_write", "settlement_change"}:
            factory = flow.service.managed_service.memory

            def memory():
                adapter = factory()
                finish = adapter.finish

                async def after_memory(*args, **kwargs):
                    result = await finish(*args, **kwargs)
                    summary = args[-1].get("summary", "")
                    if (
                        case == "after_write"
                        and "not_required_by_explicit_policy" in summary
                    ) or (
                        case == "settlement_change"
                        and summary.startswith("Task merged;")
                    ):
                        host.rules = [
                            dict(
                                type="pull_request",
                                parameters=dict(required_approving_review_count=1),
                            )
                        ]
                        host.capability = "available"
                        host.protection_error = (404, "Branch not protected")
                    return result

                adapter.finish = after_memory
                return adapter

            flow.service.managed_service.memory = memory
        action = (
            "pr-check"
            if historical or case in {"after_write", "work_failed"}
            else "merge"
        )
        if entrypoint == "api":
            result = await flow.invoke(action, task_id=task["id"])
        else:
            code, result, _, _ = await run_cli_fifo(
                flow,
                tmp_path,
                action,
                dict(task_id=task["id"]),
                pad_hex=14000,
                minimum=8192,
            )
            assert code == 1, result
        assert result["blocked_reason"], result
        assert result["phase"] != "settled"
        if case != "settlement_change":
            assert not any(c[:3] == ["gh", "pr", "merge"] for c in host.calls)
        return
    result = await check()
    assert result["merged"] is historical, result
    assert result["phase"] == ("settled" if historical else "pr_ready"), result
    current = store.get(task["id"])
    round1 = current["ci_rounds"][-1]
    if historical_rounds:
        assert current["ci_rounds"][:-1] == historical_rounds
    if case == "retry":
        assert round1["id"] == pending_id
    assert round1["memory"]["write_receipt"]["state"] == "confirmed"
    assert round1["facts"]["outcome"] == "not_required_by_explicit_policy"
    assert round1["report"]["evidence"]["execution"] is None
    assert len(round1["facts"]["check_bindings"]) == len(policy["required_checks"])
    assert round1["facts"]["identity"].get("observation_timing") == (
        "after_external_merge" if historical else None
    )
    assert current.get("merge_attempted") is not True
    assert (
        current["pr_observation"]["receipt"]["observed_host_policy"][
            "remote_capability"
        ]["state"]
        == "enforced"
    )
    for key in ("request", "report", "submitted_report", "memory", "child", "base_sha"):
        assert current.get(key) == before.get(key)
    assert any(
        e["proof"].get("reason") == "ci_host_policy_access_denied"
        for e in store.describe(task["id"])["events"]
    )
    replay = await check()
    assert replay["ci_rounds"] == result["ci_rounds"]
    recalled = await SocketMemory(flow.service.config).begin(
        "alpha",
        dict(thread_id="fresh-reader", run_id="fresh-run"),
        "recover-policy-round",
        "CI failure diagnosis correction evaluation history",
    )
    assert round1["id"] in json.dumps(recalled["context"])
    assert all(c[:3] != ["gh", "pr", "merge"] for c in host.calls)

    if historical:
        # A later external merge advances main, not the original PR's immutable
        # head/merge or the check executions already in canonical memory.
        host.base = "e" * 40
        host.current_base_head = host.base
        advanced = await check()
        assert advanced["phase"] == "settled", advanced
        assert advanced["merge_receipt"]["merge_sha"] == host.merge_sha
        assert advanced["ci_rounds"] == result["ci_rounds"]
        assert all(c[:3] != ["gh", "pr", "merge"] for c in host.calls)
    if not historical:

        def require_canonical_memory():
            saved = store.get(task["id"])["ci_rounds"]
            assert saved and all(
                r["memory"]["write_receipt"]["state"] == "confirmed" for r in saved
            )

        host.on_merge = require_canonical_memory
        if entrypoint == "api":
            merged = await flow.invoke("merge", task_id=task["id"])
        else:
            code, merged, _, stderr = await run_cli_fifo(
                flow,
                tmp_path,
                "merge",
                dict(task_id=task["id"]),
                pad_hex=14000,
                minimum=8192,
            )
            assert code == 0, (merged, stderr)
        assert merged["phase"] == "settled", merged
        assert len([c for c in host.calls if c[:3] == ["gh", "pr", "merge"]]) == 1
        assert merged["ci_rounds"] == result["ci_rounds"]
        intent = next(
            e["proof"] for e in merged["events"] if e["phase"] == "merge_pending"
        )
        assert intent["authenticated_actor"]["login"] == host.actor
        assert intent["repository"] == policy["repository"]
        assert intent["base"] == policy["base"]
        assert intent["head_sha"] == policy["head_sha"]


@pytest.mark.parametrize(
    "case",
    [
        "valid",
        "no_optin",
        "no_reason",
        "blank_reason",
        "unknown",
        "remote_check",
        "remote_classic_check",
        "workflow",
        "review",
        "codeowner",
        "lastpush",
        "repo",
        "head",
        "base",
    ],
)
def test_no_ci_remote_floor(tmp_path, case):
    from test_github_policy_compatibility import setup

    from oraculo.publication import PublicationError

    host, lifecycle, pr, policy = setup(tmp_path)
    available_policy(host)
    policy.update(required_checks=[], allow_no_checks=True)
    if case == "no_optin":
        policy.pop("allow_no_checks")
    if case == "no_reason":
        policy.pop("policy_source")
    if case == "blank_reason":
        policy["policy_source"] = "  "
    if case == "remote_classic_check":
        host.policy_checks = ["new mandatory CI"]
    if case == "unknown":
        host.rules_error = (403, "Forbidden")
    if case in {"remote_check", "workflow", "review", "codeowner", "lastpush"}:
        host.capability = "available"
        host.protection_error = (404, "Branch not protected")
        host.rules = (
            [
                dict(
                    type="required_status_checks",
                    parameters=dict(required_status_checks=[dict(context="mandatory")]),
                )
            ]
            if case == "remote_check"
            else [dict(type="workflows", parameters={})]
            if case == "workflow"
            else [
                dict(
                    type="pull_request",
                    parameters=dict(
                        required_approving_review_count=1,
                        **(
                            {"require_code_owner_review": True}
                            if case == "codeowner"
                            else {"require_last_push_approval": True}
                            if case == "lastpush"
                            else {}
                        ),
                    ),
                )
            ]
        )
    if case in {"repo", "head", "base"}:
        policy[{"repo": "repository", "head": "head_sha", "base": "base"}[case]] = (
            "incorrect"
        )
    if case == "valid":
        observed = lifecycle.validate_host_policy(pr, policy, tmp_path)
        assert observed["allow_no_checks"] is True
        assert observed["required_checks"] == []
    else:
        with pytest.raises(PublicationError):
            lifecycle.validate_host_policy(pr, policy, tmp_path)


@pytest.mark.parametrize("case", ["checks", "review", "codeowner", "lastpush"])
def test_no_ci_policy_change_after_memory(tmp_path, case):
    from test_github_policy_compatibility import setup

    from oraculo.ci_round_memory import CIRoundMemory
    from oraculo.state import StateError

    host, lifecycle, pr, policy = setup(tmp_path)
    available_policy(host)
    policy.update(required_checks=[], allow_no_checks=True)
    first = lifecycle.validate_host_policy(pr, policy, tmp_path)
    changed = copy.deepcopy(first)
    changed[
        {
            "checks": "remote_required_checks",
            "review": "review_count",
            "codeowner": "specialized_review",
            "lastpush": "specialized_review",
        }[case]
    ] = ["new"] if case == "checks" else 1 if case == "review" else True
    with pytest.raises(StateError, match="changed_after_memory"):
        CIRoundMemory.require_same_policy(first, changed)


@pytest.mark.parametrize(
    "case", ["repository", "head", "base", "ancestry", "advanced_base_unrelated"]
)
def test_no_ci_historical_identity(tmp_path, case):
    from test_github_policy_compatibility import setup

    from oraculo.publication import PublicationError

    host, lifecycle, pr, policy = setup(tmp_path)
    available_policy(host)
    policy.update(required_checks=[], allow_no_checks=True)
    host.merged = True

    def transport(argv, cwd):
        raw = host(argv, cwd)
        if argv[:2] == ["gh", "api"]:
            value = json.loads(raw)
            if argv[2].endswith("/pulls/1"):
                if case == "repository":
                    value["base"]["repo"]["full_name"] = "other/repo"
                if case == "head":
                    value["head"]["sha"] = "f" * 40
                if case == "base":
                    value["base"]["ref"] = "other"
            if "/compare/" in argv[2] and (
                case == "ancestry"
                or (
                    case == "advanced_base_unrelated"
                    and host.merge_sha + "..." in argv[2]
                )
            ):
                value["merge_base_commit"]["sha"] = "f" * 40
            return json.dumps(value)
        return raw

    lifecycle.runner = transport
    with pytest.raises(PublicationError):
        lifecycle.inspect(pr, policy, tmp_path)


@pytest.mark.parametrize(
    "rollup",
    [
        None,
        [],
        [
            dict(
                __typename="CheckRun",
                name="old optional CI",
                status="COMPLETED",
                conclusion="CANCELLED",
            )
        ],
    ],
)
@pytest.mark.parametrize("historical", [False, True])
def test_no_ci_absent_or_cancelled_rollup(tmp_path, rollup, historical):
    from test_github_policy_compatibility import setup

    host, lifecycle, pr, policy = setup(tmp_path)
    available_policy(host)
    policy.update(required_checks=[], allow_no_checks=True)
    host.merged = historical

    def transport(argv, cwd):
        raw = host(argv, cwd)
        if argv[:3] == ["gh", "pr", "view"]:
            value = json.loads(raw)
            value["statusCheckRollup"] = rollup
            if rollup and not historical:
                value["mergeStateStatus"] = "UNSTABLE"
            return json.dumps(value)
        return raw

    lifecycle.runner = transport
    assert (
        lifecycle.validate_host_policy(pr, policy, tmp_path)["allow_no_checks"] is True
    )
    observed = lifecycle.inspect(pr, policy, tmp_path)
    assert observed["blockers"] == []
    assert observed["state"] == ("merged" if historical else "open")


@pytest.mark.parametrize("state", ["BLOCKED", "UNKNOWN", "DIRTY"])
def test_no_ci_does_not_waive_other_merge_states(tmp_path, state):
    from test_github_policy_compatibility import setup

    host, lifecycle, pr, policy = setup(tmp_path)
    available_policy(host)
    policy.update(required_checks=[], allow_no_checks=True)

    def transport(argv, cwd):
        raw = host(argv, cwd)
        if argv[:3] == ["gh", "pr", "view"]:
            value = json.loads(raw)
            value["mergeStateStatus"] = state
            return json.dumps(value)
        return raw

    lifecycle.runner = transport
    assert lifecycle.validate_host_policy(pr, policy, tmp_path)["allow_no_checks"]
    assert (
        "pr_merge_state_blocked" in lifecycle.inspect(pr, policy, tmp_path)["blockers"]
    )


@pytest.mark.parametrize("endpoint", ["protection", "rules"])
def test_no_ci_inaccessible_policy_stays_blocked(tmp_path, endpoint):
    from test_github_policy_compatibility import LIMIT, setup

    from oraculo.publication import PublicationError

    host, lifecycle, pr, policy = setup(tmp_path)
    available_policy(host)
    policy.update(required_checks=[], allow_no_checks=True)
    host.capability = "available"
    host.protected = False
    if endpoint == "protection":
        host.protection_error = 403, LIMIT
    else:
        host.rules_error = 403, LIMIT

    # Remove real check floors so that this specifically tests unreadable policy.
    def transport(argv, cwd):
        raw = host(argv, cwd)
        if argv[:2] == ["gh", "api"] and argv[2].endswith("/protection"):
            value = json.loads(raw)
            value["required_status_checks"] = None
            return json.dumps(value)
        return raw

    lifecycle.runner = transport
    with pytest.raises(
        PublicationError, match="ci_no_checks_policy_discovery_required"
    ):
        lifecycle.validate_host_policy(pr, policy, tmp_path)


@pytest.mark.parametrize("case", ["unknown", "remote_checks", "changed_before_emit"])
def test_no_ci_facade_merge_checks_fresh_policy_before_emission(tmp_path, case):
    from test_github_policy_compatibility import setup

    from oraculo.publication import PublicationError

    host, lifecycle, pr, policy = setup(tmp_path)
    available_policy(host)
    policy.update(required_checks=[], allow_no_checks=True)
    intents = []

    def change():
        intents.append(True)
        if case == "changed_before_emit":
            host.policy_checks = ["new mandatory CI"]

    if case == "unknown":
        host.rules_error = 403, "Forbidden"
    if case == "remote_checks":
        host.policy_checks = ["new mandatory CI"]
    with pytest.raises(PublicationError):
        lifecycle.merge(pr, policy, tmp_path, before_merge=change)
    assert not any(c[:3] == ["gh", "pr", "merge"] for c in host.calls)
