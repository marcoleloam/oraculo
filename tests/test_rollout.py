import copy
import json
import sys

import pytest
from test_runtime_install import setup as runtime_setup
from test_service import command

from oraculo import runtime as rt
from oraculo.config import RolloutTarget
from oraculo.managed_service import ManagedService
from oraculo.operations import Operations
from oraculo.protocol import content_hash
from oraculo.rollout import RolloutIdentity, RolloutVerifier


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.now += seconds


def identity(**changes):
    return RolloutIdentity(
        **(
            dict(
                source_commit="a" * 40,
                merge_sha="b" * 40,
                release_id="synthetic-release",
                artifact_sha256="c" * 64,
                environment="synthetic",
            )
            | changes
        )
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("source_commit", "d" * 40),
        ("merge_sha", "d" * 40),
        ("release_id", "old"),
        ("artifact_sha256", "d" * 64),
        ("environment", "other"),
        ("healthy", False),
    ],
)
async def test_rollout_two_observations_must_match_identity_and_health(field, value):
    c = Clock()
    calls = []
    expected = identity()

    async def probe():
        calls.append(c())
        return (
            expected.model_dump()
            | {"healthy": True}
            | ({field: value} if len(calls) == 2 else {})
        )

    r = await RolloutVerifier(probe, clock=c, monotonic=c, sleep=c.sleep).observe(
        expected
    )
    assert r["state"] == "unknown" and len(calls) == 2 and calls[1] - calls[0] == 5


async def test_rollout_deadline_and_spacing_use_observed_clock():
    c = Clock()
    expected = identity()

    async def probe():
        c.now += 121
        return expected.model_dump() | {"healthy": True}

    assert (
        await RolloutVerifier(probe, clock=c, monotonic=c, sleep=c.sleep).observe(
            expected
        )
    )["state"] == "unknown"
    c.now = 1000

    async def fast():
        return expected.model_dump() | {"healthy": True}

    async def no_wait(seconds):
        pass

    assert (
        await RolloutVerifier(fast, clock=c, monotonic=c, sleep=no_wait).observe(
            expected
        )
    )["state"] == "unknown"


async def public_setup(service):
    expected = identity()
    value = expected.model_dump()
    destination = service.config.state_dir / "destination.json"
    destination.write_text(json.dumps(value | {"healthy": True}))
    service.config.rollout_targets[expected.environment] = RolloutTarget(
        verifier_argv=[
            sys.executable,
            "-c",
            "from pathlib import Path; print(Path('destination.json').read_text())",
        ],
        cwd=service.config.state_dir,
    )
    plan = service.plans.submit(
        "alpha",
        {
            "text": "Deploy synthetic",
            "tasks": [
                {
                    "id": "deploy",
                    "owner": "synthetic",
                    "brief": "Verify destination",
                    "stage": "deployment",
                    "criteria": [
                        {"id": "destination", "description": "Observed destination"}
                    ],
                }
            ],
        },
        "synthetic-deploy",
        service.config.profiles,
    )
    job = service.state.get_job(plan["tasks"][0]["job_id"])
    a = service.state.acquire_grant(job["id"], "codex", "current", "coordinator", "run")
    service.state.claim_grant(a["id"], a["generation"], "executor", "executor-run")
    op = Operations(service.state)
    op.prepare(
        "rollout-op",
        a["id"],
        "deploy",
        dict(
            commit=expected.merge_sha,
            target=expected.environment,
            artifact=expected.artifact_sha256,
            rollout=value,
        ),
        generation=a["generation"],
        checks_passed=True,
    )
    op.mark_dispatched("rollout-op")
    service.managed_service = ManagedService(service)
    store = service.managed_service.store
    task = store.create(
        {"thread_id": "origin", "run_id": "origin-run"},
        "rollout",
        {"title": "synthetic deployment"},
    )
    reported = dict(
        state="reported",
        environment=expected.environment,
        release=expected.merge_sha,
        verification="not_observed",
    )
    store.update(
        task["id"],
        "merged",
        {"synthetic": "bound"},
        project={"project_id": "alpha"},
        merge_receipt={"state": "merged", "merge_sha": expected.merge_sha},
        report={"commit": expected.source_commit},
        deployments=[reported],
    )
    return task, expected, destination, op, reported


async def verify(service, task, **extra):
    return await command(
        service,
        "rollout.verify",
        dict(task_id=task["id"], operation_id="rollout-op") | extra,
        origin={"thread_id": "origin", "run_id": "origin-run"},
    )


async def test_public_lifecycle_confirmed_then_destination_changed_invalidates_current_and_gates(
    running_service, monkeypatch
):
    service = running_service
    task, expected, dest, operations, reported = await public_setup(service)
    clock = Clock()
    import oraculo.orchestration_service as entry

    monkeypatch.setattr(
        entry,
        "RolloutVerifier",
        lambda probe: RolloutVerifier(probe, monotonic=clock, sleep=clock.sleep),
    )
    first = await verify(service, task)
    assert first["ok"] and first["data"]["state"] == "confirmed", first
    assert operations.get("rollout-op")["state"] == "confirmed"
    first_receipt = copy.deepcopy(first["data"])
    attempt = service.state.get_attempt(operations.get("rollout-op")["attempt_id"])
    report = {
        "status": "succeeded",
        "evidence": {
            "criteria": [
                {
                    "id": "destination",
                    "status": "passed",
                    "reference": "service rollout receipt",
                }
            ]
        },
    }
    service.plans.validate_report(attempt, report)
    dest.write_text(
        json.dumps(expected.model_dump() | {"healthy": False, "release_id": "old"})
    )
    second = await verify(service, task)
    assert second["ok"] and second["data"]["state"] == "unknown", second
    assert operations.get("rollout-op")["state"] == "uncertain"
    from oraculo.state import StateError

    with pytest.raises(StateError, match="delivery_operation_not_confirmed"):
        service.plans.validate_report(attempt, report)
    assert operations.get("rollout-op")["observed_state"] != json.dumps(first_receipt)
    current = service.managed_service.store.describe(task["id"])
    assert current["rollout_verification"]["state"] == "unknown"
    assert current["deployments"] == [reported]
    history = list(
        service.state.connection.execute(
            "SELECT payload FROM audit_events WHERE kind='rollout_observed'"
        )
    )
    assert any(json.loads(r[0])["receipt"] == first_receipt for r in history)
    # The operation gate now sees no confirmed deploy, preventing completion.
    assert not service.state._one(
        "SELECT id FROM operations WHERE id='rollout-op' AND state='confirmed'"
    )


async def test_caller_payload_cannot_confirm_or_override_probe(running_service):
    service = running_service
    task, _, _, _, _ = await public_setup(service)
    result = await verify(service, task, verified=True)
    assert not result["ok"]
    result = await verify(service, task, healthy=True)
    assert not result["ok"]


async def test_missing_report_or_different_source_cannot_confirm(running_service):
    s = running_service
    task, _, _, ops, _ = await public_setup(s)
    s.managed_service.store.update(
        task["id"], "merged", {"synthetic": "changed"}, report={"commit": "d" * 40}
    )
    r = await verify(s, task)
    assert not r["ok"] and ops.get("rollout-op")["state"] != "confirmed"


# Existing runtime activation machinery is used with disposable roots and a fake
# controller only; no installed daemon/service or launchd is touched.
setup = runtime_setup


def test_disposable_prepare_verify_activate_probe_rollback(setup):
    root, bin_dir, conf, controller = setup
    prepared = rt.verify(root / "releases/new", installed=True)
    assert prepared["release_id"] == "new"
    assert rt.activate(root, bin_dir, conf, controller, "new")["state"] == "active"
    observation = rt.rollout_identity(
        root,
        "synthetic",
        lambda: dict(
            healthy=True,
            release_id="new",
            source_commit="a" * 40,
            merge_sha="b" * 40,
            environment="synthetic",
        ),
    )
    assert observation["healthy"] and observation["artifact_sha256"] == content_hash(
        prepared["files"]
    )
    assert (
        rt.activate(root, bin_dir, conf, controller, rollback=True)["state"] == "active"
    )
    assert rt.current_release(root) == "old"
    observation = rt.rollout_identity(
        root,
        "synthetic",
        lambda: dict(
            healthy=True,
            release_id="new",
            source_commit="a" * 40,
            merge_sha="b" * 40,
            environment="synthetic",
        ),
    )
    assert observation["healthy"] is False


async def test_existing_runtime_prepare_activate_two_probes_and_rollback_in_disposable_root(
    setup, monkeypatch
):
    from pathlib import Path

    root, bin_dir, conf, controller = setup
    bundle = root.parent / "synthetic-bundle"
    bundle.mkdir()
    (bundle / "oraculo-0.1.0-py3-none-any.whl").write_bytes(
        b"synthetic installer fixture; real wheel is checked by distribution gate"
    )
    (bundle / "requirements.txt").write_text("# synthetic empty dependency set\n")
    rt.create_manifest(bundle, "candidate", "a" * 40, 1, 4)

    def installer(argv, **kwargs):
        # Only dependency installation is simulated. Runtime manifests, intents,
        # maintenance, pointers, verification and rollback are the real code.
        if "venv" in argv:
            target = Path(argv[-1])
            (target / "bin").mkdir(parents=True)
            (target / "bin/python").symlink_to(sys.executable)
        return ""

    monkeypatch.setattr(rt, "run", installer)
    assert rt.prepare(root, bin_dir, bundle)["state"] == "prepared"
    manifest = rt.verify(root / "releases/candidate", installed=True)
    assert (
        rt.activate(root, bin_dir, conf, controller, "candidate")["state"] == "active"
    )
    clock = Clock()

    async def probe():
        return rt.rollout_identity(
            root,
            "synthetic",
            lambda: dict(
                healthy=True,
                release_id="candidate",
                source_commit="a" * 40,
                merge_sha="b" * 40,
                environment="synthetic",
            ),
        )

    expected = identity(
        release_id="candidate", artifact_sha256=content_hash(manifest["files"])
    )
    proof = await RolloutVerifier(probe, monotonic=clock, sleep=clock.sleep).observe(
        expected
    )
    assert (
        proof["state"] == "confirmed"
        and proof["samples"][1]["elapsed"] - proof["samples"][0]["elapsed"] == 5
    )
    assert (
        rt.activate(root, bin_dir, conf, controller, rollback=True)["state"] == "active"
    )
    assert rt.current_release(root) == "old" and rt.verify(
        root / "releases/old", installed=True
    )


async def test_public_task_snapshot_mutation_during_probe_invalidates_confirmation(
    running_service, monkeypatch
):
    s = running_service
    task, _, _, operations, _ = await public_setup(s)
    clock = Clock()
    import oraculo.orchestration_service as entry

    mutated = False

    def verifier(probe):
        async def changing_probe():
            nonlocal mutated
            value = await probe()
            if not mutated:
                mutated = True
                s.managed_service.store.update(
                    task["id"],
                    "merged",
                    {"synthetic": "concurrent change"},
                    report={"commit": "d" * 40},
                )
            return value

        return RolloutVerifier(changing_probe, monotonic=clock, sleep=clock.sleep)

    monkeypatch.setattr(entry, "RolloutVerifier", verifier)
    result = await verify(s, task)
    assert result["ok"] and result["data"]["state"] == "unknown", result
    assert operations.get("rollout-op")["state"] == "uncertain"
    assert (
        s.managed_service.store.describe(task["id"])["rollout_verification"]["state"]
        == "unknown"
    )
