"""Laboratory launchd port, real disposable release inventory and service socket."""

import asyncio
import json
import os

import pytest
from test_rollout import public_setup
from test_runtime_install import release
from test_service import command

from oraculo.config import RolloutTarget
from oraculo.local_rollout import probe
from oraculo.protocol import content_hash
from oraculo.rollout import RolloutIdentity, RolloutVerifier
from oraculo.service_control import ServiceController


class LaboratoryLaunchd(ServiceController):
    def __init__(self, root, config):
        super().__init__(root, config, platform="darwin")
        self.pid = os.getpid()
        self.matches = True

    def inspect_launchd(self):
        return dict(loaded=True, pid=self.pid)

    def loaded_matches(self, loaded, selected):
        return self.matches


def destination(service, tmp_path):
    root = tmp_path / "runtime"
    target = release(root, "laboratory-03", schema_max=7)
    manifest = json.loads((target / "manifest.json").read_text())
    manifest["delivery_source_commit"] = "d" * 40
    (target / "manifest.json").write_text(json.dumps(manifest))
    (root / "current").symlink_to(target)
    service.process_release = dict(
        release_id=manifest["release_id"],
        source_commit=manifest["source_commit"],
        delivery_source_commit="d" * 40,
        artifact_sha256=content_hash(manifest["files"]),
    )
    # Simulate an online connection whose dispatcher retains the real diagnostic
    # category; no reservation, legacy objective or alert is removed.
    service.health = "dispatch_attempts_exhausted"
    controller = LaboratoryLaunchd(root, service.config_path)
    expected = RolloutIdentity(
        source_commit="d" * 40,
        merge_sha=manifest["source_commit"],
        release_id=manifest["release_id"],
        artifact_sha256=content_hash(manifest["files"]),
        environment="local",
    )
    return root, controller, expected


async def test_local_probe_uses_real_socket_exact_manifest_and_two_spaced_reads(
    running_service, tmp_path
):
    s = running_service
    root, controller, expected = destination(s, tmp_path)
    # The diagnostic remains visible separately from readiness.
    core = await command(s, "runtime.probe", {})
    assert core["data"]["liveness"] and core["data"]["readiness"]
    assert s._health == "dispatch_attempts_exhausted"

    async def observed():
        return await asyncio.to_thread(
            probe, root, s.config_path, "local", controller=controller
        )

    receipt = await RolloutVerifier(observed).observe(expected)
    assert receipt["state"] == "confirmed", receipt
    assert receipt["samples"][1]["elapsed"] - receipt["samples"][0]["elapsed"] >= 5
    assert receipt["samples"][0]["process"]["process_pid"] == os.getpid()


@pytest.mark.parametrize(
    "mutation",
    [
        "config",
        "pid",
        "process_release",
        "artifact",
        "maintenance",
        "disconnected",
        "plist",
    ],
)
async def test_local_identity_or_readiness_mutation_never_confirms(
    running_service, tmp_path, mutation
):
    s = running_service
    root, controller, _ = destination(s, tmp_path)
    if mutation == "config":
        s.config_path.write_text(
            s.config_path.read_text() + "\n# changed configuration\n"
        )
    if mutation == "pid":
        controller.pid += 1
    if mutation == "process_release":
        s.process_release["source_commit"] = "f" * 40
    if mutation == "artifact":
        (root / "current/payload").write_text("changed artifact")
    if mutation == "maintenance":
        s.state.set_meta("runtime_maintenance", {"token": "lab"})
    if mutation == "disconnected":
        s.health = "disconnected"
    if mutation == "plist":
        controller.matches = False
    try:
        result = await asyncio.to_thread(
            probe, root, s.config_path, "local", controller=controller
        )
    except Exception as exc:
        from oraculo.runtime import RuntimeError

        assert mutation == "artifact" and isinstance(exc, RuntimeError)
    else:
        assert result["healthy"] is False


async def test_rollout_current_view_and_report_gate_invalidate_config_mutation(
    running_service,
):
    s = running_service
    task, _, _, _, _ = await public_setup(s)
    origin = dict(thread_id="origin", run_id="origin-run")
    result = await command(
        s,
        "rollout.verify",
        dict(task_id=task["id"], operation_id="rollout-op"),
        origin=origin,
    )
    assert result["ok"] and result["data"]["state"] == "confirmed", result
    op = s.state._one("SELECT * FROM operations WHERE id='rollout-op'")
    assert s.plans._rollout_current(op)
    s.config_path.write_text(s.config_path.read_text() + "\n# configuration changed\n")
    current = s.managed_service.store.describe(task["id"])
    assert current["rollout_verification"]["state"] == "unknown"
    assert not s.plans._rollout_current(op)
    # The immutable previous observation remains available as history.
    assert (
        s.managed_service.store.get(task["id"])["rollout_verification"]["state"]
        == "confirmed"
    )


def test_runtime_target_configuration_is_usable_without_shell_or_caller_argv(tmp_path):
    target = RolloutTarget(runtime_root=tmp_path / "runtime")
    assert target.runtime_root and not target.verifier_argv and target.cwd is None
