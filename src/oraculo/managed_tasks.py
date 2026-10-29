"""Cooperative, caller-bound managed task lifecycle.

The host owns child scheduling and completion notifications. Resume is invoked
on those notifications, never by a model polling loop. SQLite stores intentions
before host mutations; a same-key delegate retry is safe under the T3 contract.
"""

from __future__ import annotations

import fcntl
import os
import re
import subprocess
from pathlib import Path

from oraculo.projects import resolve_project
from oraculo.protocol import canonical_json, content_hash, is_control_event
from oraculo.run_continuity import (
    cancelled_before_start,
    execution_run,
    native_terminal_run,
    report_link,
    terminal_chain,
)
from oraculo.settlement import Settlements
from oraculo.state import StateError
from oraculo.t3.client import T3Client
from oraculo.task_store import TaskStore

# A stopped original run may be followed by completed continuations in the same
# child thread. failed/rolled_back never belong to such a chain.
STOPPED_RUN_STATUSES = frozenset({"cancelled", "interrupted"})
CONTINUATION_RUN_STATUSES = STOPPED_RUN_STATUSES | {"completed"}
CERTIFIED_WORK_STATUSES = frozenset({"succeeded", "failed", "stopped"})

LAUNCH_HINTS = {
    "invalid_independent_flag": (
        "independent must be the JSON boolean true, false or absent."
    ),
    "native_child_unavailable_independent_required": (
        "The host offers no app-owned subagents. A top-level conversation "
        "requires an explicit independent=true request."
    ),
    "native_child_project_mismatch": (
        "The resolved project differs from the origin conversation's project. "
        "Start the task from a conversation bound to that project, or request "
        "independent=true explicitly for a top-level conversation."
    ),
    "native_child_workspace_mismatch": (
        "cwd is not the checkout bound to the origin conversation. Start from "
        "the origin's bound worktree, or request independent=true explicitly."
    ),
    "native_child_base_not_head": (
        "A native child starts from the origin checkout HEAD. Check out the "
        "requested base in the origin's bound worktree (or bind the origin to a "
        "worktree at that base) and start again, or request independent=true "
        "explicitly for a top-level conversation."
    ),
    "implicit_top_level_intent_refused": (
        "A persisted top-level intent without explicit independent=true is never "
        "sent. Cancel this task and start again natively or with independent=true."
    ),
}


class ReportValidationError(StateError):
    """Correctable input/preflight failure; never persist a blocked transition."""


class LaunchPreflightError(StateError):
    """Routing refused before any reservation, memory cycle or host mutation."""

    def __init__(self, reason: str, observed: dict | None = None):
        super().__init__(reason)
        self.observed = observed or {}


def git(
    cwd: str | Path,
    *args: str,
    strip=True,
    error="workspace_git_metadata_unavailable",
) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, LC_ALL="C")
    try:
        output = subprocess.run(
            ["git", "-C", str(cwd), *args],
            env=env,
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        ).stdout
        return output.strip() if strip else output
    except (OSError, subprocess.SubprocessError):
        raise StateError(error) from None


def unselected_paths(path: str | Path) -> list[str]:
    """Porcelain -z keeps whitespace literal; renames carry two path records."""
    records = iter(
        git(
            path, "status", "--porcelain=v1", "-z", "--untracked-files=all", strip=False
        ).split("\0")
    )
    paths = set()
    for record in records:
        if not record:
            continue
        paths.add(record[3:])
        if "R" in record[:2] or "C" in record[:2]:
            paths.add(next(records))
    return sorted(paths)


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
        self,
        config,
        store: TaskStore,
        host,
        memory,
        publisher=None,
        lifecycle=None,
        admission=None,
    ):
        self.config, self.store = config, store
        self.host, self.memory, self.publisher = host, memory, publisher
        self.lifecycle = lifecycle
        self.admission = admission

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
                if not getattr(self.store, "authority_owned", False):
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
            plan = None
            if action == "start":
                if payload.get("kind") != "work":
                    return {"state": "not_work", "created": False}
                text = payload.get("text")
                if not isinstance(text, str) or not text.strip() or len(text) > 32000:
                    raise StateError("task_brief_required")
                if is_control_event(payload) or is_control_event(text):
                    # Return notifications are never a human request or approval.
                    return {"state": "control_event_not_work", "created": False}
                if payload.get("approved_plan"):
                    reference = payload["approved_plan"]
                    plan = self.store.get(reference["task_id"])
                    if (
                        plan.get("deliverable_kind") != "plan"
                        or plan.get("approval_state") != "approved"
                        or plan["origin"]["thread_id"] != actor["thread_id"]
                        or plan["report"].get("commit") != reference.get("commit")
                    ):
                        raise StateError("approved_plan_binding_required")
                key = payload.get("key")
                if (
                    isinstance(key, str)
                    and 0 < len(key) <= 200
                    and not self.store.exists(actor, key)
                ):
                    # Routing is decided before the task, reservation, memory
                    # cycle or any host mutation exists. Other preflight errors
                    # keep their persisted blocked transition in _advance.
                    try:
                        plan = await self._launch_plan(payload, actor, caps)
                    except LaunchPreflightError as exc:
                        return self._launch_rejection(None, exc)
                    except (StateError, ValueError, KeyError):
                        plan = None
                task = self.store.create(actor, payload["key"], payload)
            else:
                task = self.store.get(payload["task_id"])
            task_id = task["id"]
            if action == "finish":
                if actor["thread_id"] not in {
                    task["origin"]["thread_id"],
                    (task.get("child") or {}).get("childThreadId"),
                }:
                    raise StateError("finish_requires_task_or_origin")
                try:
                    # Reject malformed content even before launch reconciliation,
                    # scope/cancellation transitions or outbox retries.
                    self._validate_report(task, payload.get("report"))
                except ReportValidationError as exc:
                    return self._report_rejection(task_id, str(exc))
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
                if action != "status" and task.get("work_status") == "cancelled":
                    raise StateError("managed_task_cancelled")
                if action != "status" and task.get("project"):
                    await self._scope(task)
                if action == "status":
                    pass
                elif action == "cancel":
                    await self._cancel(task_id, actor)
                elif action in {"start", "resume"}:
                    await self._advance(task_id, actor, caps, plan)
                elif action == "approve":
                    if (
                        actor["thread_id"] != task["origin"]["thread_id"]
                        or task.get("deliverable_kind") != "plan"
                        or not self._saved(task.get("memory") or {})
                        or payload.get("commit")
                        != (task.get("report") or {}).get("commit")
                        or not isinstance(payload.get("evidence"), str)
                        or not payload["evidence"].strip()
                        or is_control_event(payload["evidence"])
                    ):
                        raise StateError("plan_approval_binding_required")
                    self.store.update(
                        task_id,
                        "plan_approved",
                        {
                            "approval": payload["evidence"][:2000],
                            "commit": payload["commit"],
                            "actor": actor,
                        },
                        approval_state="approved",
                    )
                elif action == "continue":
                    await self._continue(task_id, payload)
                elif action == "progress":
                    self.store.update(
                        task_id,
                        task["phase"],
                        {"progress": str(payload["message"])[:1000]},
                    )
                elif action == "recover-report":
                    await self._recover_report(task, payload, actor)
                elif action in {"complete-origin", "supersede"}:
                    from oraculo.task_recovery import TaskRecovery

                    recovery = TaskRecovery(self)
                    if action == "complete-origin":
                        await recovery.complete(task, payload, actor)
                    else:
                        await recovery.supersede(task, payload, actor)
                elif action == "finish":
                    if actor["thread_id"] == (task.get("child") or {}).get(
                        "childThreadId"
                    ):
                        await self._prevalidate_report(task, payload.get("report"))
                        # An executing task cannot certify its own terminal host run.
                        # Persist the report; the daemon/origin confirms after return.
                        for field in ("report", "submitted_report"):
                            prior = task.get(field)
                            if prior is not None and prior != payload["report"]:
                                raise StateError("terminal_report_reused")
                        self.store.update(
                            task_id,
                            "validation_submitted",
                            {
                                "report_sha256": content_hash(payload["report"]),
                                "submission": actor,
                            },
                            submitted_report=payload["report"],
                            publication_request=payload.get("publication"),
                        )
                    else:
                        await self._finish(task_id, payload.get("report"), actor)
                elif action == "deployment":
                    from oraculo.deployment import deployment_evidence

                    if actor["thread_id"] != task["origin"]["thread_id"]:
                        raise StateError("deployment_requires_origin")
                    evidence = deployment_evidence(
                        payload.get("deployment"), task.get("merge_receipt")
                    )
                    records = task.get("deployments", [])
                    if evidence not in records:
                        self.store.update(
                            task_id,
                            task["phase"],
                            {"deployment": evidence},
                            deployments=[*records, evidence],
                        )
                elif action in {"pr-check", "merge", "settle", "unwatch"}:
                    if task.get("deliverable_kind") == "plan":
                        raise StateError("plan_not_publishable")
                    from oraculo.task_closure import TaskClosure

                    if self.lifecycle is None:
                        raise StateError("pr_lifecycle_adapter_unavailable")
                    closure = TaskClosure(self, self.lifecycle)
                    if payload.get("policy"):
                        await closure.policy(task_id, payload["policy"], actor)
                    if action == "settle":
                        await closure.finalize(task_id)
                    elif action == "unwatch":
                        if task["publication"].get("host") == "azure":
                            self.store.update(
                                task_id,
                                task["phase"],
                                {
                                    "native_watch": "unsupported",
                                    "source": "oraculo_adapter_capability_policy",
                                },
                            )
                        else:
                            receipt = await self.host.call(
                                "unwatch_pull_request",
                                {
                                    "threadId": task["origin"]["thread_id"],
                                    "url": task["publication"]["url"],
                                },
                            )
                            if receipt.get("watching") is not False:
                                raise StateError("pr_unwatch_unconfirmed")
                            self.store.update(
                                task_id, task["phase"], {"watching": False}
                            )
                    else:
                        await closure.check(task_id, merge=action == "merge")
                elif action == "publish":
                    await self._publish(task_id, payload["publication"], actor)
                else:
                    raise StateError("unknown_managed_action")
            except LaunchPreflightError as exc:
                return self._launch_rejection(task_id, exc)
            except ReportValidationError as exc:
                return self._report_rejection(task_id, str(exc))
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

    def _report_rejection(self, task_id, reason):
        # A response, not a persisted task transition or origin notification.
        hint = {
            "malformed_validation_check": (
                "checks require command and status passed|failed; optional keys: "
                "result, proof, layer. Put unexecuted checks in limitations."
            ),
            "memory_report_too_large": "Retained report must fit 262144 canonical UTF-8 bytes.",
            "validated_commit_or_clean_workspace_required": (
                "Supply the full validated HEAD SHA from the bound clean checkout."
            ),
        }.get(reason, "Correct the input or binding and retry the same task ID.")
        return dict(
            self.store.describe(task_id),
            phase="blocked",
            blocked_reason=reason,
            rejection={
                "persisted": False,
                "correctable": True,
                "reason": reason,
                "hint": hint,
            },
        )

    def _launch_rejection(self, task_id, exc: LaunchPreflightError):
        """A response only: no transition, outbox entry, reservation or launch."""
        reason = str(exc)
        rejection = {
            "persisted": False,
            "correctable": True,
            "reason": reason,
            "hint": LAUNCH_HINTS.get(
                reason, "Correct the routing request and retry the same task key."
            ),
            "observed": exc.observed,
            "host_effects": "none",
        }
        if task_id is None:
            return dict(
                state="launch_rejected",
                created=False,
                phase="blocked",
                blocked_reason=reason,
                rejection=rejection,
            )
        return dict(
            self.store.describe(task_id),
            phase="blocked",
            blocked_reason=reason,
            rejection=rejection,
        )

    async def _launch_plan(
        self, req: dict, actor: dict, caps: dict, project: dict | None = None
    ) -> dict:
        """Read-only routing decision; never chooses a top-level implicitly.

        appOwnedSubagents children keep the origin as parent. A top-level
        conversation exists only for an explicit boolean independent=true.
        """
        independent = req.get("independent", False)
        if type(independent) is not bool:
            raise LaunchPreflightError("invalid_independent_flag")
        if project is None:
            project = resolve_project(
                self.config,
                cwd=req["cwd"],
                projects=await self._projects(),
                explicit=req.get("project"),
            )
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
        requested_base = req.get("base", "HEAD")
        if not isinstance(requested_base, str) or requested_base.startswith("-"):
            raise StateError("invalid_base_ref")
        full_sha = bool(re.fullmatch(r"[a-fA-F0-9]{40}", requested_base))
        if ws["dirty"] and not full_sha:
            raise StateError("dirty_source_commit_selection_required")
        if full_sha:
            requested_base = requested_base.lower()
        base_sha = git(
            parent_path,
            "rev-parse",
            "--verify",
            requested_base + "^{commit}",
            error="invalid_base_ref",
        )
        plan = dict(
            project=project,
            ws=ws,
            base_sha=base_sha,
            native=False,
            source_dirty=ws["dirty"],
            source_commit=base_sha,
            unselected_paths=unselected_paths(parent_path) if ws["dirty"] else [],
        )
        if independent:
            return plan
        caller = await self._thread(actor["thread_id"])
        caller_path = caller.get("worktreePath") or project["project_path"]
        observed = dict(
            requested_base=requested_base,
            base_sha=base_sha,
            head=ws["head"],
            origin_project_id=actor["project_id"],
            project_id=project["project_id"],
        )
        if caps.get("features", {}).get("appOwnedSubagents") is not True:
            raise LaunchPreflightError(
                "native_child_unavailable_independent_required", observed
            )
        if actor["project_id"] != project["project_id"]:
            raise LaunchPreflightError("native_child_project_mismatch", observed)
        if Path(caller_path).resolve() != Path(ws["path"]).resolve():
            raise LaunchPreflightError("native_child_workspace_mismatch", observed)
        if ws["head"] != base_sha:
            raise LaunchPreflightError("native_child_base_not_head", observed)
        return dict(plan, native=True)

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

    async def _advance(
        self, task_id: str, actor: dict, caps: dict, plan: dict | None = None
    ):
        task = self.store.get(task_id)
        req = task["request"]
        if not task.get("project"):
            project = (
                plan["project"]
                if plan
                else resolve_project(
                    self.config,
                    cwd=req["cwd"],
                    projects=await self._projects(),
                    explicit=req.get("project"),
                )
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
            if task.get("origin_completion"):
                from oraculo.task_recovery import TaskRecovery

                await TaskRecovery(self).complete(
                    task, task["origin_completion"]["intent"], actor
                )
                return
            if task.get("supersession"):
                from oraculo.task_recovery import TaskRecovery

                await TaskRecovery(self).supersede(
                    task, task["supersession"]["intent"], actor
                )
                return
            if task["phase"] in {
                "pr_open",
                "pr_reconciled",
                "pr_waiting",
                "pr_ready",
                "merged",
                "settled",
                "awaiting_approval",
                "plan_approved",
            }:
                return
            report = task.get("submitted_report") or task.get("report")
            if report:
                await self._finish(task_id, report, task["origin"])
                publication = task.get("publication_request") or task.get(
                    "publication_spec"
                )
                if (
                    publication
                    and self.store.get(task_id).get("deliverable_kind") != "plan"
                ):
                    await self._publish(task_id, publication, task["origin"])
            elif task["work_status"] == "pending":
                await self._observe(task)
            return
        if not task.get("launch_attempted"):
            # Same read-only routing as start, before admission and memory.
            plan = plan or await self._launch_plan(req, actor, caps, project)
            if (
                task.get("transport_mode") == "managed_thread"
                and req.get("independent") is not True
            ):
                raise LaunchPreflightError("implicit_top_level_intent_refused")
        if self.admission is not None:
            provider = (req.get("target") or {}).get("providerInstanceId") or caps.get(
                "inheritedProviderInstanceId"
            )
            reservation = self.admission.reserve_managed(
                task_id, provider, req.get("executor_slots", 1)
            )
            self.store.update(
                task_id,
                task["phase"],
                {"admission": reservation},
                admission=reservation,
            )
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
                await self._reconcile_native_launch(task)
            else:
                await self._reconcile_launch(task)
            return
        ws, base_sha, native = plan["ws"], plan["base_sha"], plan["native"]
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
                {
                    "cycle_id": cycle["id"],
                    "read_receipt": cycle["read_receipt"],
                    "source_dirty": plan["source_dirty"],
                    "source_commit": plan["source_commit"],
                    "unselected_paths": plan["unselected_paths"],
                },
                memory=cycle,
                context=context,
                base_sha=base_sha,
                parent_workspace=ws,
                source_dirty=plan["source_dirty"],
                source_commit=plan["source_commit"],
                unselected_paths=plan["unselected_paths"],
            )
            task = self.store.get(task_id)
        if not task.get("launch_arguments"):
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
                    "Run long test suites in the foreground and wait for completion; never background/nohup them or create sessions that automatically reinvoke this executor.",
                    "Every host ACK must relay this frame's literal fresh tool output programmatically (ACP or bridge ack --from-file); never summarize, edit, synthesize or reuse snapshots.",
                    "Continuations require the complete terminal run chain, a report bound to its latest run, no live descendants and fresh post-memory gates. Never overwrite childRunId or bypass closure gates.",
                    "Use the caller-bound managed adapter with this task_id for status, progress, finish and publish; do not create this task again.",
                    "Finish with structured checks (command/status), decisions, changes, limitations and commit; publish only via the explicit safe repository binding.",
                    "Report host/adapter blockers to the origin through the persisted outbox. Do not require a user continue to carry out authorized work.",
                    "Additional implementation/test/review children must enter through oraculo task start with their own stable key and shared service admission; direct delegate_task bypasses capacity and is not covered. Wait for all children before reporting validation.",
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

    async def _cancel(self, task_id: str, actor: dict):
        """Origin-only: drop a task that never bound a child and free its slot.

        A lost launch response may still have created a child; the current host
        catalog must show no thread with the recorded launch title before the
        reservation is released. A visible candidate requires resume instead.
        """
        task = self.store.get(task_id)
        if actor["thread_id"] != task["origin"]["thread_id"]:
            raise StateError("managed_cancel_origin_required")
        if task.get("child"):
            raise StateError("managed_cancel_child_bound_use_finish")
        matches, rows = 0, []
        arguments = task.get("launch_arguments") or {}
        if task.get("launch_attempted") and arguments.get("title"):
            project_id = task["project"]["project_id"]
            if task.get("transport_mode") == "native_child":
                rows = await self._thread_rows(project_id)
                matches = len(
                    [
                        r
                        for r in rows
                        if r.get("title") == arguments["title"]
                        and r.get("parentThreadId") == task["origin"]["thread_id"]
                    ]
                )
            else:
                rows = await self._thread_rows(project_id)
                matches = len(
                    [
                        r
                        for r in rows
                        if r.get("title") == arguments["title"]
                        and not r.get("parentThreadId")
                    ]
                )
            if matches:
                raise StateError("managed_cancel_candidate_child_exists_resume")
        proof = {
            "reason": "origin_cancelled",
            "child_bound": False,
            "launch_attempted": bool(task.get("launch_attempted")),
            "catalog_matches": matches,
            "catalog_rows": len(rows),
            "actor": actor,
        }
        released = None
        if self.admission is not None:
            released = self.admission.release_unlaunched(task_id, proof)
        self.store.update(
            task_id,
            "cancelled",
            dict(proof, released=released),
            work_status="cancelled",
            launch_attempted=False,
        )

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

    async def _reconcile_native_launch(self, task: dict):
        """Bind an uncertain native launch: catalog first, then the same key.

        T3 exposes no read that maps a child thread to its taskId, so the only
        way to obtain the native binding is to replay the persisted
        delegate_task, byte-identical with clientRequestId ``<task_id>:delegate``
        (idempotent per caller under the T3 contract). The catalog decides how
        that replay is judged: with one unbound candidate the receipt must name
        it (a lookup, never a second child); with none the replay is the first
        effective launch; several candidates block without any host mutation.
        """
        arguments = task["launch_arguments"]
        origin = task["origin"]["thread_id"]
        rows = await self._thread_rows(task["project"]["project_id"])
        bound = {
            r[0]
            for r in self.store.db.execute(
                "SELECT json_extract(payload,'$.child.childThreadId') "
                "FROM managed_tasks WHERE origin=? AND id<>?",
                (origin, task["id"]),
            )
            if r[0]
        }
        candidates = sorted(
            r["threadId"]
            for r in rows
            if r.get("parentThreadId") == origin
            and r.get("title") == arguments.get("title")
            and r["threadId"] not in bound
        )
        if len(candidates) > 1:
            raise StateError("launch_uncertain_multiple_candidates")
        self.store.update(
            task["id"],
            "launch_pending",
            {
                "launch_reconciliation": "replay_same_key_expect_candidate"
                if candidates
                else "reissue_same_key_no_candidate",
                "client_request_id": arguments.get("clientRequestId"),
                "intent_sha256": content_hash(arguments),
                "catalog_rows": len(rows),
                "candidates": candidates,
                "excluded_bound": len(bound),
            },
        )
        receipt = await self.host.call("delegate_task", arguments)
        if candidates and receipt.get("childThreadId") != candidates[0]:
            # Never bind (or release) on a receipt that contradicts the catalog.
            raise StateError("launch_reconciliation_receipt_mismatch")
        await self._accept_launch(self.store.get(task["id"]), receipt)

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
        if self.admission is not None:
            self.admission.bind_managed(task["id"], receipt)
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
            self._phase(task["id"], "workspace_ready"),
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
                    self._phase(task["id"], "workspace_ready"),
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
            self.store.update(
                task["id"], self._phase(task["id"], phase), proof, observed=proof
            )
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
                self._phase(task["id"], "workspace_ready"),
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
        self.store.update(
            task["id"], self._phase(task["id"], phase), observed, observed=observed
        )
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

    @staticmethod
    def _report_kind(task, report):
        if not isinstance(report, dict):
            raise StateError("structured_report_required")
        kind = task["request"].get("deliverable_kind", "implementation")
        if kind not in {"plan", "implementation"}:
            raise StateError("invalid_deliverable_kind")
        if report.get("deliverable_kind", kind) != kind:
            raise StateError("deliverable_kind_conflict")
        if kind == "plan" and report.get("approval_state", "pending") != "pending":
            raise StateError("plan_requires_separate_origin_approval")
        return kind

    @staticmethod
    def _report_failed(report):
        """Validate evidence before interpreting it; completion is not approval."""
        from oraculo.memory.cycles import REPORT_FIELDS

        # Existing review producers include provenance alongside the canonical
        # memory fields. Preserve those extensions without treating them as
        # approval or copying them into the memory schema.
        review_metadata = {
            "reviewed_public_head",
            "public_base",
            "pull_request",
            "task_id",
            "linked_implementation_task",
            "origin",
            "memory",
            "findings",
            "prior_findings",
            "documentation_observations",
        }
        if (
            not isinstance(report, dict)
            or set(report) - REPORT_FIELDS - review_metadata
        ):
            raise StateError("invalid_validation_report")
        checks = report.get("checks")
        if not isinstance(checks, list) or not checks:
            raise StateError("structured_validation_checks_required")
        for check in checks:
            if (
                not isinstance(check, dict)
                or set(check) - {"command", "status", "result", "proof", "layer"}
                or not isinstance(check.get("command"), str)
                or not check["command"].strip()
                or check.get("status") not in ("passed", "failed")
            ):
                raise StateError("malformed_validation_check")
        if "verdict" in report and report["verdict"] not in ("PASS", "FAIL"):
            raise StateError("invalid_validation_verdict")
        if "summary" in report and not isinstance(report["summary"], str):
            raise StateError("invalid_validation_summary")
        return (
            report.get("verdict") == "FAIL"
            or bool(re.match(r"^\s*FAIL\b", report.get("summary", ""), re.I))
            or any(c["status"] == "failed" for c in checks)
        )

    @classmethod
    def _validate_report(cls, task, report):
        """One canonical content validator for submission, completion and recovery."""
        from oraculo.memory.cycles import report_projection

        try:
            kind = cls._report_kind(task, report)
            failed = cls._report_failed(report)
            # All persisted fields (including inert review metadata) must be JSON.
            canonical_json(report).encode()
            _, _, rejection = report_projection(report)
            if rejection:
                raise StateError(rejection)
            commit = report.get("commit")
            if (not failed or commit is not None) and (
                not isinstance(commit, str)
                or re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit) is None
            ):
                raise StateError("validated_commit_or_clean_workspace_required")
            return kind, failed
        except StateError as exc:
            raise ReportValidationError(str(exc)) from None
        except (TypeError, ValueError, UnicodeError):
            raise ReportValidationError("invalid_validation_report") from None

    async def _prevalidate_report(self, task, report):
        result = self._validate_report(task, report)
        if not task.get("child"):
            raise ReportValidationError("child_not_launched")
        if not result[1]:
            try:
                thread = await self._thread(task["child"]["childThreadId"])
                ws = self._report_workspace(task, thread)
                if ws["dirty"] or report["commit"] != ws["head"]:
                    raise StateError("validated_commit_or_clean_workspace_required")
            except StateError as exc:
                raise ReportValidationError(str(exc)) from None
        return result

    @staticmethod
    def _report_workspace(task, thread):
        expected_parent = (
            task["origin"]["thread_id"]
            if task["transport_mode"] == "native_child"
            else None
        )
        if (
            thread.get("threadId") != task["child"]["childThreadId"]
            or thread.get("projectId") != task["project"]["project_id"]
            or thread.get("parentThreadId") != expected_parent
        ):
            raise StateError("task_thread_binding_mismatch")
        if not thread.get("worktreePath"):
            raise StateError("child_workspace_not_ready")
        ws = workspace(thread["worktreePath"])
        parent = task["parent_workspace"]
        if (
            ws["path"] == parent["path"]
            or ws["common_dir"] != parent["common_dir"]
            or ws["branch"] == parent["branch"]
            or (task["expected_branch"] and ws["branch"] != task["expected_branch"])
            or (
                task.get("child_workspace")
                and ws["path"] != task["child_workspace"]["path"]
            )
        ):
            raise StateError("child_workspace_not_isolated")
        git(ws["path"], "merge-base", "--is-ancestor", task["base_sha"], "HEAD")
        return ws

    async def _recover_report(self, task, payload, actor):
        """Replace only malformed, uncertified legacy submissions, with an audit.

        This service-owned action does not finish memory or release capacity.
        Origin resumes the same task after inspecting the replacement receipt.
        """
        try:
            if actor["thread_id"] != task["origin"]["thread_id"] or actor[
                "project_id"
            ] != task["origin"].get("project_id"):
                raise StateError("report_recovery_requires_origin")
            if (
                set(payload)
                != {"task_id", "report", "expected_report_sha256", "reason"}
                or not isinstance(payload.get("reason"), str)
                or not payload["reason"].strip()
                or len(payload["reason"]) > 2000
            ):
                raise StateError("report_recovery_payload_required")
            audit = task.get("report_recovery")
            original = (
                audit["original_report"] if audit else task.get("submitted_report")
            )
            if original is None or not task.get("child"):
                raise StateError("invalid_legacy_submission_required")
            digest = content_hash(original)
            if payload["expected_report_sha256"] != digest:
                raise StateError("report_recovery_digest_mismatch")
            submissions = [
                e["proof"].get("report_sha256")
                for e in task["events"]
                if e["phase"] == "validation_submitted"
            ]
            if not submissions or submissions[-1] != digest:
                raise StateError("report_recovery_submission_digest_mismatch")
            try:
                self._validate_report(task, original)
            except ReportValidationError as exc:
                invalid_reason = str(exc)
            else:
                raise StateError("valid_submitted_report_immutable")
            replacement = payload["report"]
            _, failed = await self._prevalidate_report(task, replacement)
            if audit and (
                audit["original_sha256"] != digest
                or audit["replacement_sha256"] != content_hash(replacement)
                or audit["reason"] != payload["reason"]
                or task.get("submitted_report") != replacement
            ):
                raise StateError("report_recovery_reused")
            if isinstance(original, dict):
                negative_checks = (
                    [
                        c
                        for c in original.get("checks", [])
                        if isinstance(c, dict) and c.get("status") == "failed"
                    ]
                    if isinstance(original.get("checks"), list)
                    else []
                )
                negative = (
                    original.get("verdict") == "FAIL"
                    or (
                        isinstance(original.get("summary"), str)
                        and re.match(r"^\s*FAIL\b", original["summary"], re.I)
                    )
                    or negative_checks
                )
                if (negative and not failed) or any(
                    c not in replacement["checks"] for c in negative_checks
                ):
                    raise StateError("report_recovery_negative_evidence_required")
            # A replay after completion may acknowledge the existing audit only.
            # A first recovery must not replace any memory/report certification.
            if not audit:
                if (
                    task.get("report") is not None
                    or task.get("work_status") != "pending"
                    or task.get("publication")
                    or task.get("merged")
                    or task.get("settled")
                    or (task.get("memory") or {}).get("write_state") != "not_requested"
                ):
                    raise StateError("report_recovery_already_certified")
                cycle = await self.memory.status(
                    task["project"]["project_id"], task["parent"], task["memory"]["id"]
                )
                if (
                    cycle.get("id") != task["memory"]["id"]
                    or cycle.get("project_id") != task["project"]["project_id"]
                    or any(
                        cycle.get("identity", {}).get(k) != task["parent"][k]
                        for k in ("thread_id", "run_id")
                    )
                    or cycle.get("work_status") != "running"
                    or cycle.get("write_state") != "not_requested"
                    or cycle.get("write_receipt") is not None
                    or cycle.get("report_sha256") is not None
                ):
                    raise StateError("report_recovery_memory_not_pristine")
            # Fresh real bridge reads; the report and stored observations alone
            # are never evidence that the executor or descendants have stopped.
            snapshot = await self._terminal_gate(task)
            runs = snapshot["recentRuns"]
            if (
                len({r["runId"] for r in runs}) != len(runs)
                or any(
                    r["status"]
                    not in {"completed", "failed", "cancelled", "interrupted"}
                    for r in runs
                )
                or any(
                    r["status"] != snapshot["thread"]["status"]
                    for r in runs
                    if r["runId"] == snapshot["thread"]["latestRunId"]
                )
            ):
                raise StateError("terminal_execution_or_descendants_unproven")
            ws = self._report_workspace(task, snapshot["thread"])
            if not failed and (ws["dirty"] or replacement["commit"] != ws["head"]):
                raise StateError("validated_commit_or_clean_workspace_required")
            if task["transport_mode"] == "native_child":
                observed = await self.host.call(
                    "task_status", {"taskId": task["child"]["taskId"]}
                )
                if (
                    any(
                        observed.get(k) != task["child"].get(k)
                        for k in ("taskId", "childThreadId", "childRunId")
                    )
                    or observed.get("workState") != "result_available"
                    or observed.get("hasPendingChildRuns") is not False
                    or observed.get("status")
                    != next(
                        r["status"]
                        for r in runs
                        if r["runId"] == task["child"]["childRunId"]
                    )
                ):
                    raise StateError("terminal_execution_or_descendants_unproven")
            report_link(
                task
                if audit
                else dict(task, report_recovery={"original_sha256": digest}),
                snapshot,
                replacement,
            )
            if audit:
                return
            if cycle != await self.memory.status(
                task["project"]["project_id"], task["parent"], task["memory"]["id"]
            ):
                raise StateError("report_recovery_memory_changed_retry")
            self.store.recover_report(
                task,
                replacement,
                {
                    "original_report": original,
                    "original_sha256": digest,
                    "replacement_sha256": content_hash(replacement),
                    "invalid_reason": invalid_reason,
                    "reason": payload["reason"],
                    "actor": actor,
                    "terminal_snapshot": snapshot,
                },
            )
        except Exception as exc:
            code = str(exc) if isinstance(exc, StateError) else type(exc).__name__
            raise ReportValidationError(code) from None

    def _terminal_snapshot(self, task, snapshot, *, workspace_required=True):
        from oraculo.t3.contracts import TERMINAL_STATUSES

        thread = snapshot["thread"]
        expected_parent = (
            task["origin"]["thread_id"]
            if task["transport_mode"] == "native_child"
            else None
        )
        if (
            thread["threadId"] != task["child"]["childThreadId"]
            or thread["projectId"] != task["project"]["project_id"]
            or thread.get("parentThreadId") != expected_parent
            or thread["activeRunId"] is not None
            or thread["pendingRequestCount"] != 0
            or thread["status"] not in TERMINAL_STATUSES
            or snapshot["hasPendingChildRuns"] is not False
            or snapshot["childrenObserved"] is not True
        ):
            raise StateError("terminal_execution_or_descendants_unproven")
        chain = terminal_chain(snapshot)
        if task["child"]["childRunId"] not in {r["runId"] for r in chain}:
            raise StateError("original_task_run_not_observed")
        if (
            task["transport_mode"] != "native_child"
            and thread["latestRunId"] != task["child"]["childRunId"]
        ):
            raise StateError("terminal_execution_or_descendants_unproven")
        if workspace_required:
            self._report_workspace(task, thread)
        return chain

    @staticmethod
    def _continued_after_stop(task, chain):
        """Original cancelled/interrupted, then completed continuation(s).

        Same child thread and immutable childRunId; every earlier run stopped or
        completed and the latest run completed. A failed or rolled back run
        anywhere in the chain, or a single run, never qualifies.
        """
        return (
            len(chain) > 1
            and chain[0]["runId"] == task["child"]["childRunId"]
            and chain[0]["status"] in STOPPED_RUN_STATUSES
            and chain[-1]["status"] == "completed"
            and all(r["status"] in CONTINUATION_RUN_STATUSES for r in chain)
        )

    async def _terminal_gate(
        self,
        task,
        *,
        successful=False,
        observations=None,
        workspace_required=True,
        pending_reason=None,
    ):
        """Preserve original binding and prove every run/descendant terminal."""
        client = T3Client("http://127.0.0.1/mcp")
        client.call = self.host.call
        snapshot = await client.settlement_snapshot(
            task["child"]["childThreadId"],
            terminal_only=not successful,
            complete_runs=True,
        )
        if pending_reason:
            from oraculo.t3.contracts import TERMINAL_STATUSES

            thread = snapshot["thread"]
            if (
                thread.get("activeRunId")
                or thread.get("pendingRequestCount") != 0
                or thread.get("status") not in TERMINAL_STATUSES
                or snapshot.get("hasPendingChildRuns") is not False
                or any(
                    r.get("status") not in TERMINAL_STATUSES
                    for r in snapshot["recentRuns"]
                )
            ):
                raise StateError(pending_reason)
        if successful:
            self._successful_snapshot(task, snapshot)
        chain = self._terminal_snapshot(
            task, snapshot, workspace_required=workspace_required
        )
        if task["transport_mode"] == "native_child":
            observed = await self.host.call(
                "task_status", {"taskId": task["child"]["taskId"]}
            )
            if pending_reason and (
                observed.get("workState") != "result_available"
                or observed.get("hasPendingChildRuns") is not False
            ):
                raise StateError(pending_reason)
            # task_status.status follows the delegated task: after a stopped
            # original it reports the completed continuation, never the original.
            expected_statuses = {chain[-1]["status"]}
            native_terminal = chain[-1]
            if cancelled_before_start(chain[-1]):
                native_terminal = native_terminal_run(chain)
                expected_statuses = {chain[0]["status"], native_terminal["status"]}
            if (
                any(
                    observed.get(k) != task["child"].get(k)
                    for k in ("taskId", "childThreadId", "childRunId")
                )
                or observed.get("status") not in expected_statuses
                or observed.get("workState") != "result_available"
                or observed.get("hasPendingChildRuns") is not False
                or observed.get("hasPendingQuestion") is not False
                or (
                    len(chain) > 1
                    and (
                        observed.get("latestTerminalRunId") != native_terminal["runId"]
                        or observed.get("latestTerminalStatus")
                        != native_terminal["status"]
                    )
                )
            ):
                raise StateError("terminal_execution_or_descendants_unproven")
            if observations is not None:
                observations["task_status"] = {
                    k: observed.get(k)
                    for k in (
                        "taskId",
                        "childThreadId",
                        "childRunId",
                        "status",
                        "workState",
                        "hasPendingChildRuns",
                        "hasPendingQuestion",
                        "latestTerminalRunId",
                        "latestTerminalStatus",
                    )
                }
        return snapshot

    def _successful_snapshot(self, task, snapshot):
        """Keep host truth intact when the latest request never executed."""
        thread = snapshot["thread"]
        audit = task.get("origin_completion")
        if snapshot.get("childrenSuccessful") is not True:
            raise StateError("thread_children_pending_or_unknown")
        latest = [
            r for r in snapshot["recentRuns"] if r["runId"] == thread["latestRunId"]
        ]
        unstarted_latest = (
            thread["status"] == "cancelled"
            and len(latest) == 1
            and cancelled_before_start(latest[0])
        )
        if not audit and not unstarted_latest:
            Settlements.remote_gate(
                dict(
                    thread_id=task["child"]["childThreadId"],
                    project_id=task["project"]["project_id"],
                    run_id=(task.get("completion_evidence") or {}).get(
                        "latest_run_id", thread["latestRunId"]
                    ),
                ),
                snapshot,
            )
            return
        chain = self._terminal_snapshot(task, snapshot)
        if (
            thread.get("archived") is not False
            or type(thread.get("settled")) is not bool
            or not thread.get("updatedAt")
            or type(thread.get("itemCount")) is not int
            or snapshot.get("childrenSuccessful") is not True
        ):
            raise StateError("thread_active_failed_or_pending_question")
        if audit:
            if audit["runs"] != chain or audit["child"] != task["child"]:
                raise StateError("execution_changed_after_validation")
        elif execution_run(chain)["status"] != "completed":
            raise StateError("expected_run_not_completed")

    async def _execution_gate(
        self, task, report=None, actor=None, observations=None, *, snapshot=None
    ):
        """Success may follow a proven continuation; the original is immutable."""
        if snapshot is None:
            snapshot = await self._terminal_gate(
                task, successful=True, observations=observations
            )
        else:
            self._successful_snapshot(task, snapshot)
        chain = terminal_chain(snapshot)
        original = next(r for r in chain if r["runId"] == task["child"]["childRunId"])
        independent = task.get("origin_completion")
        current_report = report or task.get("report") or task.get("submitted_report")
        if independent and independent["report_sha256"] != content_hash(current_report):
            raise StateError("origin_completion_report_changed")
        after_stop = original["status"] != "completed"
        if (
            after_stop
            and not independent
            and not self._continued_after_stop(task, chain)
        ):
            raise StateError("expected_run_not_completed")
        binding = {
            "thread_id": task["child"]["childThreadId"],
            "project_id": task["project"]["project_id"],
            "run_id": snapshot["thread"]["latestRunId"],
        }
        evidence = report_link(
            task,
            snapshot,
            report or task.get("report") or task.get("submitted_report"),
            actor=actor,
        )
        if after_stop and not independent:
            # Stricter than a continuation of a completed original: only the
            # executor's own submission from the latest run certifies success.
            # Legacy unbound events and origin-authored reports do not.
            submitted = evidence.get("submission_actor") or {}
            if (
                not evidence.get("submission_event_id")
                or evidence.get("legacy_submission")
                or submitted.get("thread_id") != task["child"]["childThreadId"]
                or submitted.get("run_id") != chain[-1]["runId"]
            ):
                raise StateError("latest_run_report_unproven")
            if observations is not None:
                observations["continuation"] = dict(
                    basis="stopped_original_completed_continuation",
                    original_run_id=original["runId"],
                    original_status=original["status"],
                    latest_run_id=chain[-1]["runId"],
                    latest_status=chain[-1]["status"],
                    run_count=len(chain),
                    submission_run_id=submitted["run_id"],
                    task_status=observations.get("task_status"),
                )
        client = T3Client("http://127.0.0.1/mcp")
        client.call = self.host.call
        return client, binding, snapshot

    async def _finish(
        self, task_id: str, report: dict, actor: dict, *, workspace_required=True
    ):
        task = self.store.get(task_id)
        if actor["thread_id"] not in {
            task["origin"]["thread_id"],
            (task.get("child") or {}).get("childThreadId"),
        }:
            raise StateError("finish_requires_task_or_origin")
        if not task.get("child"):
            raise StateError("child_not_launched")
        if self._certified(task):
            # Certified results are immutable: replay never re-certifies,
            # rewrites memory, regresses the phase or resets approval_state.
            if report != task["report"]:
                raise ReportValidationError("terminal_report_reused")
            if self._reservation_held(task_id):
                await self._release_certified(
                    task, workspace_required=workspace_required
                )
                phase = self._certified_phase(task)
                if phase and task["phase"] == "blocked":
                    # A post-memory gate had blocked the release; now proven.
                    self.store.update(
                        task_id,
                        phase,
                        {
                            "memory_cycle_id": task["memory"]["id"],
                            "write_state": task["memory"]["write_state"],
                            "work_status": task["work_status"],
                        },
                    )
            if task["work_status"] != "succeeded":
                raise StateError("work_not_successful")
            return
        kind, failed = await self._prevalidate_report(task, report)
        for field in ("report", "submitted_report"):
            if task.get(field) is not None and task[field] != report:
                raise StateError("terminal_report_reused")
        if not task["child"].get("childRunId"):
            await self._observe(task)
            task = self.store.get(task_id)
        # Even a negative result requires fresh terminal/current-run evidence.
        observations = {}
        snapshot = await self._terminal_gate(
            task,
            observations=observations,
            workspace_required=workspace_required,
            pending_reason="children_still_running",
        )
        observed = self._terminal_observation(snapshot, observations)
        evidence = report_link(task, snapshot, report, actor=actor)
        if observed.get("status") not in {
            "completed",
            "failed",
            "cancelled",
            "interrupted",
        }:
            raise StateError("terminal_execution_unproven")
        executed = [
            r for r in terminal_chain(snapshot) if not cancelled_before_start(r)
        ]
        succeeded = not failed and (
            (bool(executed) and executed[-1]["status"] == "completed")
            or bool(task.get("origin_completion"))
        )
        if succeeded:
            # Failed/stopped original runs still save their legitimate result.
            # Only successful certification requires the complete execution gate.
            _, _, snapshot = await self._execution_gate(
                task, report, actor, observations, snapshot=snapshot
            )
            evidence = report_link(task, snapshot, report, actor=actor)
            if observations.get("continuation"):
                # Receipt of the stopped-original rule; childRunId is untouched.
                evidence = dict(evidence, continuation=observations["continuation"])
            ws = self._report_workspace(task, snapshot["thread"])
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
            completion_evidence=evidence,
            work_status=status,
            deliverable_kind=kind,
            approval_state="pending" if kind == "plan" else "not_required",
        )
        cycle = (
            task["memory"]
            if self._saved(task["memory"])
            else await self.memory.finish(
                task["project"]["project_id"],
                task["parent"],
                task["memory"]["id"],
                status,
                report,
            )
        )
        self.store.update(
            task_id,
            ("awaiting_approval" if kind == "plan" else "pr_pending")
            if self._saved(cycle) and succeeded
            else "memory_pending",
            {
                "memory_cycle_id": cycle["id"],
                "write_state": cycle["write_state"],
                "work_status": status,
            },
            memory=cycle,
        )
        if not self._saved(cycle):
            raise StateError("memory_write_unconfirmed")
        # Memory can yield while a new run, descendant or checkout change begins.
        # Re-read the same proof before capacity release, including confirmed replay.
        current = self.store.get(task_id)
        final_observations = {}
        if succeeded:
            _, _, fresh = await self._execution_gate(
                current, observations=final_observations
            )
            ws = self._report_workspace(current, fresh["thread"])
            if ws["dirty"] or report.get("commit") != ws["head"]:
                raise StateError("validated_commit_or_clean_workspace_required")
        else:
            fresh = await self._terminal_gate(
                current,
                observations=final_observations,
                workspace_required=workspace_required,
            )
        report_link(current, fresh, report)
        if self.admission is not None:
            self.admission.release_managed(
                task_id, self._terminal_observation(fresh, final_observations)
            )
        if not succeeded:
            raise StateError("work_not_successful")

    async def _release_certified(self, task: dict, *, workspace_required=True):
        """Release a reservation left by an interrupted certification.

        Fresh terminal/execution gates and the same workspace binding are
        required, exactly as in the first pass, but nothing is re-certified.
        """
        report = task["report"]
        observations = {}
        current = self.store.get(task["id"])
        if task["work_status"] == "succeeded":
            _, _, fresh = await self._execution_gate(current, observations=observations)
            ws = self._report_workspace(current, fresh["thread"])
            if ws["dirty"] or report.get("commit") != ws["head"]:
                raise StateError("validated_commit_or_clean_workspace_required")
        else:
            fresh = await self._terminal_gate(
                current,
                observations=observations,
                workspace_required=workspace_required,
            )
        report_link(current, fresh, report)
        self.admission.release_managed(
            task["id"], self._terminal_observation(fresh, observations)
        )

    @staticmethod
    def _terminal_observation(snapshot, observations):
        return observations.get("task_status") or dict(
            status=snapshot["thread"]["status"],
            workState="result_available",
            hasPendingChildRuns=False,
        )

    @staticmethod
    def _certified_phase(task: dict) -> str | None:
        if task.get("work_status") != "succeeded":
            return None
        if task.get("deliverable_kind") == "plan":
            return (
                "plan_approved"
                if task.get("approval_state") == "approved"
                else "awaiting_approval"
            )
        return "pr_pending"

    def _certified(self, task: dict) -> bool:
        """Report certified and its memory confirmed: an immutable result."""
        return (
            task.get("report") is not None
            and task.get("work_status") in CERTIFIED_WORK_STATUSES
            and self._saved(task.get("memory") or {})
        )

    def _phase(self, task_id: str, phase: str) -> str:
        """Observations never move a certified task back to an earlier phase."""
        task = self.store.get(task_id)
        return task["phase"] if self._certified(task) else phase

    def _reservation_held(self, task_id: str) -> bool:
        if self.admission is None:
            return False
        row = self.admission._one(
            "SELECT id FROM managed_reservations WHERE id=?", (task_id,)
        )
        return bool(row) or task_id in (
            self.admission.get_meta("managed_legacy_untracked") or []
        )

    def background_work(self, task: dict) -> str | None:
        """What daemon reconciliation may still do; None means nothing.

        Native children are parent-driven; PR checks, watches and merge are
        event-driven. Certified results only release a leftover reservation or
        perform a requested publication still pending.
        """
        if task.get("work_status") == "cancelled" or task.get("supersession"):
            return None
        if task.get("transport_mode") != "managed_thread":
            return None
        if not task.get("child"):
            return "launch" if task.get("launch_attempted") else None
        if self._saved(task.get("publication_memory") or {}) or task.get("merged"):
            return None
        if self._certified(task):
            if self._reservation_held(task["id"]):
                return "release"
            if task["phase"] == "pr_pending" and (
                task.get("publication_request") or task.get("publication_spec")
            ):
                return "publish"
            return None
        return "observe"

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
        if task.get("deliverable_kind") == "plan":
            raise StateError("plan_not_publishable")
        if task["work_status"] != "succeeded" or not self._saved(task["memory"]):
            raise StateError("work_and_memory_required_before_pr")
        if self._report_failed(task.get("report")):
            raise StateError("work_not_successful")
        if not self.publisher:
            raise StateError("publication_host_adapter_unavailable")
        if (task.get("publication_attempted") or task.get("publication")) and task.get(
            "publication_spec"
        ) != spec:
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
        from oraculo.azure_repos import credential_reference
        from oraculo.publication import integration_binding

        credential_reference(spec.get("credential"))
        integration_binding(spec)
        uncertain = bool(task.get("publication_attempted") or task.get("publication"))
        if not uncertain:
            await self._execution_gate(task)
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
            dict(
                spec,
                task_id=task_id,
                reconcile_only=uncertain or spec.get("reconcile_only", False),
            ),
            before_create=before_create,
        )
        observed_at = pr.get("observed_at")
        prior = task.get("publication")
        if (
            prior
            and prior.get("state") == "merged"
            and any(
                prior.get(k) != pr.get(k)
                for k in (
                    "host",
                    "repository",
                    "number",
                    "url",
                    "head_sha",
                    "base",
                    "branch",
                    "repository_id",
                    "project_id",
                    "merge_sha",
                )
            )
        ):
            raise StateError("publication_merge_receipt_changed")
        if prior and {k: v for k, v in prior.items() if k != "observed_at"} == {
            k: v for k, v in pr.items() if k != "observed_at"
        }:
            pr = prior
        self.store.update(
            task_id,
            "pr_link_pending",
            {
                "url": pr["url"],
                "state": pr["state"],
                **({"observed_at": observed_at} if observed_at else {}),
            },
            publication=pr,
        )
        task = self.store.get(task_id)
        # Lost responses may be reconciled read-only even after execution changed.
        # Preserve the observed receipt, but never certify publication as ready
        # using stale execution evidence. No retry above can re-emit the PR.
        await self._execution_gate(task)
        if task.get("settled") and prior == pr and pr.get("state") == "merged":
            # A repeated observation cannot reopen the business phase. This is
            # not a new settlement or a fresh certification of rollout.
            self.store.update(
                task_id,
                "settled",
                {"publication_reobserved": pr["url"], "observed_at": observed_at},
                blocked_reason=None,
            )
            return
        if pr.get("host") == "azure":
            # Azure native tracking/watch is not verified by this adapter. Keep this
            # local capability observation distinct from a host link/watch ACK.
            self.store.update(
                task_id,
                "pr_link_pending",
                {
                    "native_tracking": "unsupported",
                    "source": "oraculo_adapter_capability_policy",
                },
                native_pr_tracking={
                    "state": "unsupported",
                    "host": "azure",
                    "source": "oraculo_adapter_capability_policy",
                },
            )
        for thread in (
            []
            if pr.get("host") == "azure"
            else dict.fromkeys(
                (
                    task["origin"]["thread_id"],
                    task["parent"]["thread_id"],
                    task["child"]["childThreadId"],
                )
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
        if pr["state"] == "merged" and spec.get("reconcile_only") is True:
            self.store.update(
                task_id,
                "pr_reconciled",
                {"url": pr["url"], "state": "merged"},
                blocked_reason=None,
            )
            return
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
        Certified results are never re-observed or re-certified: an exact replay
        writes no event and sends nothing.
        """
        task = self.store.get(task_id)
        if self.background_work(task) == "launch":
            try:
                await self._scope(task)
                await self._reconcile_launch(task)
                task = self.store.get(task_id)
            except Exception as exc:
                code = str(exc) if isinstance(exc, StateError) else type(exc).__name__
                self.store.update(
                    task_id, "blocked", {"reason": code}, blocked_reason=code
                )
        work = self.background_work(task)
        if work in {None, "launch"}:
            await self.flush_returns(task_id)
            return
        try:
            await self._scope(task)
            report = task.get("submitted_report") or task.get("report")
            if work == "release":
                await self._finish(task_id, task["report"], task["origin"])
            elif work == "publish":
                await self._publish(
                    task_id,
                    task.get("publication_request") or task.get("publication_spec"),
                    task["origin"],
                )
            elif report:
                await self._prevalidate_report(task, report)
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
        except ReportValidationError:
            return  # Invalid legacy input remains available for explicit recovery.
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
        from oraculo.presentation import return_message

        task = self.store.get(task_id)
        for event in self.store.prepare_returns(task_id):
            try:
                receipt = await self.host.call(
                    "t3_thread_send",
                    dict(
                        threadId=task["origin"]["thread_id"],
                        message=return_message(self.store.delivery(event)),
                        mode="auto",
                        clientRequestId=event["id"],
                    ),
                )
                self.store.acknowledge_return(event["id"], receipt)
            except Exception:
                return  # Durable pending outbox is visible; retry on next adapter call.
