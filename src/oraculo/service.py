from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import logging
import logging.handlers
import os
import shlex
import signal
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from oraculo.config import Config, resources_root
from oraculo.memory.bridge import MemoryBridge, artifact_context, public_cycle
from oraculo.memory.context import Context
from oraculo.memory.cycles import MemoryCycles
from oraculo.memory.maintenance import Maintenance
from oraculo.memory.publish import Publisher
from oraculo.memory.review import ReviewManager
from oraculo.plans import Plans
from oraculo.protocol import (
    CONTROL_MARKER,
    MAX_ENVELOPE_BYTES,
    Envelope,
    canonical_json,
    parse_envelope,
    validate_memory_submission,
)
from oraculo.routing import Router
from oraculo.settlement import Settlements
from oraculo.settlement_store import SettlementStore
from oraculo.state import State, StateError


def validate_startup_config(config: Config, config_path: Path) -> None:
    expected = os.environ.get("ORACULO_EXPECTED_CONFIG_SHA256")
    if expected is not None and (
        not expected
        or config._loaded_sha256 != expected
        or hashlib.sha256(config_path.read_bytes()).hexdigest() != expected
    ):
        raise StateError("startup_config_mismatch")


class Singleton:
    def __init__(self, directory: Path):
        self.directory = directory
        self.descriptor: int | None = None

    def __enter__(self) -> Singleton:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.directory.is_symlink() or self.directory.stat().st_uid != os.getuid():
            raise ValueError("invalid_state_directory")
        self.directory.chmod(0o700)
        self.descriptor = os.open(
            self.directory / "daemon.lock",
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
            0o600,
        )
        try:
            fcntl.flock(self.descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(self.descriptor)
            self.descriptor = None
            raise
        return self

    def __exit__(self, *_: Any) -> None:
        if self.descriptor is not None:
            os.close(self.descriptor)
            self.descriptor = None


class Service:
    def __init__(self, config: Config, config_path: Path):
        validate_startup_config(config, config_path)
        self.config = config
        self.config_path = config_path
        self.config_sha256 = config._loaded_sha256 or (
            hashlib.sha256(config_path.read_bytes()).hexdigest()
            if config_path.is_file()
            else None
        )
        self.state = State(
            config.db_path,
            max_executors=config.max_executors,
            provider_limits={
                p.instance_id: min(
                    alias.max_executors
                    for alias in config.providers.values()
                    if alias.instance_id == p.instance_id
                )
                for p in config.providers.values()
            },
        )
        self.plans = Plans(self.state)
        self.settlements = SettlementStore(self.state)
        self.publisher = Publisher(config.vault_root, config.state_dir, lock_timeout=2)
        self.publisher.git.timeout = 30
        self.reviews = ReviewManager(self.publisher)
        self.memory_cycles = MemoryCycles(self.state, self.publisher)
        self.memory_flush: asyncio.Task | None = None
        self.managed_flush: asyncio.Task | None = None
        self.managed_service = None
        self.router = Router(config, self.state)
        self.catalog: dict = {}
        self.health = "starting"
        self.stop = asyncio.Event()
        self.memory_lock = asyncio.Lock()
        self.memory_operations = 0
        self.log = logging.getLogger("oraculo")
        # Claim an existing ledger before any scheduler admission can occur.
        if (config.state_dir / "managed-tasks.sqlite3").exists():
            from oraculo.managed_service import ManagedService

            self.managed_service = ManagedService(self)

    def project(self, project_id: str) -> dict:
        project = self.state.get_project(project_id)
        if (
            not project
            or project["status"] != "registered"
            or not self.config.project_in_scope(project["path"])
        ):
            raise StateError("project_not_registered_or_outside_scope")
        return project

    def owner(self, envelope: Envelope, *, executor: bool = False) -> dict:
        payload = envelope.payload
        attempt = self.state.validate_grant(
            payload["attempt_id"], int(payload["generation"])
        )
        job = self.state.get_job(attempt["job_id"])
        objective = self.state.get_objective(job["objective_id"])
        if objective["project_id"] != envelope.project_id:
            raise StateError("grant_project_mismatch")
        expected = attempt["thread_id"] if executor else attempt["owner_thread_id"]
        if (
            not envelope.origin.thread_id
            or not envelope.origin.run_id
            or envelope.origin.thread_id != expected
        ):
            raise StateError("grant_owner_mismatch")
        if executor and envelope.origin.run_id != attempt["run_id"]:
            raise StateError("grant_run_mismatch")
        return attempt

    def status(self, project_id: str) -> dict:
        plans = [
            self.plans.describe(o["id"])
            for o in self.state.list_objectives()
            if (project_id == "*" or o["project_id"] == project_id)
            and self.plans.get(o["id"])
        ]
        objectives = [
            o
            for o in self.state.list_objectives()
            if project_id == "*" or o["project_id"] == project_id
        ]
        identifiers = {o["id"] for o in objectives}
        jobs = [j for j in self.state.list_jobs() if j["objective_id"] in identifiers]
        plans = [self.operational_plan(plan) for plan in plans]
        lease = self.state.lease()
        if lease:
            lease = dict(lease, attempt=self.state.get_attempt(lease["attempt_id"]))
        return {
            "runtime": {
                "pid": os.getpid(),
                "release_id": os.environ.get("ORACULO_RELEASE_ID"),
                "config_path": str(self.config_path.resolve()),
                "config_sha256": self.config_sha256,
                "protocol_version": 1,
            },
            "maintenance": __import__(
                "oraculo.maintenance", fromlist=["status"]
            ).status(self.state),
            "health": self.health,
            "projects": self.state.list_projects(),
            "objectives": [
                {
                    k: o[k]
                    for k in ("id", "project_id", "origin", "status", "stop_generation")
                }
                for o in objectives[-100:]
            ],
            "jobs": [
                dict(
                    {k: j[k] for k in ("id", "objective_id", "kind", "state")},
                    admission=self.admission(j),
                )
                for j in jobs[-200:]
            ],
            "executor": lease,
            "executors": [
                dict(item, attempt=self.state.get_attempt(item["attempt_id"]))
                for item in self.state.leases()
            ],
            "max_executors": self.config.max_executors,
            "capacity": self.state.capacity(),
            "provider_capacity": {
                p.instance_id: self.state.provider_capacity(p.instance_id)
                for p in self.config.providers.values()
            },
            "plans": plans,
            "settlements": self.settlements.list(project_id),
            "memory_cycles": [
                public_cycle(c) for c in self.memory_cycles.list(project_id)
            ],
            "additional_spend_allowed": False,
        }

    def operational_plan(self, plan: dict) -> dict:
        plan["dag_ready"] = list(plan["ready"])
        for task in plan["tasks"]:
            task["admission"] = self.admission(self.state.get_job(task["job_id"]))
        plan["ready"] = [
            task["id"] for task in plan["tasks"] if task["admission"]["eligible"]
        ]
        if plan["dag_ready"] and not plan["ready"]:
            plan["next_action"] = (
                "Resolve admission blockers or wait for reconciliation"
            )
        if plan["concurrency"]["limited"]:
            plan["next_action"] += (
                "; requested parallelism exceeds effective capacity: "
                f"{plan['concurrency']['effective']}"
            )
        return plan

    def admission(self, job: dict) -> dict:
        blockers = self.state.admission_blockers(job)
        if job["state"] in {"queued", "quota_wait", "auth_required"}:
            target = self.router.select(job["kind"], self.catalog)
            if target is None:
                blockers.append(
                    {
                        "reason": self.router.last_reason,
                        "action": "Restore eligible subscription capacity; do not enable additional spend",
                        "category": "provider",
                        "providers": self.router.last_blockers,
                    }
                )
        # Dispatch acceptance is separate from admission; expose its real receipt.
        count = self.state.connection.execute(
            "SELECT count(*) FROM attempts WHERE job_id=?", (job["id"],)
        ).fetchone()[0]
        key = self.dispatch_key(job["id"], count)
        intents = [
            dict(row)
            for row in self.state.connection.execute(
                "SELECT id,state FROM outbox WHERE id=? OR id LIKE ? ORDER BY id",
                (key, key + ":retry:%"),
            )
        ]
        dispatch = None
        if intents and job["state"] == "queued":
            latest = intents[-1]
            dispatch = dict(
                latest,
                receipt=self.state.get_meta(f"dispatch_receipt:{latest['id']}", {}),
                action="Await coordinator; replay only the same durable dispatch intent",
            )
        return {"eligible": not blockers, "blockers": blockers, "dispatch": dispatch}

    def dispatch_key(self, job_id: str, attempt_count: int) -> str:
        return self.state.get_meta(
            f"dispatch_member:{job_id}:{attempt_count}",
            f"dispatch:{job_id}:{attempt_count}",
        )

    def target_options(self, provider: str, model: str) -> dict:
        entry = next(
            (
                p
                for p in self.catalog.get("providers", [])
                if p.get("providerInstanceId") == provider
            ),
            {},
        )
        model_entry = next(
            (
                m
                for m in entry.get("models", [])
                if isinstance(m, dict) and m.get("id") == model
            ),
            {},
        )
        options = {o["id"]: o for o in model_entry.get("options") or []}
        result = {}
        if "default" in {
            choice["id"] for choice in options.get("serviceTier", {}).get("options", [])
        }:
            result["serviceTier"] = "default"
        if options.get("fastMode", {}).get("type") == "boolean":
            result["fastMode"] = False
        return result

    def local_command(
        self, envelope: Envelope, prepared_cycle: dict | None = None
    ) -> dict:
        p = envelope.payload
        kind = envelope.kind
        if kind == "status.get":
            return self.status(envelope.project_id)
        if kind == "capacity.get":
            return self.state.capacity()
        if kind == "capacity.set":
            if envelope.origin.thread_id or envelope.origin.run_id:
                raise StateError("capacity_requires_local_operator")
            return self.state.set_capacity(p["max_executors"])
        if kind.startswith("maintenance."):
            from oraculo import maintenance

            if envelope.origin.thread_id or envelope.origin.run_id:
                raise StateError("maintenance_requires_local_operator")
            if kind == "maintenance.status":
                return maintenance.status(self.state)
            if self.managed_service is None:
                from oraculo.managed_service import ManagedService

                self.managed_service = ManagedService(self)
            if (
                self.memory_operations
                or self.memory_lock.locked()
                or (self.memory_flush is not None and not self.memory_flush.done())
            ):
                raise StateError("maintenance_memory_operation_active")
            if self.managed_service.busy:
                raise StateError("maintenance_managed_operation_active")
            maintenance.check_legacy_tasks(self.config)
            if kind == "maintenance.begin":
                return maintenance.begin(self.state, p["operation_id"])
            return maintenance.end(self.state, p["operation_id"], p["token"])
        project = self.project(envelope.project_id)
        if kind == "plan.configure":
            if (envelope.origin.thread_id or envelope.origin.run_id) and (
                envelope.origin.thread_id != project["coordinator_thread_id"]
                or not envelope.origin.run_id
            ):
                raise StateError("plan_policy_requires_coordinator_or_local_operator")
            objective = self.state.get_objective(p["objective_id"])
            if not objective or objective["project_id"] != envelope.project_id:
                raise StateError("objective_project_mismatch")
            return self.operational_plan(
                self.plans.configure(objective["id"], p["max_parallel"])
            )
        if kind == "settlement.status":
            binding_id = p.get("binding_id")
            if not binding_id:
                return {"settlements": self.settlements.list(envelope.project_id)}
            binding = self.settlements.get(binding_id)
            if not binding or binding["project_id"] != envelope.project_id:
                raise StateError("settlement_project_mismatch")
            return {
                "settlement": binding,
                "events": self.settlements.events(binding_id),
            }
        if kind == "settlement.bind":
            if envelope.origin.thread_id and (
                envelope.origin.thread_id != project["coordinator_thread_id"]
                and (
                    envelope.origin.thread_id != p.get("thread_id")
                    or envelope.origin.run_id != p.get("run_id")
                )
            ):
                raise StateError("settlement_origin_mismatch")
            return self.settlements.bind(envelope.project_id, p)
        if kind in {"plan.submit", "objective.submit"} and envelope.origin.thread_id:
            source = self.state._one(
                "SELECT j.objective_id FROM attempts a JOIN jobs j ON j.id=a.job_id "
                "WHERE a.thread_id=? AND a.run_id=?",
                (envelope.origin.thread_id, envelope.origin.run_id),
            )
            if source and self.plans.get(source["objective_id"]):
                raise StateError("planned_executor_must_use_bounded_plan_extend")
        if kind == "plan.submit":
            return self.operational_plan(
                self.plans.submit(
                    envelope.project_id,
                    p["plan"],
                    p.get("dedupe_key", envelope.request_id),
                    self.config.profiles,
                )
            )
        if kind == "plan.extend":
            attempt = self.owner(envelope, executor=True)
            return self.plans.extend(attempt, p["task"], self.config.profiles)
        if kind == "task.retry":
            objective = self.state.get_objective(p["objective_id"])
            if not objective or objective["project_id"] != envelope.project_id:
                raise StateError("objective_project_mismatch")
            return self.plans.retry(
                objective["id"], p["task_id"], 1 + self.config.max_correction_rounds
            )
        if kind == "objective.submit":
            if p.get("marker") == CONTROL_MARKER or p.get("kind") == "status_event":
                raise StateError("control_event_is_not_objective")
            job_kind = p.get("kind", "implementation")
            if job_kind not in self.config.profiles:
                raise StateError("unknown_job_kind")
            validate_memory_submission(p)
            origin = p.get("origin", "human")
            evidence = p.get("evidence")
            if origin == "autonomous" and not evidence:
                raise StateError("autonomous_objective_requires_evidence")
            objective = self.state.submit_objective(
                envelope.project_id,
                p["text"],
                origin=origin,
                dedupe_key=p.get("dedupe_key", envelope.request_id),
                evidence=evidence,
                priority=100 if origin == "human" else 10,
            )
            existing = self.state.list_jobs(objective["id"])
            job = (
                existing[0]
                if existing
                else self.state.enqueue_job(objective["id"], job_kind, p)
            )
            return {
                "objective": objective["id"],
                "job": job["id"],
                "state": job["state"],
            }
        if kind == "objective.cancel":
            objective = self.state.get_objective(p["objective_id"])
            if not objective or objective["project_id"] != envelope.project_id:
                raise StateError("objective_project_mismatch")
            cancelled = self.state.cancel_objective(objective["id"])
            for job in self.state.list_jobs(objective["id"]):
                if job["kind"] == "memory" or self.state._one(
                    "SELECT id FROM attempts WHERE job_id=?", (job["id"],)
                ):
                    continue
                payload = json.loads(job["payload"])
                cycle = self.memory_cycles.begin(
                    objective["project_id"],
                    f"job:{job['id']}:1",
                    payload.get("task_spec", {}).get("brief", objective["text"]),
                    {
                        "entry": "managed",
                        "objective_id": objective["id"],
                        "job_id": job["id"],
                        "attempt_number": 1,
                    },
                    skip_read_reason="cancelled_before_start",
                )
                self.memory_cycles.finish(
                    cycle["id"],
                    "stopped",
                    {
                        "summary": "Cancelled before executor admission",
                        "limitations": ["No executor received this context"],
                    },
                )
            return cancelled
        if kind == "grant.acquire":
            if (
                envelope.origin.thread_id != project["coordinator_thread_id"]
                or not envelope.origin.run_id
            ):
                raise StateError("coordinator_identity_required")
            job = self.state.get_job(p["job_id"])
            if (
                not job
                or self.state.get_objective(job["objective_id"])["project_id"]
                != envelope.project_id
            ):
                raise StateError("job_project_mismatch")
            target = self.router.select(job["kind"], self.catalog)
            if not target:
                raise StateError("no_eligible_subscription_model")
            if job["kind"] != "memory":
                expected = (
                    self.state.connection.execute(
                        "SELECT count(*) FROM attempts WHERE job_id=?", (job["id"],)
                    ).fetchone()[0]
                    + 1
                )
                if (
                    not prepared_cycle
                    or prepared_cycle["identity"]["attempt_number"] != expected
                    or prepared_cycle["work_status"] != "running"
                ):
                    return {
                        "state": "busy",
                        "reason": "memory_preparation_changed_retry",
                    }
            attempt = self.state.acquire_grant(
                job["id"],
                target.instance_id,
                target.model,
                envelope.origin.thread_id,
                envelope.origin.run_id,
            )
            if not attempt:
                return {"state": "busy"}
            cycle_id = self.state.get_meta(
                f"work_cycle:{job['id']}:{attempt['number']}"
            )
            cycle = self.memory_cycles.get(cycle_id) if cycle_id else None
            if cycle:
                self.memory_cycles.bind(cycle["id"], {"attempt_id": attempt["id"]})
                self.state.set_meta(f"attempt_cycle:{attempt['id']}", cycle["id"])
                reference = self.state.get_meta(
                    f"work_memory_reference:{job['id']}:{attempt['number']}"
                )
                self.state.set_meta(
                    f"memory_artifact:{job['id']}", reference["artifact"]
                )
            return {
                "memory": public_cycle(cycle) if cycle else None,
                "context": self.state.get_meta(
                    f"work_memory_reference:{job['id']}:{attempt['number']}"
                )
                if cycle
                else None,
                "state": "reserved",
                "attempt": attempt,
                "job": job,
                "target": {
                    "providerInstanceId": target.instance_id,
                    "model": target.model,
                    "options": self.target_options(target.instance_id, target.model),
                },
            }
        if kind == "grant.claim":
            current = self.state.get_attempt(p["attempt_id"])
            if current:
                job = self.state.get_job(current["job_id"])
                objective = self.state.get_objective(job["objective_id"])
                if objective["status"] == "cancel_requested":
                    if (
                        objective["project_id"] != envelope.project_id
                        or envelope.origin.thread_id != current["owner_thread_id"]
                        or not envelope.origin.run_id
                    ):
                        raise StateError("cancel_binding_owner_mismatch")
                    bound = self.state.bind_cancelled_execution(
                        p["attempt_id"],
                        p["generation"],
                        p["thread_id"],
                        p["run_id"],
                        p.get("task_id"),
                    )
                    return {
                        "state": "cancel_requested",
                        "attempt": bound,
                        "effect_authorized": False,
                    }
            self.owner(
                envelope,
                executor=bool(
                    current and current["thread_id"] == envelope.origin.thread_id
                ),
            )
            bound = self.state.claim_grant(
                p["attempt_id"],
                p["generation"],
                p["thread_id"],
                p["run_id"],
                p.get("task_id"),
            )
            cycle_id = self.state.get_meta(f"attempt_cycle:{p['attempt_id']}")
            if cycle_id:
                self.memory_cycles.bind(
                    cycle_id,
                    {k: p[k] for k in ("thread_id", "run_id", "task_id") if p.get(k)},
                )
            return bound
        if kind == "execution.report":
            attempt = self.state.get_attempt(p["attempt_id"])
            if p.get("status") == "not_started":
                if (
                    not attempt
                    or envelope.origin.thread_id != attempt["owner_thread_id"]
                    or not envelope.origin.run_id
                ):
                    raise StateError("unstarted_report_owner_mismatch")
                job = self.state.get_job(attempt["job_id"])
                if (
                    self.state.get_objective(job["objective_id"])["project_id"]
                    != envelope.project_id
                ):
                    raise StateError("execution_project_mismatch")
                final = self.state.fail_unstarted(
                    attempt["id"], p["generation"], p["evidence"]
                )
                self.state.set_meta(
                    f"execution_report:{attempt['id']}", dict(p, status=final["status"])
                )
                return {"state": final["status"], "attempt_id": attempt["id"]}
            if (
                not attempt
                or attempt["generation"] != p["generation"]
                or attempt["run_id"] != p["run_id"]
            ):
                raise StateError("execution_identity_mismatch")
            job = self.state.get_job(attempt["job_id"])
            if (
                self.state.get_objective(job["objective_id"])["project_id"]
                != envelope.project_id
            ):
                raise StateError("execution_project_mismatch")
            if p["status"] not in {"succeeded", "failed", "stopped"}:
                raise StateError("invalid_execution_status")
            if not p.get("evidence"):
                raise StateError("execution_evidence_required")
            if (
                envelope.origin.thread_id
                not in {attempt["thread_id"], attempt["owner_thread_id"]}
                or not envelope.origin.run_id
            ):
                raise StateError("execution_origin_mismatch")
            if (
                envelope.origin.thread_id == attempt["thread_id"]
                and envelope.origin.run_id != attempt["run_id"]
            ):
                raise StateError("execution_origin_run_mismatch")
            self.plans.validate_report(attempt, p)
            blocker = (
                p["evidence"].get("blocker")
                if isinstance(p["evidence"], dict)
                else None
            )
            if blocker is not None and json.loads(job["payload"]).get("task_spec"):
                if (
                    p["status"] != "failed"
                    or not isinstance(blocker, dict)
                    or any(
                        not isinstance(blocker.get(key), str)
                        or not blocker[key].strip()
                        for key in ("reason", "owner", "action")
                    )
                ):
                    raise StateError("planned_blocker_requires_reason_owner_action")
            previous = self.state.get_meta(f"execution_report:{attempt['id']}")
            if previous is not None and previous != p:
                raise StateError("execution_report_is_immutable")
            self.state.set_meta(f"execution_report:{attempt['id']}", p)
            return {"state": "validating", "attempt_id": attempt["id"]}
        if kind.startswith("operation."):
            raise StateError("operation_requires_async_handler")
        raise StateError("unsupported_command")

    async def run_command(self, argv: list[str], cwd: Path) -> str:
        if (
            not isinstance(argv, list)
            or not argv
            or not all(isinstance(a, str) and a and "\0" not in a for a in argv)
        ):
            raise StateError("invalid_verification_argv")
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            async with asyncio.timeout(120):
                output = bytearray()
                while chunk := await process.stdout.read(65536):
                    output.extend(chunk)
                    if len(output) > 2 * 1024 * 1024:
                        raise StateError("verification_output_too_large")
                await process.wait()
            if process.returncode:
                raise StateError("verification_command_failed")
            return output.decode("utf-8")
        except BaseException:
            if process.returncode is None:
                os.killpg(process.pid, signal.SIGKILL)
                await process.wait()
            raise

    async def operation_command(self, envelope: Envelope) -> dict:
        from oraculo.operations import Operations

        p = envelope.payload
        project = self.project(envelope.project_id)
        operations = Operations(self.state)
        if envelope.kind == "operation.prepare":
            attempt = self.owner(envelope, executor=True)
            job = self.state.get_job(attempt["job_id"])
            spec = json.loads(job["payload"]).get("task_spec")
            if spec:
                allowed = {
                    "publication": {"push", "merge", "pr"},
                    "deployment": {"deploy"},
                }.get(spec["stage"], set())
                if p["kind"] not in allowed:
                    raise StateError("operation_outside_planned_stage")
            previous = operations.get(p["operation_id"])
            if previous:
                raise StateError("operation_already_prepared_reconcile_before_effect")
            cwd = Path(p.get("cwd", project["path"])).resolve()
            root_git = (
                await self.run_command(
                    ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
                    Path(project["path"]),
                )
            ).strip()
            work_git = (
                await self.run_command(
                    ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
                    cwd,
                )
            ).strip()
            if Path(root_git).resolve() != Path(work_git).resolve():
                raise StateError("operation_checkout_mismatch")
            checks = p.get("checks_argv", [])
            verifier = p.get("verifier_argv")
            if p["kind"] in {"merge", "deploy"} and (not checks or not verifier):
                raise StateError("delivery_requires_checks_and_destination_verifier")
            for argv in checks:
                await self.run_command(argv, cwd)
            self.owner(envelope, executor=True)
            with self.state.transaction():
                if operations.get(p["operation_id"]):
                    raise StateError(
                        "operation_already_prepared_reconcile_before_effect"
                    )
                operation = operations.prepare(
                    p["operation_id"],
                    p["attempt_id"],
                    p["kind"],
                    p["expected_state"],
                    generation=p["generation"],
                    idempotency_key=p.get("idempotency_key"),
                    checks_passed=bool(checks),
                )
                self.state.set_meta(
                    f"operation_verifier:{operation['id']}",
                    {
                        "argv": verifier,
                        "cwd": str(cwd),
                        "project_id": envelope.project_id,
                    },
                )
                if not operations.mark_dispatched(operation["id"]):
                    raise StateError("operation_dispatch_not_acquired")
            return {
                "operation_id": operation["id"],
                "state": "dispatched",
                "effect_authorized_once": True,
            }
        operation = operations.get(p["operation_id"])
        if not operation or operation["attempt_id"] != p["attempt_id"]:
            raise StateError("operation_attempt_mismatch")
        attempt = self.state.get_attempt(operation["attempt_id"])
        job = self.state.get_job(attempt["job_id"])
        if (
            self.state.get_objective(job["objective_id"])["project_id"]
            != envelope.project_id
        ):
            raise StateError("operation_project_mismatch")
        return await self.verify_operation(operation)

    async def verify_operation(self, operation: dict) -> dict:
        from oraculo.operations import Operations

        operations = Operations(self.state)
        if operation["state"] == "confirmed":
            return operation
        verifier = self.state.get_meta(f"operation_verifier:{operation['id']}")
        if not verifier or not verifier.get("argv"):
            operations.mark_uncertain(operation["id"])
            return operations.get(operation["id"])
        try:
            observed = json.loads(
                await self.run_command(verifier["argv"], Path(verifier["cwd"]))
            )
            if not isinstance(observed, dict):
                raise StateError("invalid_destination_receipt")
            return operations.report(operation["id"], observed, verified=True)
        except (ValueError, OSError, TimeoutError):
            operations.mark_uncertain(operation["id"])
            return operations.get(operation["id"])

    async def memory_command(self, envelope: Envelope) -> dict:
        if not (
            envelope.project_id == "shared" and envelope.kind.startswith("review.")
        ):
            self.project(envelope.project_id)
        async with self.memory_lock:
            if self.state.get_meta("runtime_maintenance"):
                raise StateError("maintenance_admission_closed")
            p = envelope.payload
            if envelope.kind == "review.open":
                session_id = hashlib.sha256(envelope.request_id.encode()).hexdigest()[
                    :24
                ]
                return await asyncio.to_thread(
                    self.reviews.open, envelope.project_id, session_id=session_id
                )
            if envelope.kind == "review.close":
                session_id = p["session_id"]
                if not session_id or not all(
                    c.isalnum() or c in "-_" for c in session_id
                ):
                    raise StateError("invalid_review_session")
                metadata = json.loads(
                    (
                        self.config.vault_root / "reviews" / session_id / "review.json"
                    ).read_text()
                )
                if metadata["project_id"] != envelope.project_id:
                    raise StateError("review_project_mismatch")
                return await asyncio.to_thread(self.reviews.close, session_id)
            self.owner(envelope, executor=True)
            name = p["artifact"]
            if Path(name).name != name or name in {".", ".."}:
                raise StateError("invalid_artifact_path")
            path = self.config.state_dir / "staging" / name
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, "rb") as stream:
                if os.fstat(stream.fileno()).st_uid != os.getuid():
                    raise StateError("artifact_owner_mismatch")
                raw = stream.read(self.config.memory.max_proposal_bytes + 1)
            if (
                len(raw) > self.config.memory.max_proposal_bytes
                or hashlib.sha256(raw).hexdigest() != p["sha256"]
            ):
                raise StateError("artifact_integrity_error")
            proposal = json.loads(raw)
            if proposal.get("scope") == "shared":
                attempt = self.owner(envelope, executor=True)
                job = self.state.get_job(attempt["job_id"])
                job_payload = json.loads(job["payload"])
                if (
                    job["kind"] != "memory"
                    or job_payload.get("memory_scope") != "shared"
                ):
                    raise StateError("shared_promotion_requires_scoped_memory_job")
                sanitized = proposal["sanitized_source"]
                inner = proposal["proposal"]
                if (
                    not isinstance(sanitized, str)
                    or not sanitized.strip()
                    or inner.get("project_id") != "shared"
                ):
                    raise StateError("invalid_sanitized_promotion")
                if "projects/" in sanitized or "reviews/" in sanitized:
                    raise StateError("private_reference_in_shared_source")
                source_path = await asyncio.to_thread(
                    self.publisher.snapshot,
                    "shared",
                    sanitized,
                    {"kind": "sanitized_general_learning"},
                )
                if (
                    inner.get("source_hashes", {}).get(source_path)
                    != hashlib.sha256(sanitized.encode()).hexdigest()
                ):
                    raise StateError("shared_source_hash_required")
                receipt = await asyncio.to_thread(self.publisher.publish, inner)
                self.state.audit(
                    "shared_promotion",
                    {
                        "project_id": envelope.project_id,
                        "attempt_id": attempt["id"],
                        "operation_id": inner["operation_id"],
                        "source_path": source_path,
                    },
                )
                return receipt
            if proposal["project_id"] != envelope.project_id:
                raise StateError("proposal_project_mismatch")
            return await asyncio.to_thread(self.publisher.publish, proposal)

    async def memory_io(self, method: str, *args, **kwargs):
        """Vault I/O cannot hold the event loop or use its SQLite connection."""

        def work():
            state = State(self.config.db_path, max_executors=self.config.max_executors)
            try:
                return getattr(MemoryCycles(state, self.publisher), method)(
                    *args, **kwargs
                )
            finally:
                state.close()

        return await asyncio.to_thread(work)

    async def direct_memory_command(self, envelope: Envelope) -> dict:
        def work():
            from types import SimpleNamespace

            state = State(self.config.db_path, max_executors=self.config.max_executors)

            def project(project_id):
                item = state.get_project(project_id)
                if (
                    not item
                    or item["status"] != "registered"
                    or not self.config.project_in_scope(item["path"])
                ):
                    raise StateError("project_not_registered_or_outside_scope")
                return item

            try:
                local = SimpleNamespace(
                    config=self.config,
                    state=state,
                    project=project,
                    memory_cycles=MemoryCycles(state, self.publisher),
                )
                return MemoryBridge(local).command(envelope)
            finally:
                state.close()

        if self.state.get_meta("runtime_maintenance") and envelope.kind not in {
            "memory.cycle.read",
            "memory.cycle.status",
        }:
            raise StateError("maintenance_admission_closed")
        self.memory_operations += 1
        try:
            return await asyncio.to_thread(work)
        finally:
            self.memory_operations -= 1

    async def handle(self, envelope: Envelope) -> dict:
        prepared_cycle = None
        if envelope.kind in {"managed.capabilities", "managed.read"}:
            if envelope.kind == "managed.capabilities":
                data = {
                    "protocol_version": 1,
                    "authority": "service",
                    "coverage": "cooperative",
                    "global_host_enforcement": False,
                }
            else:
                if self.managed_service is None:
                    from oraculo.managed_service import ManagedService

                    self.managed_service = ManagedService(self)
                data = self.managed_service.describe(
                    envelope.payload["action"], envelope.payload["payload"]
                )
            return {
                "request_id": envelope.request_id,
                "ok": True,
                "state": "ok",
                "data": data,
            }
        if envelope.kind.startswith("memory.cycle."):
            # Cycle identities provide durable dedupe; reads must never use inbox cache.
            data = await self.direct_memory_command(envelope)
            return {
                "request_id": envelope.request_id,
                "ok": True,
                "state": "ok",
                "data": data,
            }
        if envelope.kind == "grant.acquire":
            # Query before reservation; admission rechecks state after awaited I/O.
            project = self.project(envelope.project_id)
            if (
                envelope.origin.thread_id != project["coordinator_thread_id"]
                or not envelope.origin.run_id
            ):
                raise StateError("coordinator_identity_required")
            if self.state._one(
                "SELECT id FROM inbox WHERE id=?", (envelope.request_id,)
            ):
                # A replay must not prepare the next attempt before returning the
                # original receipt. request_begin also checks immutable payload.
                return self.state.request_begin(
                    envelope.request_id, envelope.model_dump(mode="json")
                )
            job = self.state.get_job(envelope.payload["job_id"])
            if (
                not job
                or self.state.get_objective(job["objective_id"])["project_id"]
                != envelope.project_id
            ):
                raise StateError("job_project_mismatch")
            if job["kind"] != "memory" and job["state"] in {
                "queued",
                "quota_wait",
                "auth_required",
            }:
                prepared_cycle = await self.prepare_work_memory(job)
        if envelope.kind == "memory.read":
            # Reads revalidate the live grant even for a repeated request_id;
            # cached pages must not bypass cancellation or artifact integrity.
            from oraculo.memory.transport import MemoryArtifacts

            self.project(envelope.project_id)
            attempt = self.owner(envelope, executor=True)
            job = self.state.get_job(attempt["job_id"])
            artifact = envelope.payload["artifact"]
            if artifact != self.state.get_meta(f"memory_artifact:{job['id']}"):
                raise StateError("memory_artifact_not_issued_to_job")
            data = MemoryArtifacts(self.config.state_dir).read(
                artifact,
                envelope.project_id,
                job["id"],
                envelope.payload.get("offset", 0),
            )
            cycle_id = self.state.get_meta(f"attempt_cycle:{attempt['id']}")
            if cycle_id:
                self.memory_cycles.delivered(
                    cycle_id, attempt["thread_id"], attempt["run_id"], artifact
                )
            return {
                "request_id": envelope.request_id,
                "ok": True,
                "state": "ok",
                "data": data,
            }
        payload = envelope.model_dump(mode="json")
        origin_key = None
        if envelope.origin.message_id:
            origin_key = f"command:{envelope.origin.thread_id}:{envelope.origin.message_id}:{envelope.kind}"
        memory = envelope.kind in {"memory.propose", "review.open", "review.close"}
        external = memory or envelope.kind.startswith("operation.")
        with self.state.transaction():
            previous = self.state.request_begin(
                envelope.request_id, payload, origin_key
            )
            if previous and not (previous.get("pending") and external):
                if envelope.kind == "operation.prepare" and previous.get("ok"):
                    return dict(
                        previous,
                        data=dict(
                            previous["data"],
                            effect_authorized_once=False,
                            receipt_replay=True,
                        ),
                    )
                return previous
            if not external:
                data = self.local_command(envelope, prepared_cycle)
                response = {
                    "request_id": envelope.request_id,
                    "ok": True,
                    "state": data.get("state", "ok"),
                    "data": data,
                }
                self.state.request_finish(envelope.request_id, response)
                return response
        data = (
            await self.memory_command(envelope)
            if memory
            else await self.operation_command(envelope)
        )
        response = {
            "request_id": envelope.request_id,
            "ok": True,
            "state": data.get("state", "ok"),
            "data": data,
        }
        self.state.request_finish(envelope.request_id, response)
        return response

    async def connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        request_id = "unknown"
        try:
            async with asyncio.timeout(5):
                raw = await reader.readline()
            envelope = parse_envelope(raw)
            request_id = envelope.request_id
            if envelope.kind == "managed.execute":
                if self.managed_service is None:
                    from oraculo.managed_service import ManagedService

                    self.managed_service = ManagedService(self)
                data = await self.managed_service.execute(envelope, reader, writer)
                response = {
                    "request_id": request_id,
                    "ok": True,
                    "state": "ok",
                    "data": data,
                }
            else:
                response = await self.handle(envelope)
        except Exception as exc:
            response = {
                "request_id": request_id,
                "ok": False,
                "state": "error",
                "error": {
                    "code": type(exc).__name__,
                    "message": str(exc),
                    "retryable": False,
                },
            }
        try:
            writer.write(canonical_json(response).encode() + b"\n")
            await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass

    def prompt(self, name: str) -> str:
        path = resources_root() / "prompts" / f"{name}.md"
        return path.read_text().replace(
            "{{CLI}}",
            shlex.join(
                [sys.executable, "-m", "oraculo", "--config", str(self.config_path)]
            ),
        )

    async def ensure_coordinator(self, client: Any, project: dict) -> str | None:
        if project["coordinator_thread_id"]:
            return project["coordinator_thread_id"]
        key = f"coordinator_launch:{project['id']}"
        intent = self.state.get_meta(key)
        if intent:
            matches = await client.list_threads(
                project["id"], title_contains=intent["marker"]
            )
            matches = [
                t
                for t in matches
                if t["title"] == intent["marker"] and not t.get("parentThreadId")
            ]
            if len(matches) == 1:
                self.state.set_coordinator(project["id"], matches[0]["threadId"])
                return matches[0]["threadId"]
            self.health = "uncertain_coordinator_launch"
            return None
        target = self.router.select("architecture", self.catalog)
        if not target:
            self.health = "blocked_eligibility"
            return None
        marker = f"Oráculo {project['id']} {uuid.uuid4().hex[:12]}"
        self.state.set_meta(
            key, {"marker": marker, "state": "prepared", "created_at": time.time()}
        )
        try:
            result = await client.launch(
                project_id=project["id"],
                title=marker,
                message=self.prompt("coordinator")
                + "\n"
                + canonical_json(
                    {
                        "marker": CONTROL_MARKER,
                        "kind": "bootstrap",
                        "project_id": project["id"],
                    }
                ),
                model_selection={
                    "instanceId": target.instance_id,
                    "model": target.model,
                    "options": self.target_options(target.instance_id, target.model),
                },
                workspace_strategy={"type": "root"},
            )
        except Exception:
            self.state.set_meta(
                key, {"marker": marker, "state": "uncertain", "created_at": time.time()}
            )
            raise
        self.state.set_coordinator(project["id"], result["threadId"])
        self.state.set_meta(
            key,
            {"marker": marker, "state": "confirmed", "thread_id": result["threadId"]},
        )
        return result["threadId"]

    def job_ready(self, job: dict) -> bool:
        return not self.state._one(
            "SELECT d.job_id FROM dependencies d JOIN jobs j ON d.depends_on=j.id WHERE d.job_id=? AND j.state!='succeeded' LIMIT 1",
            (job["id"],),
        )

    async def dispatch(self, client: Any) -> None:
        if not self.state.capacity()["available"]:
            return
        candidates = [j for j in self.state.list_jobs() if j["state"] == "queued"]
        candidates.sort(
            key=lambda j: (
                -self.state.get_objective(j["objective_id"])["priority"],
                j["created_at"],
            )
        )
        for job in candidates:
            objective = self.state.get_objective(job["objective_id"])
            if objective["status"] in {"cancel_requested", "stopped"}:
                continue
            if self.state.admission_blockers(job):
                continue
            if not self.router.select(job["kind"], self.catalog):
                self.health = "blocked_eligibility"
                continue
            project = self.project(objective["project_id"])
            coordinator = await self.ensure_coordinator(client, project)
            if not coordinator:
                return
            attempt_count = self.state.connection.execute(
                "SELECT count(*) FROM attempts WHERE job_id=?", (job["id"],)
            ).fetchone()[0]
            base_key = self.dispatch_key(job["id"], attempt_count)
            key = base_key
            existing = self.state._one("SELECT * FROM outbox WHERE id=?", (key,))
            if existing:
                dispatch_number = 2
                while dispatch_number <= self.config.max_dispatch_attempts:
                    next_key = f"{base_key}:retry:{dispatch_number}"
                    following = self.state._one(
                        "SELECT * FROM outbox WHERE id=?", (next_key,)
                    )
                    if not following:
                        break
                    key, existing = next_key, following
                    dispatch_number += 1
                if existing["state"] != "pending":
                    if existing["state"] != "sent":
                        continue
                    receipt = self.state.get_meta(f"dispatch_receipt:{key}", {})
                    run_id = receipt.get("run_id")
                    if not isinstance(run_id, str) or not run_id:
                        self.health = "dispatch_identity_unknown"
                        continue
                    persisted = json.loads(existing["payload"])
                    from oraculo.t3.contracts import dispatch_retry_safe

                    snapshot = await client.read_thread(persisted["thread_id"])
                    if snapshot["thread"]["threadId"] != persisted[
                        "thread_id"
                    ] or not dispatch_retry_safe(snapshot, run_id):
                        self.health = "awaiting_coordinator"
                        continue
                    if not self.dispatch_current(job, attempt_count):
                        continue
                    if dispatch_number > self.config.max_dispatch_attempts:
                        self.health = "dispatch_attempts_exhausted"
                        continue
                    key = f"{base_key}:retry:{dispatch_number}"
                    self.state.enqueue_outbox(key, persisted, objective["id"])
                    existing = self.state._one(
                        "SELECT * FROM outbox WHERE id=?", (key,)
                    )
                if self.dispatch_current(job, attempt_count):
                    await self.send_dispatch(client, existing)
                    return
                continue
            payload = json.loads(job["payload"])
            context: dict = {}
            if job["kind"] == "memory":
                context = await self.memory_context(project, payload)
            else:
                cycle = await self.prepare_work_memory(job)
                context = self.state.get_meta(
                    f"work_memory_reference:{job['id']}:{cycle['identity']['attempt_number']}"
                )
            if not self.dispatch_current(job, attempt_count):
                continue
            body = {
                "marker": CONTROL_MARKER,
                "kind": "dispatch",
                "job_id": job["id"],
                "project_id": project["id"],
                "payload": payload,
                "context": context,
                "executor_instructions": self.prompt("executor"),
                "objective_id": objective["id"],
                "task_context": self.task_context(job),
                "memory": public_cycle(cycle) if job["kind"] != "memory" else None,
            }
            if body["task_context"]:
                # Existing coordinator contexts can predate this protocol version.
                body["coordinator_instructions"] = self.prompt("coordinator")
                body["parallel_dispatches"] = self.parallel_dispatches(job, candidates)
            if job["kind"] == "memory":
                body["memory_instructions"] = self.prompt("memory")
                from oraculo.memory.transport import MemoryArtifacts, memory_message

                message, reference = memory_message(
                    body, MemoryArtifacts(self.config.state_dir)
                )
                if reference:
                    self.state.set_meta(
                        f"memory_artifact:{job['id']}", reference["artifact"]
                    )
            else:
                from oraculo.memory.transport import MAX_MESSAGE_UNITS, utf16_units

                # An oversized companion must not block an otherwise bounded task.
                companions = body.pop("parallel_dispatches", [])
                if companions:
                    body["parallel_dispatches"] = []
                    for companion in companions:
                        body["parallel_dispatches"].append(companion)
                        if utf16_units(canonical_json(body)) > MAX_MESSAGE_UNITS:
                            body["parallel_dispatches"].pop()
                message = canonical_json(body)
                if utf16_units(message) > MAX_MESSAGE_UNITS:
                    # This is a new intent, known not to have been sent. Persist a
                    # useful blocker instead of treating it as an uncertain replay.
                    with self.state.transaction():
                        self.state.connection.execute(
                            "UPDATE jobs SET state='blocked' WHERE id=?", (job["id"],)
                        )
                        self.state.set_meta(
                            f"blocker:{job['id']}",
                            {
                                "reason": "task_dispatch_too_large",
                                "owner": payload.get("task_spec", {}).get(
                                    "owner", "objective owner"
                                ),
                                "action": (
                                    "Cancel and resubmit a bounded plan using source "
                                    "references and concise criterion/dependency receipts"
                                ),
                            },
                        )
                        self.state.connection.execute(
                            "UPDATE objectives SET status='blocked' WHERE id=?",
                            (objective["id"],),
                        )
                    self.health = "task_dispatch_too_large"
                    continue
            with self.state.transaction():
                self.state.enqueue_outbox(
                    key, {"thread_id": coordinator, "message": message}, objective["id"]
                )
                for offered in [body, *body.get("parallel_dispatches", [])]:
                    offered_count = self.state.connection.execute(
                        "SELECT count(*) FROM attempts WHERE job_id=?",
                        (offered["job_id"],),
                    ).fetchone()[0]
                    self.state.set_meta(
                        f"dispatch_member:{offered['job_id']}:{offered_count}", key
                    )
            await self.send_dispatch(
                client, self.state._one("SELECT * FROM outbox WHERE id=?", (key,))
            )
            return

    async def prepare_work_memory(self, job: dict, number: int | None = None) -> dict:
        """A new attempt gets a new snapshot; dispatch replay preserves its version."""
        objective = self.state.get_objective(job["objective_id"])
        number = number or (
            self.state.connection.execute(
                "SELECT count(*) FROM attempts WHERE job_id=?", (job["id"],)
            ).fetchone()[0]
            + 1
        )
        payload = json.loads(job["payload"])
        cycle = await self.memory_io(
            "begin",
            objective["project_id"],
            f"job:{job['id']}:{number}",
            payload.get("task_spec", {}).get("brief", objective["text"]),
            {
                "entry": "managed",
                "objective_id": objective["id"],
                "job_id": job["id"],
                "attempt_number": number,
            },
        )
        from oraculo.memory.transport import MemoryArtifacts

        reference = MemoryArtifacts(self.config.state_dir).create(
            {
                "project_id": objective["project_id"],
                "job_id": job["id"],
                "payload": {},
                "context": artifact_context(cycle),
            }
        )
        self.state.set_meta(f"work_cycle:{job['id']}:{number}", cycle["id"])
        self.state.set_meta(f"work_memory_reference:{job['id']}:{number}", reference)
        return cycle

    def dispatch_current(self, job: dict, attempt_count: int) -> bool:
        """Recheck admission after remote reads or context preparation yielded."""
        current = self.state.get_job(job["id"])
        objective = self.state.get_objective(job["objective_id"])
        count = self.state.connection.execute(
            "SELECT count(*) FROM attempts WHERE job_id=?", (job["id"],)
        ).fetchone()[0]
        return (
            not self.state.admission_blockers(current)
            and current["state"] == "queued"
            and objective["status"] not in {"cancel_requested", "stopped"}
            and not self.state._scope_conflicts(current, objective["project_id"])
            and count == attempt_count
        )

    def parallel_dispatches(self, primary: dict, candidates: list[dict]) -> list[dict]:
        """Offer bounded companions; only acquire_grant can authorize each child."""
        capacity = self.state.plan_capacity(primary["objective_id"])
        if not capacity["requested"] or capacity["requested"] < 2:
            return []
        remaining = max(0, capacity["available"] - 1)
        result = []
        project_id = self.state.get_objective(primary["objective_id"])["project_id"]
        offered = [dict(primary, project_id=project_id)]
        primary_target = self.router.select(primary["kind"], self.catalog)
        allocations = {primary_target.provider: 1} if primary_target else {}
        for job in candidates:
            if len(result) >= remaining:
                break
            if (
                job["id"] == primary["id"]
                or job["objective_id"] != primary["objective_id"]
                or job["kind"] == "memory"
                or not self.admission(job)["eligible"]
                or self.state._scope_conflicts(job, project_id, active=offered)
            ):
                continue
            count = self.state.connection.execute(
                "SELECT count(*) FROM attempts WHERE job_id=?", (job["id"],)
            ).fetchone()[0]
            if self.state._one(
                "SELECT id FROM outbox WHERE id=?",
                (self.dispatch_key(job["id"], count),),
            ):
                # An accepted/uncertain offer retains its original outbox identity.
                continue
            target = self.router.select(job["kind"], self.catalog, reserved=allocations)
            if (
                not target
                or allocations.get(target.provider, 0)
                >= self.state.provider_capacity(target.provider)["available"]
            ):
                continue
            allocations[target.provider] = allocations.get(target.provider, 0) + 1
            offered.append(dict(job, project_id=project_id))
            result.append(
                {
                    "job_id": job["id"],
                    "payload": json.loads(job["payload"]),
                    "task_context": self.task_context(job),
                }
            )
        return result

    def task_context(self, job: dict) -> dict:
        plan = self.plans.get(job["objective_id"])
        if not plan:
            return {}
        dependencies = self.state.connection.execute(
            "SELECT depends_on FROM dependencies WHERE job_id=?", (job["id"],)
        ).fetchall()
        proofs = []
        for dep in dependencies:
            attempt = self.state._one(
                "SELECT * FROM attempts WHERE job_id=? ORDER BY number DESC LIMIT 1",
                (dep["depends_on"],),
            )
            if attempt:
                report = self.state.get_meta(f"execution_report:{attempt['id']}", {})
                proofs.append(
                    {
                        "job_id": dep["depends_on"],
                        "thread_id": attempt["thread_id"],
                        "run_id": attempt["run_id"],
                        "task_id": attempt["task_id"],
                        "evidence": report.get("evidence"),
                    }
                )
        return {
            "objective": plan["spec"]["text"],
            "dependency_evidence": proofs,
            "isolation": "shared",
        }

    async def send_dispatch(self, client: Any, row: dict) -> None:
        from oraculo.memory.transport import MAX_MESSAGE_UNITS, utf16_units

        payload = json.loads(row["payload"])
        if utf16_units(payload["message"]) > MAX_MESSAGE_UNITS:
            # A legacy pending row may already have been accepted remotely.
            # Never replace its bytes under the same idempotency key.
            self.health = "legacy_dispatch_oversize_requires_reconciliation"
            return
        receipt = await client.send(
            payload["thread_id"], payload["message"], row["id"], mode="queue"
        )
        with self.state.transaction():
            self.state.set_meta(
                f"dispatch_receipt:{row['id']}",
                {"run_id": receipt.get("runId") if isinstance(receipt, dict) else None},
            )
            self.state.connection.execute(
                "UPDATE outbox SET state='sent' WHERE id=? AND state='pending'",
                (row["id"],),
            )

    async def memory_context(self, project: dict, payload: dict) -> dict:
        async with self.memory_lock:
            operation = payload.get("memory_operation", "query")
            result = await asyncio.to_thread(
                Maintenance(self.publisher).request,
                project["id"],
                operation,
                payload.get("query", ""),
            )
            source = payload.get("source_path")
            if operation == "ingest" and source:
                path = Path(source)
                if (
                    not path.resolve().is_relative_to(Path(project["path"]).resolve())
                    or path.is_symlink()
                ):
                    raise StateError("source_outside_project")
                raw = path.read_bytes()
                if len(raw) > self.config.memory.max_proposal_bytes:
                    raise StateError("source_too_large")
                result["raw_path"] = await asyncio.to_thread(
                    self.publisher.snapshot,
                    project["id"],
                    raw.decode("utf-8"),
                    {
                        "source_path": str(path),
                        "project_id": project["id"],
                        "captured_at": time.time(),
                    },
                )
                result["source_content"] = raw.decode("utf-8")
            if operation == "ingest" and payload.get("execution_evidence"):
                content = canonical_json(payload["execution_evidence"])
                result["raw_path"] = await asyncio.to_thread(
                    self.publisher.snapshot,
                    project["id"],
                    content,
                    payload.get("provenance", {}),
                )
                result["source_content"] = content
            scope = (
                "shared" if payload.get("memory_scope") == "shared" else project["id"]
            )
            result["memory_scope"] = scope
            result["bundle"] = await asyncio.to_thread(
                self.consistent_bundle, scope, payload.get("query", "")
            )
            return result

    def consistent_bundle(self, project_id: str, query: str) -> dict:
        with self.publisher.locked():
            self.publisher._recover()
            result = Context(self.config.vault_root).bundle(project_id, query)
            result["base_commit"] = self.publisher.git.head()
            for document in result["documents"]:
                document["sha256"] = hashlib.sha256(
                    document["content"].encode()
                ).hexdigest()
            return result

    async def tick(self, client: Any) -> None:
        from oraculo.reconcile import Reconciler

        self.catalog = await client.capabilities()
        projects = await client.list_projects()
        registered = set()
        for project in projects:
            if not project.get("deletedAt") and self.config.project_in_scope(
                project["workspaceRoot"]
            ):
                registered.add(project["id"])
                self.state.register_project(project["id"], project["workspaceRoot"])
        for project in self.state.list_projects():
            if project["id"] not in registered:
                self.state.register_project(
                    project["id"], project["path"], "unregistered"
                )
        operations = [
            dict(row)
            for row in self.state.connection.execute(
                "SELECT * FROM operations WHERE state IN ('dispatched','uncertain')"
            )
        ]
        for operation in operations:
            await self.verify_operation(operation)
        await Reconciler(self.state, client).reconcile()
        self.after_execution()
        self.start_memory_flush()
        await self.poll_coordinators(client)
        await self.reconcile_settlements(client)
        await self.flush_status(client)
        await self.reconcile_managed_tasks(client)
        self.health = "online"
        self.discover()
        await self.dispatch(client)

    async def reconcile_managed_tasks(self, client: Any) -> None:
        """Bounded background recovery; slow Git/GitHub never blocks admission."""
        if self.managed_flush is not None and not self.managed_flush.done():
            return
        if self.managed_flush is not None:
            try:
                self.managed_flush.result()
            except Exception as exc:
                self.log.warning("managed_reconcile_failed:%s", type(exc).__name__)
        path = self.config.state_dir / "managed-tasks.sqlite3"
        if not path.exists() or self.state.get_meta("runtime_maintenance"):
            return
        if self.managed_service is None:
            from oraculo.managed_service import ManagedService

            try:
                self.managed_service = ManagedService(self)
            except BlockingIOError:
                return
        if self.managed_service.busy:
            return
        facade = self.managed_service
        facade.busy = True
        loop = asyncio.get_running_loop()

        class HostProxy:
            async def call(self, name, arguments):
                future = asyncio.run_coroutine_threadsafe(
                    client.call(name, arguments), loop
                )
                return await asyncio.wrap_future(future)

        def reconcile_batch():
            from oraculo.managed_tasks import ManagedTasks
            from oraculo.publication import GitHubPublisher
            from oraculo.task_store import TaskStore
            from oraculo.task_transport import SocketMemory

            store = admission = None
            try:
                # The facade retains the process lock. A worker-local connection
                # keeps slow Git operations off the service loop; busy serializes
                # it with caller-bound mutations under the same authority.
                store = TaskStore(path)
                store.authority_owned = True
                admission = State(
                    self.config.db_path,
                    max_executors=self.config.max_executors,
                    provider_limits=self.state.provider_limits,
                )
                adapter = ManagedTasks(
                    self.config,
                    store,
                    HostProxy(),
                    SocketMemory(self.config),
                    GitHubPublisher(),
                    admission=admission,
                )
                cursor = store.state.get_meta("managed_reconcile_cursor", 0)
                rows = store.db.execute(
                    "SELECT rowid,id FROM managed_tasks WHERE rowid>? ORDER BY rowid LIMIT 16",
                    (cursor,),
                ).fetchall()
                if not rows:
                    store.state.set_meta("managed_reconcile_cursor", 0)

                async def run_batch():
                    for row in rows:
                        try:
                            await adapter.reconcile(row["id"])
                        except Exception as exc:
                            self.log.warning(
                                "managed_reconcile_failed:%s", type(exc).__name__
                            )

                asyncio.run(run_batch())
                if rows:
                    store.state.set_meta("managed_reconcile_cursor", rows[-1]["rowid"])
            finally:
                if store:
                    store.close()
                if admission:
                    admission.close()

        async def serialized_batch():
            try:
                await asyncio.to_thread(reconcile_batch)
            finally:
                facade.busy = False

        self.managed_flush = asyncio.create_task(serialized_batch())

    def start_memory_flush(self) -> None:
        if self.state.get_meta("runtime_maintenance"):
            return
        # One bounded batch at a time, independent of dispatch/reconciliation.
        if self.memory_flush is not None and not self.memory_flush.done():
            return
        if self.memory_flush is not None:
            try:
                self.memory_flush.result()
            except Exception as exc:
                self.log.warning("memory_flush_failed:%s", type(exc).__name__)
        if not self.memory_cycles.pending():
            return
        self.memory_flush = asyncio.create_task(
            self.memory_io("publish_pending", limit=4)
        )

    async def reconcile_settlements(self, client: Any) -> None:
        engine = Settlements(self.state, self.settlements, self.plans)
        for binding in self.settlements.list():
            if binding["state"] == "reopened":
                continue
            project = self.state.get_project(binding["project_id"])
            if (
                not project
                or project["status"] != "registered"
                or not self.config.project_in_scope(project["path"])
            ):
                self.settlements.record(
                    binding["id"],
                    "blocked",
                    error="project_not_registered_or_outside_scope",
                )
                continue
            await engine.reconcile(client, binding["id"])

    def after_execution(self) -> None:
        # A dispatch may be cancelled before any grant/child exists. Preserve that
        # history without inventing an executor identity or scheduling more work.
        for cycle in self.memory_cycles.list():
            identity = cycle["identity"]
            if (
                identity.get("entry") != "managed"
                or identity.get("attempt_id")
                or cycle["work_status"] != "running"
            ):
                continue
            objective = self.state.get_objective(identity["objective_id"])
            if objective["status"] in {"cancel_requested", "stopped"}:
                self.memory_cycles.finish(
                    cycle["id"],
                    "stopped",
                    {
                        "summary": "Cancelled before executor admission",
                        "limitations": ["No executor received this context"],
                    },
                )
        attempts = [
            dict(row)
            for row in self.state.connection.execute(
                "SELECT * FROM attempts WHERE status IN ('succeeded','failed','stopped')"
            )
        ]
        for attempt in attempts:
            key = f"postprocessed:{attempt['id']}"
            if self.state.get_meta(key):
                continue
            job = self.state.get_job(attempt["job_id"])
            objective = self.state.get_objective(job["objective_id"])
            report = self.state.get_meta(f"execution_report:{attempt['id']}", {})
            evidence = report.get("evidence", {})
            cycle_id = self.state.get_meta(f"attempt_cycle:{attempt['id']}")
            if cycle_id:
                self.memory_cycles.bind(
                    cycle_id,
                    {
                        k: attempt[k]
                        for k in ("thread_id", "run_id", "task_id")
                        if attempt.get(k)
                    },
                )
                self.memory_cycles.finish(
                    cycle_id,
                    attempt["status"],
                    evidence if isinstance(evidence, dict) else {"evidence": evidence},
                )
            with self.state.transaction():
                quota = (
                    isinstance(evidence, dict) and evidence.get("error_kind") == "quota"
                )
                blocker = (
                    evidence.get("blocker") if isinstance(evidence, dict) else None
                )
                planned = self.plans.get(objective["id"]) is not None
                retry = (
                    not blocker
                    and attempt["status"] == "failed"
                    and attempt["number"] <= self.config.max_correction_rounds
                    and objective["status"] not in {"cancel_requested", "stopped"}
                )
                if quota:
                    self.router.mark_quota(attempt["provider"])
                if retry:
                    payload = json.loads(job["payload"])
                    payload["previous_failure"] = evidence
                    self.state.connection.execute(
                        "UPDATE jobs SET state='queued',payload=? WHERE id=?",
                        (canonical_json(payload), job["id"]),
                    )
                    self.state.connection.execute(
                        "UPDATE objectives SET status='queued' WHERE id=?",
                        (objective["id"],),
                    )
                elif (
                    not cycle_id
                    and not planned
                    and job["kind"] not in {"memory", "discovery"}
                    and objective["status"] not in {"cancel_requested", "stopped"}
                ):
                    self.state.enqueue_job(
                        objective["id"],
                        "memory",
                        {
                            "memory_operation": "ingest",
                            "query": "Sintetizar decisão, resultado, status histórico e aprendizado com referências.",
                            "execution_evidence": {
                                "status": attempt["status"],
                                "objective": objective["text"],
                                "evidence": evidence,
                            },
                            "provenance": {
                                "project_id": objective["project_id"],
                                "thread_id": attempt["thread_id"],
                                "run_id": attempt["run_id"],
                                "task_id": attempt["task_id"],
                            },
                        },
                    )
                    self.state.connection.execute(
                        "UPDATE objectives SET status='queued' WHERE id=?",
                        (objective["id"],),
                    )
                if planned:
                    if blocker and attempt["status"] == "failed":
                        self.state.set_meta(f"blocker:{job['id']}", blocker)
                        self.state.connection.execute(
                            "UPDATE jobs SET state='blocked' WHERE id=?", (job["id"],)
                        )
                    self.plans.describe(objective["id"])
                project = self.state.get_project(objective["project_id"])
                if project["coordinator_thread_id"]:
                    message = canonical_json(
                        {
                            "marker": CONTROL_MARKER,
                            "kind": "status_event",
                            "objective_id": objective["id"],
                            "attempt_id": attempt["id"],
                            "status": attempt["status"],
                            "retry_queued": retry,
                            "memory": public_cycle(self.memory_cycles.get(cycle_id))
                            if cycle_id
                            else None,
                        }
                    )
                    self.state.enqueue_outbox(
                        f"status:{attempt['id']}",
                        {
                            "thread_id": project["coordinator_thread_id"],
                            "message": message,
                            "project_id": project["id"],
                        },
                    )
                self.state.set_meta(key, True)

    async def poll_coordinators(self, client: Any) -> None:
        for project in self.state.list_projects():
            thread_id = project["coordinator_thread_id"]
            if not thread_id or project["status"] != "registered":
                continue
            cursor = self.state._one(
                "SELECT cursor FROM thread_cursors WHERE thread_id=?", (thread_id,)
            )
            after = json.loads(cursor["cursor"]) if cursor else None
            for _ in range(3):
                snapshot = await client.read_thread(thread_id, after_position=after)
                items = snapshot.get("items", [])
                next_position = snapshot.get("nextPosition")
                if next_position is not None:
                    self.state.ingest_items(thread_id, items, next_position)
                if not snapshot.get("hasMore") or next_position == after:
                    break
                after = next_position

    async def flush_status(self, client: Any) -> None:
        rows = self.state.connection.execute(
            "SELECT * FROM outbox WHERE state='pending' AND id LIKE 'status:%'"
        ).fetchall()
        for row in rows:
            payload = json.loads(row["payload"])
            await client.send(
                payload["thread_id"], payload["message"], row["id"], mode="queue"
            )
            self.state.connection.execute(
                "UPDATE outbox SET state='sent' WHERE id=?", (row["id"],)
            )

    def discover(self) -> None:
        if self.state.lease() or any(
            j["state"] in {"queued", "admitted", "running"}
            for j in self.state.list_jobs()
        ):
            return
        now = time.time()
        last = self.state.get_meta("last_discovery_at", 0)
        if (
            now - last < self.config.proactive_interval_seconds
            or not self.router.select("discovery", self.catalog)
        ):
            return
        projects = [
            p for p in self.state.list_projects() if p["status"] == "registered"
        ]
        if not projects:
            return
        cursor = self.state.get_meta("discovery_cursor", 0)
        project = projects[cursor % len(projects)]
        with self.state.transaction():
            objective = self.state.submit_objective(
                project["id"],
                "Inspecionar evidências e propor no máximo uma melhoria concreta sem duplicar objetivos existentes.",
                origin="autonomous",
                dedupe_key=f"discovery:{project['id']}:{int(now)}",
                evidence={"scheduled_inspection": True},
                priority=1,
            )
            self.state.enqueue_job(
                objective["id"],
                "discovery",
                {
                    "instruction": "Inspecione o projeto. Se houver melhoria concreta, envie objective.submit com origin=autonomous, evidence e dedupe_key estável projeto+tipo+alvo+hash da evidência. Sem melhoria, reporte sucesso sem criar objetivo.",
                    "existing_objectives": [
                        {"id": o["id"], "text": o["text"], "status": o["status"]}
                        for o in self.state.list_objectives()
                        if o["project_id"] == project["id"]
                    ][-50:],
                },
            )
            self.state.set_meta("last_discovery_at", now)
            self.state.set_meta("discovery_cursor", cursor + 1)

    async def loop(self) -> None:
        from oraculo.t3.auth import build_auth
        from oraculo.t3.client import T3Client
        from oraculo.t3.contracts import ContractError

        while not self.stop.is_set():
            self.start_memory_flush()
            try:
                async with T3Client(
                    self.config.t3_mcp_url, build_auth(self.config.t3_mcp_url)
                ) as client:
                    while not self.stop.is_set():
                        await self.tick(client)
                        await self.pause()
            except Exception as exc:
                self.health = (
                    "auth_required"
                    if "auth" in type(exc).__name__.lower()
                    else "disconnected"
                )
                self.log.warning(
                    canonical_json(
                        {
                            "event": "connection_unavailable",
                            "error_type": type(exc).__name__,
                            "state": self.health,
                            **(
                                {"tool": exc.tool, "direction": exc.direction}
                                if isinstance(exc, ContractError) and exc.tool
                                else {}
                            ),
                        }
                    )
                )
                await self.pause()

    async def pause(self) -> None:
        timeout = (
            self.config.poll_active_seconds
            if self.state.lease()
            else self.config.poll_idle_seconds
        )
        try:
            await asyncio.wait_for(self.stop.wait(), timeout)
        except TimeoutError:
            pass


async def serve(config: Config, config_path: Path) -> None:
    validate_startup_config(config, config_path)
    with Singleton(config.state_dir):
        service = Service(config, config_path)
        log_dir = Path.home() / "Library/Logs/Oraculo"
        log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        handler = logging.handlers.RotatingFileHandler(
            log_dir / "service.jsonl", maxBytes=10 * 1024 * 1024, backupCount=5
        )
        service.log.addHandler(handler)
        service.log.setLevel(logging.INFO)
        try:
            await asyncio.to_thread(service.publisher.initialize)
            await asyncio.to_thread(service.publisher.recover)
            config.socket_path.unlink(missing_ok=True)
            server = await asyncio.start_unix_server(
                service.connection,
                path=str(config.socket_path),
                limit=MAX_ENVELOPE_BYTES + 1,
            )
            config.socket_path.chmod(0o600)
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(sig, service.stop.set)
            worker = asyncio.create_task(service.loop())
            async with server:
                await service.stop.wait()
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        finally:
            if service.managed_flush is not None:
                # Keep writer ownership until the worker has persisted its final
                # observation; do not release the lock under a live thread.
                await asyncio.gather(service.managed_flush, return_exceptions=True)
            if service.memory_flush is not None:
                await asyncio.gather(service.memory_flush, return_exceptions=True)
            config.socket_path.unlink(missing_ok=True)
            if service.managed_service is not None:
                service.managed_service.close()
            service.state.close()
            service.log.removeHandler(handler)
            handler.close()
