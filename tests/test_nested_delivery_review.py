"""A recursive host snapshot contains several legitimate delivery scopes."""

import copy
from pathlib import Path

import pytest
from test_managed_tasks import git, report
from test_review_product_acceptance import (
    execute,
    implementation,
    observe_agents,
    reviewer,
)
from test_review_product_acceptance import flow as flow_fixture

from oraculo.delivery_review import target

flow = flow_fixture
SCRIPT = "from nested import nested_answer; assert nested_answer() == 42; print('nested behavior passed')"


async def nested_tree(flow, role, *, bad_functional=False, complete_root=True):
    observe_agents(flow)
    root, _, _ = await implementation(flow, "root")
    flow.host.caller = root["child"]["childThreadId"]
    nested = await flow.invoke(
        "start",
        kind="work",
        key="nested",
        text="Implement a separately delivered nested component and validate its exact scope",
        cwd=root["child_workspace"]["path"],
    )
    assert nested["phase"] == "executing", nested
    cwd = Path(nested["child_workspace"]["path"])
    (cwd / "nested.py").write_text(
        f"def nested_answer():\n    return {41 if bad_functional else 42}\n"
    )
    git(cwd, "add", "nested.py")
    git(
        cwd,
        "-c",
        "user.name=Example",
        "-c",
        "user.email=example@example.org",
        "commit",
        "-m",
        "Nested component",
    )
    data = report(nested)
    data["checks"] = [
        execute(
            cwd,
            cwd.parent / "nested-implementation.log",
            script="from nested import nested_answer; assert callable(nested_answer); print('nested smoke passed')"
            if bad_functional
            else SCRIPT,
        )
    ]
    flow.host.caller = nested["child"]["childThreadId"]
    submitted = await flow.invoke("finish", task_id=nested["id"], report=data)
    assert submitted["phase"] == "validation_submitted", submitted
    review = await reviewer(
        flow,
        nested,
        outcome="no_verdict" if role == "quality_review" else "passed",
        role=role,
        script=SCRIPT,
    )
    flow.host.caller = root["child"]["childThreadId"]
    delivered = await flow.invoke("resume", task_id=nested["id"])
    if bad_functional:
        assert (
            delivered["blocked_reason"] == "mandatory_functional_validation_failed"
        ), delivered
    else:
        assert delivered["phase"] == "pr_pending", delivered
    if complete_root:
        flow.host.complete(root["child"]["childThreadId"])
        flow.host.caller = "origin"
    return root, nested, review, data


@pytest.mark.parametrize("role", ["quality_review", "functional_tests"])
async def test_nested_review_uses_its_implementation_owner(flow, role):
    root, nested, review, data = await nested_tree(flow, role)
    store = flow.service.managed_service.store
    original_nested, original_review = store.get(nested["id"]), store.get(review["id"])
    original = flow.host.call

    async def guarded(name, args):
        if name == "task_status":
            assert args["taskId"] == root["child"]["taskId"], (
                "Root must not impersonate the parent of a nested native task"
            )
        return await original(name, args)

    flow.host.call = guarded
    result = await flow.invoke("resume", task_id=root["id"])
    assert result["phase"] == "pr_pending", result
    receipt = store.get(root["id"])["delivery_review"]
    assert receipt["scope_validations"][0]["task_id"] == nested["id"]
    assert receipt["scope_validations"][0]["target"]["commit"] == data["commit"]
    assert data["commit"] != receipt["target"]["commit"]
    assert receipt["review_approved"] is False
    if role == "functional_tests":
        assert receipt["mandatory_validations"][0]["target"]["task_id"] == nested["id"]
    else:
        assert receipt["quality_reviews"][0]["owner_task_id"] == nested["id"]
        assert (
            result["delivery_review"]["quality_reviews"][0]["owner_task_id"]
            == nested["id"]
        )
    assert result["delivery_review"]["scope_validations"][0]["task_id"] == nested["id"]
    before = flow.snapshot(root)
    flow.host.threads[nested["child"]["childThreadId"]]["title"] = (
        "Updated display title"
    )
    flow.host.threads[review["child"]["childThreadId"]]["title"] = (
        "Review display update"
    )
    again = await flow.invoke("resume", task_id=root["id"])
    assert again["phase"] == "pr_pending" and flow.snapshot(root) == before
    assert store.get(nested["id"]) == original_nested
    assert store.get(review["id"]) == original_review
    assert flow.service.state.capacity()["occupied"] == 0


@pytest.mark.parametrize("role", ["quality_review", "functional_tests"])
@pytest.mark.parametrize(
    "defect", ["swapped_root", "outside_tree", "owner_head", "owner_log"]
)
async def test_nested_review_rejects_wrong_owner_and_stale_scope(flow, role, defect):
    root, nested, review, _ = await nested_tree(flow, role)
    store = flow.service.managed_service.store
    if defect in {"swapped_root", "outside_tree"}:
        if defect == "outside_tree":
            unrelated, _, _ = await implementation(flow, "unrelated")
            flow.host.complete(unrelated["child"]["childThreadId"])
            flow.host.caller = "origin"
            replacement = target(store.get(unrelated["id"]))
        else:
            replacement = target(store.get(root["id"]))
        get = store.get

        def changed(task_id):
            value = get(task_id)
            if task_id == review["id"]:
                value["request"]["review_target"] = replacement
            return value

        store.get = changed
    elif defect == "owner_head":
        git(
            nested["child_workspace"]["path"],
            "-c",
            "user.name=Example",
            "-c",
            "user.email=example@example.org",
            "commit",
            "--allow-empty",
            "-m",
            "New nested HEAD",
        )
    else:
        Path(store.get(nested["id"])["report"]["checks"][0]["proof"]).write_text(
            "changed nested implementation evidence"
        )
    original_review = copy.deepcopy(store.get(review["id"]))
    result = await flow.invoke("resume", task_id=root["id"])
    assert result["phase"] == "blocked", result
    assert result["memory"]["write_state"] == "not_requested"
    assert result["admission"]["state"] == "executing"
    assert store.get(review["id"]) == original_review
    expected = {
        "swapped_root": "review_owner_relation_unproven",
        "outside_tree": "review_owner_outside_observed_tree",
        "owner_head": "review_owner_delivery_unproven",
        "owner_log": "implementation_test_evidence_changed",
    }
    assert result["blocked_reason"] == expected[defect], result


async def test_nested_functional_negative_cannot_be_optional_quality(flow):
    root, nested, review, _ = await nested_tree(
        flow, "functional_tests", bad_functional=True
    )
    store = flow.service.managed_service.store
    original_review = store.get(review["id"])
    assert original_review["work_status"] == "failed"
    assert original_review["report"]["checks"][0]["status"] == "failed"
    assert original_review["memory"]["write_receipt"]["state"] == "confirmed"
    result = await flow.invoke("resume", task_id=root["id"])
    assert result["blocked_reason"] == "mandatory_functional_validation_failed", result
    assert result["memory"]["write_state"] == "not_requested"
    assert store.get(nested["id"])["memory"]["write_state"] == "not_requested"
    assert store.get(review["id"]) == original_review


@pytest.mark.parametrize("which", ["owner", "functional"])
async def test_nested_scope_corruption_during_root_memory_keeps_reservation_and_receipt(
    flow, which
):
    root, nested, review, _ = await nested_tree(flow, "functional_tests")
    store = flow.service.managed_service.store
    producer = nested if which == "owner" else review
    proof = Path(store.get(producer["id"])["report"]["checks"][0]["proof"])
    original_bytes = proof.read_bytes()
    original_nested = store.get(nested["id"])
    original_review = store.get(review["id"])
    facade = flow.service.managed_service
    factory = facade.memory

    def memory():
        adapter = factory()
        finish = adapter.finish

        async def changed(*args, **kwargs):
            cycle = await finish(*args, **kwargs)
            proof.write_text("altered nested proof during root memory")
            return cycle

        adapter.finish = changed
        return adapter

    facade.memory = memory
    blocked = await flow.invoke("resume", task_id=root["id"])
    assert blocked["blocked_reason"] == (
        "implementation_test_evidence_changed"
        if which == "owner"
        else "mandatory_functional_evidence_changed"
    ), blocked
    assert blocked["memory"]["write_receipt"]["state"] == "confirmed"
    assert blocked["admission"]["state"] == "executing"
    receipt = blocked["memory"]["write_receipt"]
    facade.memory = factory
    proof.write_bytes(original_bytes)
    accepted = await flow.invoke("resume", task_id=root["id"])
    assert accepted["phase"] == "pr_pending", accepted
    assert accepted["memory"]["write_receipt"] == receipt
    assert accepted["admission"]["state"] == "not_reserved"
    assert store.get(nested["id"]) == original_nested
    assert store.get(review["id"]) == original_review


@pytest.mark.parametrize("role", ["quality_review", "functional_tests"])
async def test_nested_validation_wrong_launch_head_has_no_execution_or_reservation(
    flow, role
):
    root, nested, _, _ = await nested_tree(flow, "quality_review", complete_root=False)
    root_head = git(root["child_workspace"]["path"], "rev-parse", "HEAD")
    expected = target(flow.service.managed_service.store.get(nested["id"]))
    assert root_head != expected["commit"]
    before_children = list(flow.host.created)
    before_capacity = flow.service.state.capacity()
    rejected = await flow.invoke(
        "start",
        kind="work",
        key="wrong-base-" + role,
        text="Validation requested at an incorrect launch base",
        deliverable_kind="validation",
        task_role=role,
        review_target=expected,
        base=root_head,
        cwd=root["child_workspace"]["path"],
    )
    assert rejected["blocked_reason"] == "validation_role_base_mismatch", rejected
    assert rejected.get("child") is None and rejected.get("memory") is None
    assert flow.host.created == before_children
    assert flow.service.state.capacity() == before_capacity
