"""Cooperative, caller-bound managed task lifecycle.

The host owns child scheduling and completion notifications. Resume is invoked
on those notifications, never by a model polling loop. SQLite stores intentions
before host mutations; a same-key delegate retry is safe under the T3 contract.
"""

from __future__ import annotations

import fcntl
import os
import subprocess
from pathlib import Path

from oraculo.projects import resolve_project
from oraculo.protocol import canonical_json, content_hash
from oraculo.state import StateError
from oraculo.task_store import TaskStore


def git(cwd: str | Path, *args: str) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, LC_ALL="C")
    try:
        return subprocess.run(
            ["git", "-C", str(cwd), *args],
            env=env,
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        raise StateError("workspace_git_metadata_unavailable") from None


def workspace(path: str | Path) -> dict:
    root = git(path, "rev-parse", "--show-toplevel")
    return dict(
        path=str(Path(root).resolve()),
        head=git(path, "rev-parse", "HEAD"),
        branch=git(path, "symbolic-ref", "--short", "HEAD"),
        common_dir=str(
            Path(
                git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
            ).resolve()
        ),
        dirty=bool(git(path, "status", "--porcelain")),
    )


class ManagedTasks:
    def __init__(
        self, config, store: TaskStore, host, memory, publisher=None, lifecycle=None
    ):
        self.config, self.store = config, store
        self.host, self.memory, self.publisher = host, memory, publisher
        self.lifecycle = lifecycle

    async def _identity(self) -> tuple[dict, dict]:
        caps = await self.host.call("orchestrator_capabilities", {})
        thread_id = caps.get("parentThreadId")
        if not thread_id:
            raise StateError("caller_bound_parent_required")
        thread = await self._thread(thread_id)
        run = thread.get("activeRunId")
        if not run:
            raise StateError("active_parent_run_required")
        return dict(
            thread_id=thread_id, run_id=run, project_id=thread["projectId"]
        ), caps

    async def _thread(self, thread_id: str) -> dict:
        result = await self.host.call(
            "t3_thread_read",
            dict(
                threadId=thread_id,
                view="activity",
                limit=1,
                runLimit=1,
                maxCharsPerItem=1,
            ),
        )
        thread = result["thread"]
        if thread["threadId"] != thread_id:
            raise StateError("host_thread_identity_mismatch")
        return thread

    async def _projects(self) -> list[dict]:
        rows, cursor, seen = [], None, set()
        for _ in range(100):
            page = await self.host.call(
                "t3_project_list",
                {"limit": 100, **({"cursor": cursor} if cursor is not None else {})},
            )
            rows.extend(page["projects"])
            cursor = page["nextCursor"]
            if cursor is None:
                return rows
            if cursor in seen:
                break
            seen.add(cursor)
        raise StateError("project_catalog_incomplete")

    async def command(self, action: str, payload: dict) -> dict:
        # A process-level lock also serializes independent adapter invocations.
        lock_path = self.store.state.path.with_suffix(".lock")
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise StateError("managed_adapter_busy_retry_same_key") from None
            actor, caps = await self._identity()
            if action == "list":
                return {
                    "tasks": [
                        self.store.describe(r[0])
                        for r in self.store.db.execute(
                            "SELECT id FROM managed_tasks WHERE origin=? ORDER BY rowid DESC",
                            (actor["thread_id"],),
                        )
                    ]
                }
            if action == "start":
                if payload.get("kind") != "work":
                    return {"state": "not_work", "created": False}
                text = payload.get("text")
                if not isinstance(text, str) or not text.strip() or len(text) > 32000:
                    raise StateError("task_brief_required")
                task = self.store.create(actor, payload["key"], payload)
            else:
                task = self.store.get(payload["task_id"])
            task_id = task["id"]
            if (
                not task.get("child")
                and task.get("launch_attempted")
                and task.get("transport_mode") == "managed_thread"
                and actor["thread_id"] != task["origin"]["thread_id"]
            ):
                await self._reconcile_launch(task)
                task = self.store.get(task_id)
            if actor["thread_id"] not in {
                task["origin"]["thread_id"],
                task["parent"]["thread_id"],
                (task.get("child") or {}).get("childThreadId"),
            }:
                raise StateError("managed_task_origin_mismatch")
            try:
                if action != "status" and task.get("project"):
                    await self._scope(task)
                if action == "status":
                    pass
                elif action in {"start", "resume"}:
                    await self._advance(task_id, actor, caps)
                elif action == "continue":
                    await self._continue(task_id, payload)
                elif action == "progress":
                    self.store.update(
                        task_id,
                        task["phase"],
                        {"progress": str(payload["message"])[:1000]},
                    )
                elif action == "finish":
                    if actor["thread_id"] == (task.get("child") or {}).get(
                        "childThreadId"
                    ):
                        # An executing task cannot certify its own terminal host run.
                        # Persist the report; the daemon/origin confirms after return.
                        prior = task.get("submitted_report")
                        if prior is not None and prior != payload["report"]:
                            raise StateError("terminal_report_reused")
                        self.store.update(
                            task_id,
                            "validation_submitted",
                            {"report_sha256": content_hash(payload["report"])},
                            submitted_report=payload["report"],
                            publication_request=payload.get("publication"),
                        )
                    else:
                        await self._finish(task_id, payload["report"], actor)
                elif action in {"pr-check", "merge", "settle", "unwatch"}:
                    from oraculo.task_closure import TaskClosure

                    if self.lifecycle is None:
                        raise StateError("pr_lifecycle_adapter_unavailable")
                    closure = TaskClosure(self, self.lifecycle)
                    if payload.get("policy"):
                        await closure.policy(task_id, payload["policy"], actor)
                    if action == "settle":
                        await closure.finalize(task_id)
                    elif action == "unwatch":
                        receipt = await self.host.call(
                            "unwatch_pull_request",
                            {
                                "threadId": task["origin"]["thread_id"],
                                "url": task["publication"]["url"],
                            },
                        )
                        if receipt.get("watching") is not False:
                            raise StateError("pr_unwatch_unconfirmed")
                        self.store.update(task_id, task["phase"], {"watching": False})
                    else:
                        await closure.check(task_id, merge=action == "merge")
                elif action == "publish":
                    await self._publish(task_id, payload["publication"], actor)
                else:
                    raise StateError("unknown_managed_action")
            except Exception as exc:
                # Do not return raw tool errors, prompts, stderr or private path dumps.
                code = (
                    str(exc)
                    if isinstance(exc, StateError)
                    else getattr(
                        exc, "reason", getattr(exc, "code", type(exc).__name__)
                    )
                )
                self.store.update(
                    task_id,
                    "blocked",
                    {"reason": code[:200]},
                    blocked_reason=code[:200],
                )
            await self.flush_returns(task_id)
            return self.store.describe(task_id)
        finally:
            os.close(fd)

    async def _scope(self, task: dict):
        project = task["project"]
        if not self.config.project_in_scope(project["project_path"]):
            raise StateError("managed_project_scope_revoked")
        current = resolve_project(
            self.config,
            cwd=project["project_path"],
            projects=await self._projects(),
            explicit=project["project_id"],
        )
        if any(
            current[key] != project[key]
            for key in ("project_id", "project_path", "repository_common_dir")
        ):
            raise StateError("managed_project_binding_changed")

    async def _advance(self, task_id: str, actor: dict, caps: dict):
        task = self.store.get(task_id)
        req = task["request"]
        if not task.get("project"):
            project = resolve_project(
                self.config,
                cwd=req["cwd"],
                projects=await self._projects(),
                explicit=req.get("project"),
            )
            self.store.update(
                task_id,
                "project_resolved",
                {"project_id": project["project_id"], "method": project["evidence"]},
                project=project,
            )
            task = self.store.get(task_id)
        project = task["project"]
        if task.get("child"):
            if task["phase"] in {
                "pr_open",
                "pr_waiting",
                "pr_ready",
                "merged",
                "settled",
            }:
                return
            report = task.get("submitted_report") or task.get("report")
            if report:
                await self._finish(task_id, report, task["origin"])
                publication = task.get("publication_request") or task.get(
                    "publication_spec"
                )
                if publication:
                    await self._publish(task_id, publication, task["origin"])
            elif task["work_status"] == "pending":
                await self._observe(task)
            return
        if task.get("launch_attempted"):
            if task["transport_mode"] == "native_child":
                caller = await self._thread(actor["thread_id"])
                source_path = (
                    caller.get("worktreePath") or task["project"]["project_path"]
                )
                if (
                    actor["thread_id"] != task["origin"]["thread_id"]
                    or caller["projectId"] != task["project"]["project_id"]
                    or workspace(source_path) != task["parent_workspace"]
                ):
                    raise StateError("native_retry_parent_binding_changed")
                receipt = await self.host.call(
                    "delegate_task", task["launch_arguments"]
                )
                await self._accept_launch(task, receipt)
            else:
                await self._reconcile_launch(task)
            return
        # The host launches from the selected project's Git repository. A cwd in
        # that repository may pin a private development commit; other-project cwd
        # never chooses the destination base implicitly.
        try:
            source = resolve_project(
                self.config, cwd=req["cwd"], projects=await self._projects()
            )
        except ValueError:
            if not req.get("project"):
                raise
            source = {}
        parent_path = (
            req["cwd"]
            if source.get("project_id") == project["project_id"]
            else project["project_path"]
        )
        ws = workspace(parent_path)
        if ws["dirty"]:
            raise StateError("dirty_source_commit_selection_required")
        requested_base = req.get("base", "HEAD")
        if not isinstance(requested_base, str) or requested_base.startswith("-"):
            raise StateError("invalid_base_ref")
        base_sha = git(
            parent_path, "rev-parse", "--verify", requested_base + "^{commit}"
        )
        if task.get("base_sha") and task["base_sha"] != base_sha:
            raise StateError("source_base_changed_reconcile_original_intent")
        if not task.get("memory"):
            context = await self.memory.begin(
                project["project_id"], task["parent"], task_id, req["text"]
            )
            cycle = context["cycle"]
            self.store.update(
                task_id,
                "memory_read",
                {"cycle_id": cycle["id"], "read_receipt": cycle["read_receipt"]},
                memory=cycle,
                context=context,
                base_sha=base_sha,
                parent_workspace=ws,
            )
            task = self.store.get(task_id)
        if not task.get("launch_arguments"):
            caller_thread = await self._thread(actor["thread_id"])
            caller_path = caller_thread.get("worktreePath") or project["project_path"]
            native = (
                not req.get("independent", False)
                and actor["project_id"] == project["project_id"]
                and caps.get("features", {}).get("appOwnedSubagents") is True
                and Path(caller_path).resolve() == Path(ws["path"]).resolve()
                and ws["head"] == base_sha
            )
            mode = "native_child" if native else "managed_thread"
            reference = self.store.context_artifact(task["context"])
            brief = {
                "marker": "ORACULO_MANAGED_TASK_V1",
                "task_id": task_id,
                "title": req.get("title", "Project task"),
                "request": req["text"],
                "project_id": project["project_id"],
                "base_sha": base_sha,
                "origin": task["origin"],
                "memory": reference,
                "adapter": {
                    "entrypoint": "oraculo task",
                    "config": str(self.config_path)
                    if hasattr(self, "config_path")
                    else None,
                },
                "rules": [
                    "This is a managed task. Use the supplied origin identity; native parentage applies only in native_child transport.",
                    "Before edits verify host-bound isolated workspace, requested branch, base SHA, and the SHA256 of the supplied private memory artifact. Read the whole artifact.",
                    "Implement and validate; preserve unrelated changes. Never push private history or commit unselected files.",
                    "Use the caller-bound managed adapter with this task_id for status, progress, finish and publish; do not create this task again.",
                    "Finish with structured checks (command/status), decisions, changes, limitations and commit; publish only via the explicit safe repository binding.",
                    "Report host/adapter blockers to the origin through the persisted outbox. Do not require a user continue to carry out authorized work.",
                    "Optional implementation/test/review children use delegate_task; wait for their results before reporting validation.",
                ],
            }
            branch = "oraculo/task-" + task_id.removeprefix("task-")[:16]
            title = req.get("title", "Project task")[:140] + " [" + task_id + "]"
            arguments = dict(
                projectId=project["project_id"],
                title=title,
                message=canonical_json(brief),
                workspaceStrategy=dict(
                    type="worktree",
                    baseRef=base_sha,
                    branch=branch,
                    startFromOrigin=False,
                ),
            )
            if native:
                arguments = dict(
                    task=canonical_json(brief),
                    title=req.get("title", "Project task"),
                    role="implementation",
                    isolation="worktree",
                    mode="async",
                    clientRequestId=task_id + ":delegate",
                )
                if req.get("target"):
                    arguments["target"] = req["target"]
            elif req.get("model_selection"):
                arguments["modelSelection"] = req["model_selection"]
            self.store.update(
                task_id,
                "launch_pending",
                {"title": title, "branch": branch, "base_sha": base_sha},
                launch_arguments=arguments,
                expected_branch=branch if not native else None,
                transport_mode=mode,
            )
            task = self.store.get(task_id)
        # launch has NO idempotency key. Persist before sending; every later call
        # only reconciles list/read, even when an empty response looks retryable.
        self.store.update(
            task_id,
            "launch_pending",
            {"intent_sha256": content_hash(task["launch_arguments"])},
            launch_attempted=True,
        )
        child = await self.host.call(
            "delegate_task"
            if task["transport_mode"] == "native_child"
            else "t3_thread_launch",
            task["launch_arguments"],
        )
        await self._accept_launch(self.store.get(task_id), child)

    async def _thread_rows(self, project_id: str) -> list[dict]:
        rows, cursor, seen = [], None, set()
        for _ in range(10):
            page = await self.host.call(
                "t3_thread_list",
                dict(
                    projectId=project_id,
                    includeSubagents=True,
                    limit=100,
                    **({"cursor": cursor} if cursor is not None else {}),
                ),
            )
            if page.get("projectId", project_id) != project_id:
                raise StateError("thread_catalog_project_mismatch")
            rows.extend(page["threads"])
            cursor = page["nextCursor"]
            if cursor is None:
                return rows
            if cursor in seen:
                break
            seen.add(cursor)
        raise StateError("thread_catalog_incomplete")

    async def _reconcile_launch(self, task: dict):
        await self._scope(task)
        rows = await self._thread_rows(task["project"]["project_id"])
        matches = [
            r
            for r in rows
            if r.get("title") == task["launch_arguments"]["title"]
            and not r.get("parentThreadId")
        ]
        if len(matches) != 1:
            raise StateError("launch_uncertain_no_unique_match")
        thread = await self._thread(matches[0]["threadId"])
        if thread.get("branch") != task["expected_branch"]:
            raise StateError("launch_reconciliation_workspace_mismatch")
        await self._accept_launch(
            task,
            dict(
                threadId=thread["threadId"],
                projectId=thread["projectId"],
                runId=None,
            ),
        )

    async def _accept_launch(self, task: dict, receipt: dict):
        native = task["transport_mode"] == "native_child"
        thread_id = receipt.get("childThreadId") if native else receipt.get("threadId")
        if (
            not thread_id
            or (native and not receipt.get("taskId"))
            or (
                not native and receipt.get("projectId") != task["project"]["project_id"]
            )
        ):
            raise StateError("task_launch_ack_incomplete")
        child = dict(
            childThreadId=thread_id,
            childRunId=receipt.get("childRunId") if native else receipt.get("runId"),
            launch_receipt=receipt,
        )
        if native:
            child["taskId"] = receipt["taskId"]
        self.store.update(
            task["id"],
            "launch_acknowledged",
            {
                "title": task["request"].get("title", "Project task"),
                "project_id": task["project"]["project_id"],
                "branch": task["expected_branch"],
                "thread_url": "t3-thread://v1/" + thread_id,
                "native_relationship": "subagent" if native else "top_level",
                "worktree": "preparing; host binding will follow",
            },
            child=child,
        )
        await self._observe(self.store.get(task["id"]))

    async def _observe(self, task: dict) -> dict:
        child = task["child"]
        thread = await self._thread(child["childThreadId"])
        expected_parent = (
            task["origin"]["thread_id"]
            if task["transport_mode"] == "native_child"
            else None
        )
        if (
            thread.get("parentThreadId") != expected_parent
            or thread["projectId"] != task["project"]["project_id"]
        ):
            raise StateError("task_thread_binding_mismatch")
        path = thread.get("worktreePath")
        if not path:
            raise StateError("child_workspace_not_ready")
        ws = workspace(path)
        parent_ws = task["parent_workspace"]
        if (
            ws["path"] == parent_ws["path"]
            or ws["common_dir"] != parent_ws["common_dir"]
            or ws["branch"] == parent_ws["branch"]
            or (task["expected_branch"] and ws["branch"] != task["expected_branch"])
        ):
            raise StateError("child_workspace_not_isolated")
        git(path, "merge-base", "--is-ancestor", task["base_sha"], "HEAD")
        self.store.update(
            task["id"],
            "workspace_ready",
            {
                "title": task["request"].get("title", "Project task"),
                "project_id": task["project"]["project_id"],
                "branch": ws["branch"],
                "worktree": ws["path"],
                "thread_url": "t3-thread://v1/" + child["childThreadId"],
                "base_sha": task["base_sha"],
            },
            child_workspace=ws,
        )
        if task["transport_mode"] == "native_child":
            observed = await self.host.call("task_status", {"taskId": child["taskId"]})
            if (
                observed.get("taskId") != child["taskId"]
                or observed.get("childThreadId") != child["childThreadId"]
                or (
                    child.get("childRunId")
                    and observed.get("childRunId") != child["childRunId"]
                )
            ):
                raise StateError("task_status_identity_mismatch")
            if not child.get("childRunId") and observed.get("childRunId"):
                child["childRunId"] = observed["childRunId"]
                self.store.update(
                    task["id"],
                    "workspace_ready",
                    {"original_run_id": child["childRunId"]},
                    child=child,
                )
            proof = {
                k: observed.get(k)
                for k in ("status", "workState", "hasPendingChildRuns")
            }
            phase = (
                "validating"
                if proof["workState"] == "result_available"
                and proof["hasPendingChildRuns"] is False
                else "executing"
            )
            self.store.update(task["id"], phase, proof, observed=proof)
            return proof
        snapshot = await self.host.call(
            "t3_thread_read",
            dict(
                threadId=child["childThreadId"],
                view="activity",
                limit=1,
                runLimit=50,
                maxCharsPerItem=1,
            ),
        )
        runs = snapshot.get("recentRuns", [])
        if not child.get("childRunId"):
            # A launch may acknowledge preparation before creating its first run.
            originals = [r for r in runs if r.get("ordinal") == 1]
            if (
                not originals
                and len(runs) == 1
                and snapshot["thread"].get("runCount", 1) == 1
            ):
                originals = runs
            if len(originals) != 1:
                raise StateError("original_task_run_not_observed")
            child["childRunId"] = originals[0]["runId"]
            self.store.update(
                task["id"],
                "workspace_ready",
                {"original_run_id": child["childRunId"]},
                child=child,
            )
        matched = [r for r in runs if r.get("runId") == child["childRunId"]]
        if len(matched) != 1:
            raise StateError("original_task_run_not_observed")
        terminal = {"completed", "failed", "cancelled", "interrupted"}
        status = matched[0]["status"]
        rows = await self._thread_rows(task["project"]["project_id"])
        children = {r["threadId"]: r for r in rows}
        descendants, seen = [child["childThreadId"]], set()
        current = snapshot["thread"]
        pending = (
            bool(current.get("activeRunId"))
            or current.get("status") not in terminal | {"idle"}
            or current.get("pendingRequestCount", 0) != 0
            or any(r.get("status") not in terminal for r in runs)
        )
        while descendants:
            current = descendants.pop()
            if current in seen:
                raise StateError("cyclic_task_descendants")
            seen.add(current)
            for row in children.values():
                if row.get("parentThreadId") == current:
                    pending = (
                        pending
                        or row.get("status") not in terminal | {"idle"}
                        or bool(row.get("activeRunId"))
                        or row.get("pendingRequestCount", 0) != 0
                    )
                    descendants.append(row["threadId"])
        observed = dict(
            status=status,
            original_run_id=child["childRunId"],
            hasPendingChildRuns=pending,
            workState="result_available"
            if status in terminal and not pending
            else "working",
        )
        phase = (
            "validating" if observed["workState"] == "result_available" else "executing"
        )
        self.store.update(task["id"], phase, observed, observed=observed)
        return observed

    async def _continue(self, task_id: str, payload: dict):
        task = self.store.get(task_id)
        if not payload.get("key") or not payload.get("text"):
            raise StateError("continuation_key_and_text_required")
        if not task.get("child"):
            raise StateError("child_not_launched")
        observed = await self._observe(task)
        if observed.get("workState") == "result_available":
            raise StateError("terminal_task_requires_explicit_new_revision")
        key = task_id + ":continue:" + payload["key"]
        prior = self.store.state.get_meta(key)
        digest = content_hash(payload["text"])
        if prior and prior["sha256"] != digest:
            raise StateError("continuation_key_reused")
        if prior and prior.get("receipt"):
            return
        self.store.state.set_meta(key, {"sha256": digest})
        receipt = await self.host.call(
            "t3_thread_send",
            dict(
                threadId=task["child"]["childThreadId"],
                message=payload["text"],
                mode="auto",
                clientRequestId=key,
            ),
        )
        self.store.state.set_meta(key, {"sha256": digest, "receipt": receipt})

    async def _finish(self, task_id: str, report: dict, actor: dict):
        task = self.store.get(task_id)
        if actor["thread_id"] not in {
            task["origin"]["thread_id"],
            (task.get("child") or {}).get("childThreadId"),
        }:
            raise StateError("finish_requires_task_or_origin")
        if not task.get("child"):
            raise StateError("child_not_launched")
        observed = await self._observe(task)
        if (
            observed.get("workState") != "result_available"
            or observed.get("hasPendingChildRuns") is not False
        ):
            raise StateError("children_still_running")
        if task.get("report") and task["report"] != report:
            raise StateError("terminal_report_reused")
        succeeded = observed.get("status") == "completed"
        if not isinstance(report, dict):
            raise StateError("structured_report_required")
        if succeeded and (
            not report.get("checks")
            or any(
                not isinstance(c, dict)
                or c.get("status") != "passed"
                or not c.get("command")
                for c in report["checks"]
            )
        ):
            raise StateError("passing_validation_evidence_required")
        if succeeded:
            ws = workspace(task["child_workspace"]["path"])
            if ws["dirty"] or report.get("commit") != ws["head"]:
                raise StateError("validated_commit_or_clean_workspace_required")
        status = (
            "succeeded"
            if succeeded
            else "stopped"
            if observed.get("status") in {"cancelled", "interrupted"}
            else "failed"
        )
        self.store.update(
            task_id,
            "memory_pending",
            {"work_status": status, "report_sha256": content_hash(report)},
            report=report,
            work_status=status,
        )
        cycle = await self.memory.finish(
            task["project"]["project_id"],
            task["parent"],
            task["memory"]["id"],
            status,
            report,
        )
        self.store.update(
            task_id,
            "pr_pending" if self._saved(cycle) and succeeded else "memory_pending",
            {
                "memory_cycle_id": cycle["id"],
                "write_state": cycle["write_state"],
                "work_status": status,
            },
            memory=cycle,
        )
        if not self._saved(cycle):
            raise StateError("memory_write_unconfirmed")
        if not succeeded:
            raise StateError("work_not_successful")

    @staticmethod
    def _saved(cycle: dict) -> bool:
        receipt = cycle.get("write_receipt") or {}
        return (
            cycle.get("write_state") == "saved"
            and receipt.get("state") == "confirmed"
            and receipt.get("operation_id") == cycle["id"]
            and bool(receipt.get("commit"))
        )

    async def _publish(self, task_id: str, spec: dict, actor: dict):
        task = self.store.get(task_id)
        if task["work_status"] != "succeeded" or not self._saved(task["memory"]):
            raise StateError("work_and_memory_required_before_pr")
        if not self.publisher:
            raise StateError("publication_host_adapter_unavailable")
        if task.get("publication_attempted") and task.get("publication_spec") != spec:
            raise StateError("publication_intent_reused")
        # Explicit public binding permits a separate safe checkout; never assumes
        # the private development checkout must have a remote or can be pushed.
        publication_path = Path(spec["cwd"]).resolve()
        allowed_path = Path(spec.get("publication_root", spec["cwd"])).resolve()
        if not self.config.project_in_scope(allowed_path):
            raise StateError("publication_binding_outside_scope")
        if (
            workspace(publication_path)["common_dir"]
            != workspace(allowed_path)["common_dir"]
        ):
            raise StateError("publication_checkout_binding_mismatch")
        if not spec.get("repository"):
            raise StateError("explicit_publication_repository_required")
        if spec.get("source_project_id") != task["project"]["project_id"] or spec.get(
            "source_commit"
        ) != task["report"].get("commit"):
            raise StateError("publication_source_binding_mismatch")
        uncertain = bool(task.get("publication_attempted"))
        self.store.update(
            task_id,
            "pr_pending",
            {"repository": spec["repository"], "head_sha": spec["head_sha"]},
            publication_spec=spec,
        )

        def before_create():
            self.store.update(
                task_id,
                "pr_pending",
                {"emission_intent": content_hash(spec)},
                publication_attempted=True,
            )

        # Always refresh a cached receipt, including after a lost link response.
        pr = self.publisher.ensure_pr(
            dict(spec, task_id=task_id, reconcile_only=uncertain),
            before_create=before_create,
        )
        self.store.update(
            task_id,
            "pr_link_pending",
            {"url": pr["url"], "state": pr["state"]},
            publication=pr,
        )
        task = self.store.get(task_id)
        for thread in dict.fromkeys(
            (
                task["origin"]["thread_id"],
                task["parent"]["thread_id"],
                task["child"]["childThreadId"],
            )
        ):
            linked = await self.host.call(
                "link_pull_request", {"threadId": thread, "url": pr["url"]}
            )
            if linked.get("url") != pr["url"]:
                raise StateError("pr_link_unconfirmed")
            links = await self.host.call(
                "list_thread_pull_requests", {"threadId": thread}
            )
            if not any(
                row.get("url") == pr["url"] for row in links.get("pullRequests", [])
            ):
                raise StateError("pr_link_not_observed")
        # A second immutable canonical cycle records the PR update idempotently;
        # the first terminal report and its receipt remain intact.
        revision = "pr-" + content_hash(pr)[:40]
        update = await self.memory.begin(
            task["project"]["project_id"],
            task["parent"],
            task_id + ":" + revision,
            "Task publication receipt",
        )
        saved = await self.memory.finish(
            task["project"]["project_id"],
            task["parent"],
            update["cycle"]["id"],
            "succeeded",
            {
                "summary": "Publication receipt",
                "artifacts": [pr],
                "evidence": {"task_id": task_id},
            },
        )
        self.store.update(
            task_id,
            "pr_memory_pending",
            {"url": pr["url"], "write_state": saved["write_state"]},
            publication_memory=saved,
        )
        if not self._saved(saved):
            raise StateError("pr_memory_write_unconfirmed")
        if pr["state"].upper() != "OPEN":
            raise StateError("pr_not_open_requires_reconciliation")
        self.store.update(
            task_id,
            "pr_open",
            {
                "url": pr["url"],
                "head_sha": pr["head_sha"],
                "base": pr["base"],
                "memory_commit": saved["write_receipt"]["commit"],
            },
            blocked_reason=None,
        )

    async def reconcile(self, task_id: str):
        """Daemon reconciliation: read known bindings and publish queued evidence.

        No launch/delegate here. Native task_status is parent-scoped, so a daemon
        queues one idempotent resume notice to that parent instead of impersonating it.
        """
        task = self.store.get(task_id)
        if (
            not task.get("child")
            and task.get("launch_attempted")
            and task.get("transport_mode") == "managed_thread"
        ):
            try:
                await self._scope(task)
                await self._reconcile_launch(task)
                task = self.store.get(task_id)
            except Exception as exc:
                code = str(exc) if isinstance(exc, StateError) else type(exc).__name__
                self.store.update(
                    task_id, "blocked", {"reason": code}, blocked_reason=code
                )
        if (
            not task.get("child")
            or self._saved(task.get("publication_memory") or {})
            or task.get("merged")
        ):
            # PR checks/merge are event-driven through T3 watches, never daemon polling.
            await self.flush_returns(task_id)
            return
        if task["transport_mode"] == "native_child":
            # Native completion already notifies its parent. Persisted reports
            # remain available to that parent's next adapter resume/finish call.
            await self.flush_returns(task_id)
            return
        try:
            await self._scope(task)
            report = task.get("submitted_report") or task.get("report")
            if report:
                observed = await self._observe(task)
                if (
                    observed["workState"] == "result_available"
                    and observed["hasPendingChildRuns"] is False
                ):
                    await self._finish(task_id, report, task["origin"])
                    publication = task.get("publication_request") or task.get(
                        "publication_spec"
                    )
                    if publication:
                        await self._publish(task_id, publication, task["origin"])
            else:
                observed = await self._observe(task)
                if observed["workState"] == "result_available":
                    raise StateError("terminal_task_report_missing")
        except Exception as exc:
            code = (
                str(exc)
                if isinstance(exc, StateError)
                else getattr(exc, "code", type(exc).__name__)
            )
            self.store.update(
                task_id, "blocked", {"reason": code[:200]}, blocked_reason=code[:200]
            )
        await self.flush_returns(task_id)

    async def flush_returns(self, task_id: str):
        task = self.store.get(task_id)
        for event in self.store.pending_returns(task_id):
            try:
                receipt = await self.host.call(
                    "t3_thread_send",
                    dict(
                        threadId=task["origin"]["thread_id"],
                        message=canonical_json(event["payload"]),
                        mode="auto",
                        clientRequestId=event["id"],
                    ),
                )
                self.store.acknowledge_return(event["id"], receipt)
            except Exception:
                return  # Durable pending outbox is visible; retry on next adapter call.
