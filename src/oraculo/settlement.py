"""Verified task closure using T3's existing PR tracking and public lifecycle.

No host watcher lives here. Reads use T3's tracked host state in the ordinary
service reconciliation cycle. A durable request is never automatically replayed.
"""

from __future__ import annotations

from typing import Any

from oraculo.plans import Plans
from oraculo.settlement_store import SettlementStore
from oraculo.state import State, StateError


def _pr_key(pr: dict) -> tuple:
    return pr.get("host"), pr.get("repository"), pr.get("number")


def verify_pull_requests(expected: list[dict], observed: dict) -> list[dict]:
    rows = observed.get("pullRequests")
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        raise StateError("pull_request_observation_missing")
    keys = [_pr_key(r) for r in rows]
    if len(set(keys)) != len(keys) or set(keys) != {_pr_key(r) for r in expected}:
        raise StateError("linked_pull_request_set_mismatch")
    for pr in expected:
        actual = rows[keys.index(_pr_key(pr))]
        if actual.get("state") != "merged" or actual.get("isDraft") is not False:
            raise StateError("pull_request_not_verified_merged")
        if actual.get("baseBranch") != pr["base_branch"]:
            raise StateError("pull_request_target_mismatch")
        for field, remote in (("head_sha", "headSha"), ("merge_sha", "mergeCommitSha")):
            if pr.get(field) and actual.get(remote) != pr[field]:
                raise StateError("pull_request_commit_unverified:" + field)
    return rows


class Settlements:
    def __init__(self, state: State, store: SettlementStore, plans: Plans):
        self.state = state
        self.store = store
        self.plans = plans

    def local_gate(self, binding: dict) -> dict:
        objective = self.state.get_objective(binding["objective_id"])
        project = self.state.get_project(binding["project_id"])
        if not project or project["status"] != "registered":
            raise StateError("settlement_project_unregistered")
        if any(
            p["coordinator_thread_id"] == binding["thread_id"]
            for p in self.state.list_projects()
        ):
            raise StateError("permanent_coordinator_cannot_settle")
        if not objective or objective["project_id"] != binding["project_id"]:
            raise StateError("settlement_objective_identity_mismatch")
        plan = self.plans.describe(objective["id"])
        objective = self.state.get_objective(objective["id"])
        # Enrollment is prospective: old objectives have no cycle rows.
        if self.state._one(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='memory_cycles'"
        ):
            from oraculo.memory.cycles import MemoryCycles

            # Use the ledger only; do not instantiate a publisher or mutate the vault.
            ledger = MemoryCycles.for_state(self.state)
            cycles = [
                c
                for c in ledger.list(binding["project_id"])
                if c["identity"].get("objective_id") == objective["id"]
                or (
                    c["identity"].get("thread_id") == binding["thread_id"]
                    and c["identity"].get("run_id") == binding["run_id"]
                )
            ]
            if any(
                c["write_state"] != "saved"
                or (c.get("write_receipt") or {}).get("state") != "confirmed"
                or (c.get("write_receipt") or {}).get("operation_id") != c["id"]
                or not (c.get("write_receipt") or {}).get("commit")
                for c in cycles
            ):
                raise StateError("required_memory_not_saved")
        if objective["status"] != "succeeded":
            raise StateError("objective_not_succeeded")
        jobs = self.state.list_jobs(objective["id"])
        if not jobs or any(j["state"] != "succeeded" for j in jobs):
            raise StateError("objective_jobs_pending_or_unsatisfied")
        if plan and not plan["delivery_contract_satisfied"]:
            raise StateError("delivery_contract_not_satisfied")
        if binding.get("job_id") and binding["job_id"] not in {j["id"] for j in jobs}:
            raise StateError("settlement_job_identity_mismatch")
        # A thread may have received another objective since authorization.
        others = self.state.connection.execute(
            "SELECT DISTINCT o.id,o.status FROM attempts a "
            "JOIN jobs j ON j.id=a.job_id JOIN objectives o ON o.id=j.objective_id "
            "WHERE a.thread_id=? OR a.owner_thread_id=?",
            (binding["thread_id"], binding["thread_id"]),
        ).fetchall()
        if any(o["id"] != objective["id"] for o in others):
            raise StateError("thread_has_other_objectives_outside_binding")
        unresolved = self.state._one(
            "SELECT x.id FROM operations x JOIN attempts a ON a.id=x.attempt_id "
            "JOIN jobs j ON j.id=a.job_id WHERE j.objective_id=? "
            "AND x.state NOT IN ('confirmed') LIMIT 1",
            (objective["id"],),
        )
        active = self.state._one(
            "SELECT a.id FROM attempts a JOIN jobs j ON j.id=a.job_id "
            "WHERE j.objective_id=? AND a.status NOT IN ('succeeded','failed','stopped')",
            (objective["id"],),
        )
        if unresolved or active:
            raise StateError("objective_execution_or_effect_unresolved")
        return {
            "objective_status": objective["status"],
            "job_states": {j["id"]: j["state"] for j in jobs},
            "delivery_stages": plan.get("stages", {}),
        }

    @staticmethod
    def remote_gate(binding: dict, snapshot: dict) -> dict:
        thread = snapshot.get("thread", {})
        if (
            thread.get("threadId") != binding["thread_id"]
            or thread.get("projectId") != binding["project_id"]
        ):
            raise StateError("settlement_thread_identity_mismatch")
        if type(thread.get("settled")) is not bool:
            raise StateError("settled_observation_unavailable")
        if (
            not isinstance(thread.get("updatedAt"), str)
            or not thread["updatedAt"]
            or type(thread.get("itemCount")) is not int
            or thread["itemCount"] < 0
        ):
            raise StateError("thread_revision_observation_unavailable")
        if thread.get("archived") is not False:
            raise StateError("thread_archived_or_unknown")
        if (
            thread.get("activeRunId", "unknown") is not None
            or thread.get("pendingRequestCount") != 0
            or thread.get("status") != "completed"
        ):
            raise StateError("thread_active_failed_or_pending_question")
        if thread.get("latestRunId") != binding["run_id"]:
            raise StateError("thread_changed_since_authorization")
        statuses = [
            r.get("status")
            for r in snapshot.get("recentRuns", [])
            if r.get("runId") == binding["run_id"]
        ]
        if not statuses or any(s != "completed" for s in statuses):
            raise StateError("expected_run_not_completed")
        if (
            snapshot.get("childrenObserved") is not True
            or snapshot.get("hasPendingChildRuns") is not False
        ):
            raise StateError("thread_children_pending_or_unknown")
        return thread

    async def reconcile(self, client: Any, binding_id: str) -> dict:
        binding = self.store.get(binding_id)
        if not binding:
            raise StateError("unknown_settlement")
        # A later message invalidates the old intent permanently. Never unsettle
        # or start a settle/unsettle loop to force an old decision.
        history = self.store.events(binding_id)
        if binding["state"] == "reopened" or any(
            e["state"] == "reopened" for e in history
        ):
            return binding
        evidence: dict[str, Any] = {}
        try:
            snapshot = await client.settlement_snapshot(binding["thread_id"])
            thread = snapshot.get("thread", {})
            evidence["thread"] = thread
            evidence["childrenObserved"] = snapshot.get("childrenObserved")
            evidence["hasPendingChildRuns"] = snapshot.get("hasPendingChildRuns")
            if (
                thread.get("threadId") != binding["thread_id"]
                or thread.get("projectId") != binding["project_id"]
            ):
                raise StateError("settlement_thread_identity_mismatch")
            ever_confirmed = binding["state"] == "confirmed" or any(
                e["state"] == "confirmed" for e in history
            )
            if ever_confirmed:
                if thread.get("settled") is False:
                    return self.store.record(
                        binding_id,
                        "reopened",
                        evidence,
                        "settled_no_longer_observed; new human intent required",
                    )
                if thread.get("settled") is not True:
                    raise StateError("settled_observation_unavailable")
            if thread.get("latestRunId") != binding["run_id"]:
                return self.store.record(
                    binding_id,
                    "reopened",
                    evidence,
                    "thread_changed_since_authorization; new human intent required",
                )
            evidence.update(self.local_gate(binding))
            self.remote_gate(binding, snapshot)
            prs = await client.list_thread_pull_requests(binding["thread_id"])
            evidence["pull_requests"] = verify_pull_requests(
                binding["pull_requests"], prs
            )
            evidence["merge_source"] = "t3_tracked_host_state"
            if thread["settled"]:
                evidence["closure_source"] = (
                    "observed_after_request"
                    if binding["request_claimed"]
                    else "observed_existing_t3_settlement"
                )
                return self.store.record(binding_id, "confirmed", evidence)
            if binding["request_claimed"]:
                # Includes lost response, process crash and deferred request.
                # Public T3 has no idempotency key/CAS for organize; do not retry.
                return self.store.record(
                    binding_id,
                    "uncertain",
                    evidence,
                    "request_not_observed_settled; inspect T3 before new authorization",
                )
            # Re-read before mutation to catch new runs/messages/questions while
            # the tracked PR set was being inspected.
            fresh = await client.settlement_snapshot(binding["thread_id"])
            fresh_thread = self.remote_gate(binding, fresh)
            if thread.get("updatedAt") != fresh_thread.get("updatedAt") or thread.get(
                "itemCount"
            ) != fresh_thread.get("itemCount"):
                raise StateError("thread_changed_during_settlement_check")
            evidence.update(self.local_gate(binding))
            if fresh_thread["settled"]:
                evidence["thread"] = fresh_thread
                evidence["closure_source"] = "observed_existing_t3_settlement"
                return self.store.record(binding_id, "confirmed", evidence)
            client.validate_settle(binding["thread_id"])
            if not self.store.claim_request(binding_id, evidence):
                return self.store.get(binding_id)
            response = await client.settle_thread(binding["thread_id"])
            evidence["request_response"] = response
            # Even settled:true in a mutation response is not read confirmation.
            return self.store.record(binding_id, "requested", evidence)
        except Exception as exc:
            current = self.store.get(binding_id)
            return self.store.record(
                binding_id,
                "uncertain" if current["request_claimed"] else "blocked",
                evidence,
                str(exc) or type(exc).__name__,
            )
