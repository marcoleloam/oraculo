import hashlib
import subprocess
import sys

import pytest
from test_plan_service import grant, report, submit, task
from test_service import command

from oraculo.protocol import content_hash


def git(root, *args):
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=True
    ).stdout.strip()


def source(root):
    git(root, "init", "-q")
    git(root, "config", "user.email", "synthetic@example.invalid")
    git(root, "config", "user.name", "Synthetic")
    (root / "source.py").write_text("print('immutable source')\n")
    git(root, "add", "source.py")
    git(root, "commit", "-qm", "synthetic baseline")
    return git(root, "rev-parse", "HEAD")


async def prepare(service, *, argv=None, semantic=False):
    root = service.config.projects_root / "alpha"
    sha = source(root)
    spec = task("build")
    contract = dict(
        version=2,
        kind="semantic" if semantic else "command_artifact",
        commit=sha,
        argv=argv
        or [
            sys.executable,
            "-c",
            "from pathlib import Path; Path('proof.txt').write_text('proof')",
        ],
        artifact="proof.txt",
        sha256=hashlib.sha256(b"proof").hexdigest(),
    )
    if semantic:
        contract["review_task"] = "review"
    spec["criteria"][0]["outcome"] = contract
    tasks = [spec]
    if semantic:
        tasks.append(task("review", stage="review", paths=[]))
    plan = await submit(service, tasks)
    attempt = await grant(service, plan["tasks"][0]["job_id"], "build")
    return root, sha, attempt


async def verify(service, attempt, request_id="verify-one"):
    return await command(
        service,
        "outcome.verify",
        dict(
            attempt_id=attempt["id"],
            generation=attempt["generation"],
            criterion_id="proof",
        ),
        origin={"thread_id": attempt["thread_id"], "run_id": attempt["run_id"]},
        request_id=request_id,
    )


def evidence(receipt):
    return {
        "criteria": [dict(id="proof", status="passed", reference=content_hash(receipt))]
    }


async def test_service_outcome_binds_command_exit_artifact_commit_and_report(
    running_service,
):
    s = running_service
    root, sha, a = await prepare(s)
    forged = await report(
        s,
        a,
        evidence={
            "criteria": [dict(id="proof", status="passed", reference="caller says so")]
        },
    )
    assert not forged["ok"]
    verified = await verify(s, a)
    assert verified["ok"] and verified["data"]["status"] == "passed", verified
    r = verified["data"]
    assert r["commit"] == sha and r["command"]["exit"] == 0 and r["command"]["argv"]
    assert (
        r["artifact_sha256"]
        == hashlib.sha256((root / "proof.txt").read_bytes()).hexdigest()
    )
    assert (await report(s, a, evidence=evidence(r)))["ok"]


@pytest.mark.parametrize("mutation", ["artifact", "source", "head", "missing"])
async def test_post_verification_tampering_same_head_dirty_or_missing_never_completes(
    running_service, mutation
):
    s = running_service
    root, _, a = await prepare(s)
    result = await verify(s, a)
    r = result["data"]
    assert r["status"] == "passed", result
    if mutation == "artifact":
        (root / "proof.txt").write_text("tampered")
    elif mutation == "source":
        (root / "source.py").write_text("print('different command source')\n")
    elif mutation == "head":
        git(root, "commit", "--allow-empty", "-qm", "different SHA")
    elif mutation == "missing":
        (root / "proof.txt").unlink()
    assert not (await report(s, a, evidence=evidence(r)))["ok"]
    replay = await verify(s, a)
    assert replay["data"]["status"] == "pending"


async def test_dirty_source_before_command_does_not_execute(running_service):
    s = running_service
    root, _, a = await prepare(s)
    (root / "source.py").write_text("changed without changing HEAD")
    r = await verify(s, a)
    assert r["data"]["status"] == "pending"
    assert not (root / "proof.txt").exists()


async def test_command_changes_tracked_source_fails_after_execution(running_service):
    s = running_service
    argv = [
        sys.executable,
        "-c",
        "from pathlib import Path; Path('proof.txt').write_text('proof'); Path('source.py').write_text('mutated')",
    ]
    _, _, a = await prepare(s, argv=argv)
    r = await verify(s, a)
    assert r["data"]["status"] == "pending"
    assert r["data"]["command"]["exit"] == 0


async def test_nonzero_command_keeps_exact_exit_and_blocks_successor(running_service):
    s = running_service
    _, _, a = await prepare(s, argv=[sys.executable, "-c", "raise SystemExit(7)"])
    r = await verify(s, a)
    assert r["data"]["status"] == "pending" and r["data"]["command"]["exit"] == 7
    assert not (await report(s, a, evidence=evidence(r["data"])))["ok"]


async def test_ack_lost_replay_never_executes_command_again(running_service):
    s = running_service
    argv = [
        sys.executable,
        "-c",
        "from pathlib import Path; p=Path('proof.txt'); p.write_text('proof' if not p.exists() else 'ran twice')",
    ]
    root, _, a = await prepare(s, argv=argv)
    first = await verify(s, a)
    assert first["data"]["status"] == "passed"
    second = await verify(s, a)
    assert second["data"]["replay"] and second["data"]["status"] == "passed"
    assert (root / "proof.txt").read_text() == "proof"


async def test_crash_intent_never_reexecutes_after_restart(running_service):
    s = running_service
    root, _, a = await prepare(s)
    key = "outcome:v2:request:" + content_hash([a["id"], "verify-one"])
    s.state.set_meta(
        key, {"hash": content_hash([a["id"], "proof", str(root)]), "state": "intent"}
    )
    response = await verify(s, a)
    assert response["data"]["status"] == "pending"
    assert not (root / "proof.txt").exists()


async def test_semantic_positive_executor_claim_or_missing_reviewer_is_inconclusive(
    running_service,
):
    s = running_service
    _, sha, a = await prepare(s, semantic=True)
    result = await verify(s, a)
    assert result["data"]["status"] == "pending"
    forged = evidence(result["data"])
    forged["review"] = {
        "commit": sha,
        "verdict": "accepted",
        "reason": "executor says yes",
    }
    assert not (await report(s, a, evidence=forged))["ok"]
    assert not s.outcomes.satisfied(a, "proof")


async def test_duplicate_receipts_and_caller_verified_boolean_rejected(running_service):
    s = running_service
    _, _, a = await prepare(s)
    r = (await verify(s, a))["data"]
    ev = evidence(r)
    ev["criteria"] *= 2
    assert not (await report(s, a, evidence=ev))["ok"]
    from oraculo.operations import Operations

    with pytest.raises(TypeError):
        Operations(s.state).report("fake", {}, verified=True)


async def test_semantic_acceptance_has_independent_review_and_service_command_artifact_proofs(
    running_service,
):
    from test_plan_service import finish

    s = running_service
    root = s.config.projects_root / "alpha"
    sha = source(root)
    contract = dict(
        version=2,
        kind="command_artifact",
        commit=sha,
        argv=[
            sys.executable,
            "-c",
            "from pathlib import Path; Path('proof.txt').write_text('proof')",
        ],
        artifact="proof.txt",
        sha256=hashlib.sha256(b"proof").hexdigest(),
    )
    review = task("review", stage="review", paths=[])
    review["criteria"][0]["outcome"] = contract
    acceptance = task("acceptance", stage="acceptance", dependencies=["review"])
    acceptance["criteria"][0]["outcome"] = contract | dict(
        kind="semantic", review_task="review"
    )
    plan = await submit(s, [review, acceptance])
    reviewer = await grant(s, plan["tasks"][0]["job_id"], "independent-review")
    r = (await verify(s, reviewer, "verify-review"))["data"]
    assert r["status"] == "passed"
    ev = evidence(r)
    ev["review"] = {
        "commit": sha,
        "verdict": "accepted",
        "reason": "Independent command/artifact inspection is consistent",
    }
    assert (await report(s, reviewer, evidence=ev))["ok"]
    await finish(s, reviewer)
    a = await grant(s, plan["tasks"][1]["job_id"], "acceptance")
    observed = (await verify(s, a, "verify-acceptance"))["data"]
    assert observed["status"] == "passed", observed
    assert observed["review"]["attempt"] == reviewer["id"]
    assert observed["review"]["thread"] != a["thread_id"]
    assert observed["review"]["run"] != a["run_id"]
    assert observed["command"]["exit"] == 0 and observed["commit"] == sha
    assert (await report(s, a, evidence=evidence(observed)))["ok"]


async def test_assume_unchanged_tracked_source_at_same_head_still_blocks(
    running_service,
):
    s = running_service
    root, _, a = await prepare(s)
    git(root, "update-index", "--assume-unchanged", "source.py")
    (root / "source.py").write_text("print('hidden dirty source')\n")
    assert git(root, "status", "--porcelain") == ""
    r = await verify(s, a)
    assert r["data"]["status"] == "pending"
    assert not (root / "proof.txt").exists()
