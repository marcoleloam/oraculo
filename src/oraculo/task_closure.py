"""Event-driven PR gates, merge receipt and verified settlement for managed work.

GitHub owns merge enforcement; T3 owns watches. No PR polling loop is installed.
The original execution receipt and final memory are separate from settlement.
"""

from __future__ import annotations

from pathlib import Path

from oraculo.protocol import content_hash
from oraculo.settlement import Settlements, verify_pull_requests
from oraculo.state import StateError


class TaskClosure:
    def __init__(self, engine, lifecycle):
        self.engine = engine
        self.store, self.host, self.memory = engine.store, engine.host, engine.memory
        self.lifecycle = lifecycle

    async def policy(self, task_id: str, policy: dict, actor: dict):
        task = self.store.get(task_id)
        if task.get("deliverable_kind") == "plan":
            raise StateError("plan_not_publishable")
        if actor["thread_id"] != task["origin"]["thread_id"]:
            raise StateError("merge_policy_requires_origin")
        if not isinstance(policy, dict) or policy.get("merge_authorized") is not True:
            raise StateError("merge_authorization_required")
        if not policy.get("policy_source"):
            raise StateError("project_merge_policy_source_required")
        if task.get("merge_attempted") and task.get("merge_policy") != policy:
            raise StateError("uncertain_merge_policy_immutable")
        self.store.update(
            task_id,
            "pr_policy_recorded",
            {"policy_sha256": content_hash(policy), "authorized_by": actor},
            merge_policy=policy,
        )

    def _requirements(self, task):
        if task.get("deliverable_kind") == "plan":
            raise StateError("plan_not_publishable")
        if not task.get("publication") or not task.get("publication_spec"):
            raise StateError("publication_required_before_merge")
        specification = task["publication_spec"]
        if not self.engine.config.project_in_scope(
            Path(specification.get("publication_root", specification["cwd"])).resolve()
        ):
            raise StateError("publication_binding_outside_scope")
        if not self.engine._saved(task.get("memory") or {}) or not self.engine._saved(
            task.get("publication_memory") or {}
        ):
            raise StateError("confirmed_memory_required_before_merge")
        if task.get("work_status") != "succeeded" or not task.get("merge_policy"):
            raise StateError("validated_work_and_merge_policy_required")
        if self.engine._report_failed(task.get("report")):
            raise StateError("work_not_successful")
        if task["merge_policy"]["head_sha"] != task["publication"]["head_sha"]:
            raise StateError("new_head_requires_new_validation_and_publication")

    async def watch(self, task_id):
        task = self.store.get(task_id)
        if task.get("deliverable_kind") == "plan":
            raise StateError("plan_not_publishable")
        origin = await self.engine._thread(task["origin"]["thread_id"])
        if origin.get("parentThreadId"):
            raise StateError("pr_watch_requires_root_origin")
        receipt = await self.host.call(
            "watch_pull_request",
            {
                "threadId": task["origin"]["thread_id"],
                "url": task["publication"]["url"],
            },
        )
        if (
            receipt.get("url") != task["publication"]["url"]
            or receipt.get("watching") is not True
        ):
            raise StateError("pr_watch_unconfirmed")
        self.store.update(
            task_id,
            "pr_waiting",
            {"url": receipt["url"], "watching": True},
            watch_receipt=receipt,
        )

    async def check(self, task_id, *, merge=False):
        task = self.store.get(task_id)
        if task.get("deliverable_kind") == "plan":
            raise StateError("plan_not_publishable")
        self._requirements(task)
        receipt = self.lifecycle.inspect(
            task["publication"], task["merge_policy"], task["publication_spec"]["cwd"]
        )
        if receipt["state"] == "merged":
            if not receipt.get("merge_sha"):
                raise StateError("merge_not_confirmed")
            self.store.update(
                task_id,
                "merged",
                {
                    "url": receipt["url"],
                    "head_sha": receipt["head_sha"],
                    "merge_sha": receipt["merge_sha"],
                },
                merged=True,
                merge_receipt=receipt,
            )
            await self.finalize(task_id)
            return
        self.store.update(
            task_id,
            "pr_ready" if receipt["ready"] else "pr_checks_pending",
            {
                "head_sha": receipt["head_sha"],
                "ready": receipt["ready"],
                "blockers": receipt["blockers"],
            },
            pr_gate=receipt,
        )
        if receipt["state"] == "closed":
            raise StateError("closed_pr_requires_explicit_revision")
        if receipt["state"] != "merged" and not receipt["ready"]:
            await self.watch(task_id)
            return
        if not merge:
            return
        if receipt["state"] != "merged":
            observed = await self.engine._observe(task)
            if (
                observed.get("workState") != "result_available"
                or observed.get("hasPendingChildRuns") is not False
            ):
                raise StateError("task_work_pending_before_merge")

        if receipt["state"] != "merged":
            await self.watch(task_id)
            # A terminal result is not proof of successful current execution.
            # Recheck the original run, current thread and all visible descendants
            # before the irreversible merge, including for native delegations.
            await self.engine._execution_gate(task)

        def before_merge():
            self.store.update(
                task_id,
                "merge_pending",
                {
                    "head_sha": receipt["head_sha"],
                    "policy_sha256": content_hash(task["merge_policy"]),
                },
                merge_attempted=True,
            )

        merged = self.lifecycle.merge(
            task["publication"],
            task["merge_policy"],
            task["publication_spec"]["cwd"],
            before_merge=before_merge,
            reconcile_only=bool(task.get("merge_attempted")),
        )
        if merged.get("state") != "merged" or not merged.get("merge_sha"):
            raise StateError("merge_not_confirmed")
        self.store.update(
            task_id,
            "merged",
            {
                "url": merged["url"],
                "head_sha": merged["head_sha"],
                "merge_sha": merged["merge_sha"],
            },
            merged=True,
            merge_receipt=merged,
        )
        await self.finalize(task_id)

    async def finalize(self, task_id):
        task = self.store.get(task_id)
        if task.get("deliverable_kind") == "plan":
            raise StateError("plan_not_publishable")
        merged = task.get("merge_receipt")
        if not merged or not task["merged"]:
            raise StateError("verified_merge_required")
        key = task_id + ":merge:" + content_hash(merged)[:32]
        memory = await self.memory.begin(
            task["project"]["project_id"],
            task["parent"],
            key,
            "Verified merge and final task result",
        )
        saved = await self.memory.finish(
            task["project"]["project_id"],
            task["parent"],
            memory["cycle"]["id"],
            "succeeded",
            {
                "summary": "Task merged; deployment not observed",
                "artifacts": [merged],
                "evidence": {"task_id": task_id},
                "limitations": ["Merge is not deployment."],
            },
        )
        self.store.update(
            task_id,
            "final_memory_pending",
            {"write_state": saved["write_state"], "cycle_id": saved["id"]},
            final_memory=saved,
        )
        if not self.engine._saved(saved):
            raise StateError("final_memory_write_unconfirmed")
        client, binding, snapshot = await self.engine._execution_gate(task)
        thread_id = binding["thread_id"]
        await self._pull_request_gate(client, task)
        # PR inspection may overlap a new message or run. Re-read execution
        # before requesting settlement; the host offers no atomic cross-API lock.
        _, _, fresh = await self.engine._execution_gate(task)
        if any(
            fresh["thread"][key] != snapshot["thread"][key]
            for key in ("updatedAt", "itemCount")
        ):
            raise StateError("thread_changed_during_settlement_check")
        snapshot = fresh
        if snapshot["thread"]["settled"] is not True:
            if task.get("settlement_attempted"):
                raise StateError("settlement_uncertain_observe_only")
            self.store.update(
                task_id,
                "settlement_pending",
                {"thread_id": thread_id, "run_id": binding["run_id"]},
                settlement_attempted=True,
            )
            await self.host.call(
                "t3_thread_organize", {"threadId": thread_id, "action": "settle"}
            )
            snapshot = await client.settlement_snapshot(thread_id)
            Settlements.remote_gate(binding, snapshot)
            if snapshot["thread"]["settled"] is not True:
                raise StateError("settlement_unconfirmed")
        # Also validate after a new request or when reconciling a lost response.
        await self._pull_request_gate(client, task)
        unwatched = await self.host.call(
            "unwatch_pull_request",
            {"threadId": task["origin"]["thread_id"], "url": merged["url"]},
        )
        if unwatched.get("watching") is not False:
            raise StateError("pr_unwatch_unconfirmed")
        self.store.update(
            task_id,
            "settled",
            {
                "thread_id": thread_id,
                "merge_sha": merged["merge_sha"],
                "memory_commit": saved["write_receipt"]["commit"],
                "deployment": "not_observed",
            },
            settled=True,
        )

    async def _pull_request_gate(self, client, task):
        pr = task["merge_receipt"]
        # GitHub supplied the verified commit receipt. T3 exposes the linked
        # PR identities, target and host state, but not commit SHAs.
        expected = [
            {
                "host": "github.com" if pr["host"] == "github" else pr["host"],
                "repository": pr["repository"],
                "number": pr["number"],
                "base_branch": pr["base"],
            }
        ]
        observed = await client.list_thread_pull_requests(
            task["child"]["childThreadId"]
        )
        verify_pull_requests(expected, observed)
