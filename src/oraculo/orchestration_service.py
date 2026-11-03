"""Public orchestration commands; authority comes from service observations."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from oraculo.ci_events import CIBinding
from oraculo.decisions import Snapshot
from oraculo.protocol import content_hash
from oraculo.rollout import RolloutIdentity, RolloutVerifier
from oraculo.routing import Router
from oraculo.state import StateError


def job_context(service, project_id, job_id):
    job = service.state.get_job(job_id)
    objective = service.state.get_objective(job["objective_id"]) if job else None
    if not objective or objective["project_id"] != project_id:
        raise StateError("job_project_mismatch")
    project = service.project(project_id)
    return job, objective, project


async def checkout(service, root, selected=None):
    root = Path(root).resolve()
    cwd = Path(selected or root).resolve()
    a = (
        await service.run_command(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], root
        )
    ).strip()
    b = (
        await service.run_command(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd
        )
    ).strip()
    if Path(a).resolve() != Path(b).resolve():
        raise StateError("verification_checkout_mismatch")
    return cwd


def decision_snapshot(service, project_id, job_id, cwd):
    # Recomputed by the service after network I/O; no caller-signed snapshot.
    job, obj, _ = job_context(service, project_id, job_id)
    payload = json.loads(job["payload"])
    binding = CIBinding.model_validate(payload["ci_binding"])
    receipt = service.ci_events.current(project_id, binding)
    if receipt is None:
        raise StateError("ci_observation_required")
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    ).stdout.strip()
    attempt = service.state._one(
        "SELECT * FROM attempts WHERE job_id=? ORDER BY number DESC LIMIT 1", (job_id,)
    )
    return Snapshot(
        project=project_id,
        repository=binding.repository,
        head=head,
        source=binding.head,
        task=job_id,
        attempt=attempt["id"] if attempt else "not_started",
        generation=obj["stop_generation"],
        event=receipt["event"]["event_id"],
        ci_run=binding.run,
        ci_attempt=binding.attempt,
        revision=receipt["event"]["sequence"],
        state_hash=content_hash([job, obj, attempt, receipt]),
        policy_hash=content_hash(service.config.model_dump(mode="json")),
        catalog_hash=content_hash(service.catalog),
    )


async def shadow(service, project_id, job_id):
    job, _, project = job_context(service, project_id, job_id)
    payload = json.loads(job["payload"])
    cwd = await checkout(service, project["path"], payload.get("cwd"))
    snap = decision_snapshot(service, project_id, job_id, cwd)
    binding = CIBinding.model_validate(payload["ci_binding"])
    event = service.ci_events.current(project_id, binding)["event"]
    # Baseline uses the current Router. Recommendation never applies a profile.
    router = Router(service.config, service.state)
    target = router.select(job["kind"], service.catalog)
    baseline = (
        target.__dict__
        if target and hasattr(target, "__dict__")
        else (
            {
                "provider": target.provider,
                "model": target.model,
                "profile": target.profile,
            }
            if target
            else {"reason": router.last_reason}
        )
    )
    result = await service.decisions.advise(
        snap,
        event["diagnostic"],
        lambda: decision_snapshot(service, project_id, job_id, cwd).model_dump(
            mode="json"
        ),
    )
    record = dict(
        baseline=baseline,
        snapshot_hash=content_hash(snap.model_dump(mode="json")),
        recommendation=result,
        recommended_profile=target.profile
        if target
        and result["state"] == "answered"
        and result["recommendation"] != "unknown"
        else None,
        applied=False,
        fallback_executed=False,
    )
    service.state.set_meta("shadow:v1:" + result["key"], record)
    return record


async def verify_rollout_operation(service, operation):
    from oraculo.operations import Operations

    operations = Operations(service.state)
    expected = json.loads(operation["expected_state"])
    try:
        identity = RolloutIdentity.model_validate(expected["rollout"])
        target = service.config.rollout_targets.get(identity.environment)
        if (
            target is None
            or expected["commit"] != identity.merge_sha
            or expected["target"] != identity.environment
            or expected["artifact"] != identity.artifact_sha256
        ):
            raise StateError("rollout_target_unconfigured_or_mismatched")

        target_hash = content_hash(target.model_dump(mode="json"))

        async def probe():
            return json.loads(
                await service.run_command(target.verifier_argv, target.cwd)
            )

        receipt = await RolloutVerifier(probe).observe(identity)
        current_target = service.config.rollout_targets.get(identity.environment)
        if (
            current_target is None
            or content_hash(current_target.model_dump(mode="json")) != target_hash
        ):
            receipt = dict(
                receipt, state="unknown", reason="verifier_configuration_changed"
            )
        service.state.audit(
            "rollout_observed", {"operation": operation["id"], "receipt": receipt}
        )
        observed = dict(
            expected, healthy=receipt["state"] == "confirmed", rollout_receipt=receipt
        )
        return await operations.reconcile(operation["id"], lambda frozen: observed)
    except (KeyError, ValueError, OSError, TimeoutError):
        # A previously confirmed destination can become unknown; its previous
        # proof remains in the immutable audit history.
        return await operations.reconcile(operation["id"], lambda frozen: {})


async def command(service, envelope):
    p, kind = envelope.payload, envelope.kind
    project = service.project(envelope.project_id)
    allowed = {
        "ci.observe": {"job_id"},
        "decisions.shadow": {"job_id"},
        "decisions.kill": {"enabled"},
        "outcome.verify": {"attempt_id", "generation", "criterion_id", "cwd"},
        "rollout.verify": {"task_id", "operation_id"},
    }[kind]
    if set(p) - allowed:
        raise StateError("orchestration_payload_fields_invalid")
    if kind == "decisions.kill":
        if envelope.origin.thread_id or envelope.origin.run_id:
            raise StateError("decisions_kill_requires_local_operator")
        service.decisions.kill(p["enabled"])
        return {
            "state": "disabled" if p["enabled"] else "configured_policy",
            "kill": p["enabled"],
        }
    if kind == "outcome.verify":
        attempt = service.owner(envelope, executor=True)
        cwd = await checkout(service, project["path"], p.get("cwd"))
        return await service.outcomes.verify(
            attempt, p["criterion_id"], cwd, envelope.request_id
        )
    if kind in {"ci.observe", "decisions.shadow"}:
        if (
            envelope.origin.thread_id != project["coordinator_thread_id"]
            or not envelope.origin.run_id
        ):
            raise StateError("coordinator_identity_required")
        job, _, _ = job_context(service, envelope.project_id, p["job_id"])
        if kind == "decisions.shadow":
            return await shadow(service, envelope.project_id, job["id"])
        binding = CIBinding.model_validate(json.loads(job["payload"])["ci_binding"])
        return await service.ci_events.observe(
            envelope.project_id, binding, service.ci_observer
        )
    if service.managed_service is None:
        raise StateError("managed_task_required")
    store = service.managed_service.store
    task = store.get(p["task_id"])
    from oraculo.deliverables import require_implementation

    require_implementation(task)
    if (
        task["origin"]["thread_id"] != envelope.origin.thread_id
        or not envelope.origin.run_id
        or (task.get("project") or {}).get("project_id") != envelope.project_id
    ):
        raise StateError("rollout_origin_or_project_mismatch")
    operation = service.state._one(
        "SELECT * FROM operations WHERE id=?", (p["operation_id"],)
    )
    if not operation or operation["kind"] != "deploy":
        raise StateError("rollout_operation_required")
    attempt = service.state.get_attempt(operation["attempt_id"])
    job_context(service, envelope.project_id, attempt["job_id"])
    identity = RolloutIdentity.model_validate(
        json.loads(operation["expected_state"])["rollout"]
    )
    merge, report = task.get("merge_receipt") or {}, task.get("report") or {}
    if (
        merge.get("state") != "merged"
        or merge.get("merge_sha") != identity.merge_sha
        or report.get("commit") != identity.source_commit
        or not any(
            d.get("state") == "reported"
            and d.get("environment") == identity.environment
            and d.get("release") == identity.merge_sha
            for d in task.get("deployments", [])
        )
    ):
        raise StateError("rollout_source_merge_report_binding_required")
    before = content_hash(
        [merge, report, task.get("deployments"), task.get("snapshot_revision")]
    )
    observed = await verify_rollout_operation(service, operation)
    current = store.get(task["id"])
    after = content_hash(
        [
            current.get("merge_receipt"),
            current.get("report"),
            current.get("deployments"),
            current.get("snapshot_revision"),
        ]
    )
    if before != after:
        from oraculo.operations import Operations

        await Operations(service.state).reconcile(operation["id"], lambda expected: {})
        confirmation = {"state": "unknown", "reason": "task_snapshot_changed"}
        store.update(
            task["id"],
            current["phase"],
            {"rollout_verification": confirmation},
            rollout_verification=confirmation,
        )
        return confirmation
    proof = json.loads(observed["observed_state"] or "{}")
    confirmation = proof.get("rollout_receipt", {"state": "unknown"})
    if observed["state"] != "confirmed":
        confirmation = dict(
            confirmation, state="unknown", reason="destination_or_operation_changed"
        )
    store.update(
        task["id"],
        task["phase"],
        {"rollout_verification": confirmation},
        rollout_verification=confirmation,
    )
    return confirmation
