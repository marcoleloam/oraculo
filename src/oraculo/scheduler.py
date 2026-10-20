from __future__ import annotations

import time
from typing import Any

from oraculo.protocol import content_hash
from oraculo.routing import Router
from oraculo.state import State


class Scheduler:
    def __init__(self, state: State, router: Router) -> None:
        self.state = state
        self.router = router

    def admit_next(
        self,
        owner_thread_id: str,
        owner_run_id: str,
        catalog: dict[str, Any] | list[dict[str, Any]],
        project_id: str | None = None,
    ) -> dict[str, Any] | None:
        if len(self.state.leases()) >= self.state.max_executors:
            return None
        rows = self.state.connection.execute(
            "SELECT j.*,o.priority,o.origin,o.project_id FROM jobs j JOIN objectives o ON j.objective_id=o.id WHERE j.state IN ('queued','quota_wait','auth_required') AND o.status NOT IN ('cancel_requested','stopped') AND j.next_attempt_at<=? ORDER BY (o.priority + CASE WHEN j.kind='memory' THEN CAST((?-j.created_at)/3600 AS INTEGER) ELSE 0 END) DESC,j.created_at",
            (time.time(), time.time()),
        ).fetchall()
        for row in rows:
            if project_id and row["project_id"] != project_id:
                continue
            target = self.router.select(row["kind"], catalog)
            if target is None:
                continue
            grant = self.state.acquire_grant(
                row["id"], target.provider, target.model, owner_thread_id, owner_run_id
            )
            if grant:
                return grant
        return None

    def propose_improvement(
        self,
        project_id: str,
        improvement_type: str,
        target: str,
        evidence: str,
        text: str,
    ) -> dict[str, Any]:
        normalized = " ".join(evidence.split())
        key = content_hash([project_id, improvement_type, target, normalized])
        with self.state.transaction():
            objective = self.state.submit_objective(
                project_id,
                text,
                "autonomous",
                key,
                {"type": improvement_type, "target": target, "evidence": normalized},
                10,
            )
            if not self.state.list_jobs(objective["id"]):
                self.state.enqueue_job(
                    objective["id"],
                    "implementation",
                    {"text": text, "evidence": normalized},
                )
            return objective

    def schedule_discovery(self, now: float | None = None) -> dict[str, Any] | None:
        timestamp = time.time() if now is None else now
        if self.state.lease() or timestamp < self.state.get_meta("next_discovery", 0):
            return None
        projects = [
            p for p in self.state.list_projects() if p["status"] == "registered"
        ]
        if not projects:
            return None
        with self.state.transaction():
            index = self.state.get_meta("discovery_index", 0) % len(projects)
            project = projects[index]
            outstanding = self.state._one(
                "SELECT j.id FROM jobs j JOIN objectives o ON o.id=j.objective_id WHERE j.kind='discovery' AND j.state NOT IN ('succeeded','failed','stopped')"
            )
            if outstanding:
                return None
            window = int(timestamp // self.router.config.proactive_interval_seconds)
            objective = self.state.submit_objective(
                project["id"],
                "Identificar uma melhoria concreta com evidência",
                "autonomous",
                f"discovery:{project['id']}:{window}",
                priority=0,
            )
            job = self.state.enqueue_job(
                objective["id"], "discovery", {"max_proposals": 1}
            )
            self.state.set_meta("discovery_index", index + 1)
            self.state.set_meta(
                "next_discovery",
                timestamp + self.router.config.proactive_interval_seconds,
            )
            return job
