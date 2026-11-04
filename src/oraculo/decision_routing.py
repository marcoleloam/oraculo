"""Decisions ranks deterministic options; grants remain State-owned effects."""

from __future__ import annotations

import json
import subprocess
from dataclasses import asdict
from pathlib import Path

from oraculo.decisions import Snapshot
from oraculo.protocol import content_hash
from oraculo.routing import Router
from oraculo.state import StateError


def routing_traits(config, options):
    from oraculo.config import RoutingTraits

    result = []
    for option in options:
        provider = next(
            p
            for p in config.providers.values()
            if p.instance_id == option.provider and option.model in p.models
        )
        result.append(
            provider.routing_traits.get(option.model, RoutingTraits()).model_dump()
        )
    return result


def job_options(service, job):
    explicit = json.loads(job["payload"]).get("target")
    return service.router.candidates(job["kind"], service.catalog, explicit=explicit)


def job_snapshot(service, job_id):
    job = service.state.get_job(job_id)
    obj = service.state.get_objective(job["objective_id"])
    project = service.project(obj["project_id"])
    root = Path(json.loads(job["payload"]).get("cwd") or project["path"]).resolve()
    # An executor cannot redirect observation to another repository.
    from oraculo.managed_tasks import git

    common = [
        git(p, "rev-parse", "--path-format=absolute", "--git-common-dir")
        for p in (root, project["path"])
    ]
    if common[0] != common[1]:
        raise StateError("decision_checkout_mismatch")
    head = git(root, "rev-parse", "HEAD")
    options = job_options(service, job)
    binding = json.loads(job["payload"]).get("ci_binding") or {}
    return Snapshot(
        project=obj["project_id"],
        repository=common[0],
        head=head,
        source=head,
        task=job_id,
        attempt="admission",
        generation=obj["stop_generation"],
        event="admission",
        ci_run=binding.get("run", "none"),
        ci_attempt=binding.get("attempt", 1),
        revision=0,
        state_hash=content_hash(
            [
                job,
                obj,
                service.state.capacity(),
                service.state.admission_blockers(job),
                [asdict(t) for t in options],
            ]
        ),
        policy_hash=content_hash(service.config.model_dump(mode="json")),
        catalog_hash=content_hash(service.catalog),
    )


async def prepare_job_route(service, job):
    options = job_options(service, job)
    record = dict(
        baseline=asdict(options[0]) if options else None,
        selected=None,
        applied=False,
        fallback_executed=False,
        reason="deterministic",
    )
    if (
        len(options) < 2
        or json.loads(job["payload"]).get("target")
        or service.state.admission_blockers(job)
        or service.config.decisions.mode not in {"active", "assisted"}
    ):
        return record
    try:
        snap = job_snapshot(service, job["id"])
    except (OSError, ValueError, subprocess.SubprocessError):
        return dict(record, reason="snapshot_unavailable")
    features = {"kind": job["kind"]}
    hint = service.state.get_meta("ci-triage:v1:" + job["id"], {})
    if hint.get("head") == snap.head:
        features["ci_cause"] = hint.get("recommendation")
    result = await service.decisions.choose(
        snap,
        len(options),
        features,
        lambda: job_snapshot(service, job["id"]).model_dump(mode="json"),
        traits=routing_traits(service.config, options),
    )
    record.update(
        reason=result["reason"], advice=result, snapshot=snap.model_dump(mode="json")
    )
    if result["state"] == "answered" and result["recommendation"] != "unknown":
        index = int(result["recommendation"].removeprefix("option_"))
        record["selected"] = asdict(options[index])
    return record


def consume_job_route(service, job, record):
    """Called inside the grant transaction. Recompute every deterministic gate."""
    options = job_options(service, job)
    binding = json.loads(job["payload"]).get("ci_binding")
    if binding and job_snapshot(service, job["id"]).head != binding["head"]:
        raise StateError("ci_source_head_changed")
    if not options:
        return None
    if record and record.get("selected"):
        try:
            unchanged = (
                job_snapshot(service, job["id"]).model_dump(mode="json")
                == record["snapshot"]
            )
        except (OSError, ValueError, subprocess.SubprocessError):
            unchanged = False
        if unchanged and service.decisions.usable(record.get("advice", {})):
            for target in options:
                if asdict(target) == record["selected"]:
                    return target
        record.update(selected=None, reason="effect_revalidation_failed")
    return options[0]


async def managed_route(engine, task, req, caps, plan):
    """Only unpinned native tasks can rank options; composer always takes precedence."""
    advisor = getattr(engine, "decisions", None)
    if (
        not advisor
        or not plan["native"]
        or advisor.config.mode not in {"active", "assisted"}
    ):
        return None
    router = Router(engine.config, engine.admission)
    explicit = req.get("target")
    if not explicit and req.get("model_selection"):
        selected = req["model_selection"]
        explicit = dict(
            providerInstanceId=selected.get("instanceId"), model=selected.get("model")
        )
    if explicit:
        # User-chosen provider/model: only the host catalog can refuse it;
        # the config allowlist/profiles never apply. Capacity is admission's.
        if not Router(engine.config).candidates(
            "implementation", caps, explicit=explicit
        ):
            raise StateError("explicit_model_not_in_host_catalog")
        return None
    if caps.get("inheritedModel"):
        # Inheritance from the origin conversation is unconditional.
        return None
    options = router.candidates("implementation", caps)
    if len(options) < 2:
        return None
    from oraculo.managed_tasks import workspace

    def snapshot(current_caps):
        ws = workspace(plan["ws"]["path"])
        return Snapshot(
            project=task["project"]["project_id"],
            repository=ws["common_dir"],
            head=ws["head"],
            source=plan["base_sha"],
            task=task["id"],
            attempt="managed-admission",
            generation=0,
            event="launch",
            ci_run="none",
            ci_attempt=1,
            revision=task.get("snapshot_revision", 0),
            state_hash=content_hash([ws, task["request"], engine.admission.capacity()]),
            policy_hash=content_hash(engine.config.model_dump(mode="json")),
            catalog_hash=content_hash(current_caps),
        )

    frozen = snapshot(caps)
    result = await advisor.choose(
        frozen,
        len(options),
        {"kind": "implementation"},
        lambda: snapshot(caps).model_dump(mode="json"),
        traits=routing_traits(engine.config, options),
    )
    fresh = await engine.host.call("orchestrator_capabilities", {})
    current = router.candidates("implementation", fresh)
    selected = options[0]
    used = False
    if (
        result["state"] == "answered"
        and result["recommendation"] != "unknown"
        and snapshot(fresh) == frozen
    ):
        selected = options[int(result["recommendation"].removeprefix("option_"))]
        used = True
    if selected not in current:
        if not current:
            raise StateError("no_eligible_subscription_model")
        selected, used = current[0], False
    return dict(
        target={"providerInstanceId": selected.provider, "model": selected.model},
        advice=result,
        selected=used and selected != options[0],
        fallback=not used,
        applied=False,
        fallback_executed=False,
        config_hash=frozen.policy_hash,
        head=frozen.head,
    )
