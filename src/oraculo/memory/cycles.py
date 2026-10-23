"""Durable memory lifecycle, independent of executor slots and model jobs.

Only the existing Publisher writes the vault. SQLite records intent before I/O;
its durable proposal plus the publisher journal make replay safe after a crash.
The connection belongs to the calling thread: do not run these methods in a pool.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any

from oraculo.state import State

from .context import Context
from .publish import Publisher
from .schema import ConflictError, MemoryError, corpus, digest, project_key, render_page


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


REPORT_FIELDS = frozenset(
    {
        "verdict",
        "deliverable_kind",
        "approval_state",
        "approved_plan",
        "decisions",
        "changes",
        "results",
        "evidence",
        "limitations",
        "pending",
        "next_steps",
        "summary",
        "checks",
        "commit",
        "artifacts",
        "criteria",
        "operations",
        "title",
        "topics",
        "memory_application",
        "lessons",
        "errors",
    }
)


def report_projection(evidence):
    """The exact retained evidence, UTF-8 bytes and size/shape rejection.

    Pure so managed submissions can reject before freezing their report.
    Keep MemoryCycles' existing rejection receipt behavior for direct callers.
    """
    selected = (
        {key: value for key, value in evidence.items() if key in REPORT_FIELDS}
        if isinstance(evidence, dict)
        else evidence
    )
    serialized = _json(selected).encode()
    rejection = (
        "memory_report_must_be_structured"
        if not isinstance(evidence, dict)
        else "memory_report_too_large"
        if len(serialized) > 256 * 1024
        else None
    )
    return selected, serialized, rejection


class MemoryCycles:
    def __init__(self, state: State, publisher: Publisher | None) -> None:
        self.state = state
        self.publisher = publisher
        state.connection.execute(
            "CREATE TABLE IF NOT EXISTS memory_cycles("
            "id TEXT PRIMARY KEY, project_id TEXT NOT NULL, payload TEXT NOT NULL)"
        )
        state.connection.execute(
            "CREATE INDEX IF NOT EXISTS memory_cycles_project ON memory_cycles(project_id)"
        )

    @classmethod
    def for_state(cls, state: State) -> MemoryCycles:
        """Read the ledger for settlement without opening a vault or publisher."""
        return cls(state, None)

    def get(self, cycle_id: str) -> dict:
        row = self.state.connection.execute(
            "SELECT payload FROM memory_cycles WHERE id=?", (cycle_id,)
        ).fetchone()
        if row is None:
            raise MemoryError("memory_cycle_not_found")
        return json.loads(row["payload"])

    def _save(self, item: dict) -> dict:
        self.state.connection.execute(
            "UPDATE memory_cycles SET payload=? WHERE id=?",
            (_json(item), item["id"]),
        )
        return item

    def list(self, project_id: str = "*") -> list[dict]:
        sql, args = "SELECT payload FROM memory_cycles", ()
        if project_id != "*":
            sql, args = sql + " WHERE project_id=?", (project_id,)
        return [
            json.loads(row[0])
            for row in self.state.connection.execute(sql + " ORDER BY rowid", args)
        ]

    def for_objective(self, objective_id: str) -> list[dict]:
        return [
            item
            for item in self.list()
            if item["identity"].get("objective_id") == objective_id
        ]

    def pending(self, objective_id: str | None = None) -> list[dict]:
        items = self.for_objective(objective_id) if objective_id else self.list()
        return [item for item in items if item["write_state"] in {"pending", "failed"}]

    def begin(
        self,
        project_id: str,
        cycle_key: str,
        query: str,
        identity: dict,
        *,
        skip_read_reason: str | None = None,
    ) -> dict:
        project_key(project_id)
        if project_id == "shared":
            raise MemoryError("automatic_memory_requires_private_project")
        if (
            not cycle_key
            or not isinstance(query, str)
            or not isinstance(identity, dict)
        ):
            raise MemoryError("invalid_memory_cycle")
        if skip_read_reason is not None and (
            not isinstance(skip_read_reason, str)
            or not skip_read_reason
            or len(skip_read_reason) > 512
        ):
            raise MemoryError("invalid_memory_skip_read_reason")
        cycle_id = "cycle-" + digest(_json([project_id, cycle_key]))
        with self.state.transaction():
            existing = self.state.connection.execute(
                "SELECT id FROM memory_cycles WHERE id=?", (cycle_id,)
            ).fetchone()
            if existing:
                item = self.get(cycle_id)
                if item["query"] != query or item["initial_identity"] != identity:
                    raise ConflictError("memory_cycle_key_reused")
                if (item["read_receipt"] or {}).get("error") != "memory_read_pending":
                    return item
                if skip_read_reason:
                    if item["work_status"] == "running":
                        item.update(
                            read_state="unavailable",
                            context={},
                            read_receipt={
                                "at": _now(),
                                "base_commit": None,
                                "sha256": None,
                                "sources": [],
                                "error": skip_read_reason,
                            },
                        )
                        self._save(item)
                    return item
            else:
                item = {
                    "id": cycle_id,
                    "project_id": project_id,
                    "cycle_key": cycle_key,
                    "query": query,
                    "initial_identity": identity,
                    "identity": identity.copy(),
                    "created_at": _now(),
                    "read_state": "unavailable",
                    "read_receipt": {
                        "at": _now(),
                        "base_commit": None,
                        "sha256": None,
                        "sources": [],
                        "error": skip_read_reason or "memory_read_pending",
                    },
                    "read_history": [],
                    "context": {},
                    "deliveries": [],
                    "delivery_receipt": None,
                    "work_status": "running",
                    "write_state": "not_requested",
                    "write_receipt": None,
                    "write_attempts": 0,
                    "next_attempt_at": 0,
                }
                self.state.connection.execute(
                    "INSERT INTO memory_cycles VALUES(?,?,?)",
                    (cycle_id, project_id, _json(item)),
                )
        # Complete a durable reservation left by a concurrent caller or crash.
        # Never hold the SQLite transaction while acquiring the vault lock.
        return self.retry_read(cycle_id, _initial_only=True)

    def retry_read(self, cycle_id: str, *, _initial_only: bool = False) -> dict:
        item = self.get(cycle_id)
        previous_receipt = item["read_receipt"]
        if _initial_only and (
            item["work_status"] != "running"
            or (previous_receipt or {}).get("error") != "memory_read_pending"
        ):
            return item
        if item["work_status"] != "running":
            raise ConflictError("memory_read_after_finish")
        if item["read_state"] != "unavailable":
            return item
        receipt: dict[str, Any] = {
            "at": _now(),
            "base_commit": None,
            "sha256": None,
            "sources": [],
        }
        context = {}
        try:
            with self.publisher.locked():
                self.publisher._recover()
                self.publisher._check_integrity()
                bundle = Context(self.publisher.vault).bundle(
                    item["project_id"], item["query"]
                )
                bundle["base_commit"] = self.publisher.git.head()
                if bundle["base_commit"] is None:
                    raise MemoryError("memory_vault_not_initialized")
                for doc in bundle["documents"]:
                    doc["sha256"] = digest(doc["content"])
                receipt.update(
                    base_commit=bundle["base_commit"],
                    sha256=digest(_json(bundle)),
                    sources=[
                        {"path": doc["path"], "sha256": doc["sha256"]}
                        for doc in bundle["documents"]
                    ],
                )
                context = {"bundle": bundle}
                read_state = "found" if bundle["documents"] else "empty"
        except Exception as exc:
            receipt["error"] = f"{type(exc).__name__}:{exc}"[:1000]
            read_state = "unavailable"
        with self.state.transaction():
            item = self.get(cycle_id)
            # Another caller may already have delivered a stable version.
            if (
                item["work_status"] != "running"
                or item["read_state"] != "unavailable"
                or item["read_receipt"] != previous_receipt
            ):
                return item
            item.update(read_state=read_state, read_receipt=receipt, context=context)
            item["read_history"].append({"state": read_state, **receipt})
            return self._save(item)

    def bind(self, cycle_id: str, identity: dict) -> dict:
        with self.state.transaction():
            item = self.get(cycle_id)
            for key, value in identity.items():
                if key in item["identity"] and item["identity"][key] != value:
                    raise ConflictError("memory_identity_rebound")
                if item["work_status"] != "running" and key not in item["identity"]:
                    raise ConflictError("memory_identity_after_finish")
            item["identity"].update(identity)
            return self._save(item)

    def delivered(
        self, cycle_id: str, thread_id: str, run_id: str | None, artifact: Any
    ) -> dict:
        with self.state.transaction():
            item = self.get(cycle_id)
            bound = item["identity"]
            if (
                bound.get("thread_id", thread_id) != thread_id
                or bound.get("run_id", run_id) != run_id
            ):
                raise ConflictError("memory_delivery_identity_mismatch")
            receipt = {
                "thread_id": thread_id,
                "run_id": run_id,
                "artifact": artifact,
                "context_sha256": (item["read_receipt"] or {}).get("sha256"),
                "read_receipt_sha256": digest(_json(item["read_receipt"])),
                "kind": "transport_delivery_only",
            }
            for old in item["deliveries"]:
                if old["artifact"] == artifact:
                    # A transport artifact must never be relabeled as delivering
                    # a newer context. Different artifacts keep separate receipts.
                    if (
                        old["context_sha256"] != receipt["context_sha256"]
                        or old.get(
                            "read_receipt_sha256", receipt["read_receipt_sha256"]
                        )
                        != receipt["read_receipt_sha256"]
                        or (
                            "read_receipt_sha256" not in old
                            and old["at"] < (item["read_receipt"] or {}).get("at", "")
                        )
                    ):
                        raise ConflictError("memory_delivery_reused")
                    if old["thread_id"] == thread_id and old["run_id"] == run_id:
                        return item
            if item["work_status"] != "running":
                raise ConflictError("memory_delivery_after_finish")
            item["deliveries"].append({**receipt, "at": _now()})
            item["delivery_receipt"] = item["deliveries"][-1]
            return self._save(item)

    def finish(self, cycle_id: str, status: str, evidence: dict) -> dict:
        if status not in {"succeeded", "failed", "stopped"}:
            raise MemoryError("invalid_memory_work_status")
        selected, serialized, rejection = report_projection(evidence)
        with self.state.transaction():
            item = self.get(cycle_id)
            if "report" in item:
                if (
                    item["work_status"] != status
                    or item["report"]["evidence"] != selected
                ):
                    raise ConflictError("memory_finish_reused")
                return item
            if item["work_status"] not in {"running", status}:
                raise ConflictError("memory_finish_reused")
            finished_at = item.get("finished_at", _now())
            if rejection:
                # Preserve terminal work independently of a rejected memory report.
                # The caller retains original evidence; this ledger stores a bounded
                # receipt and waits for an explicit corrected finish, without retries.
                record = {
                    "status": status,
                    "evidence_sha256": digest(serialized),
                    "bytes": len(serialized),
                    "error": rejection,
                }
                previous = item.get("finish_rejection", {})
                if any(previous.get(key) != value for key, value in record.items()):
                    record["at"] = _now()
                    item["finish_rejection"] = record
                    # Preserve the first rejected source even after a corrected finish.
                    item.setdefault("initial_finish_rejection", record)
                item.update(
                    work_status=status, write_state="blocked", finished_at=finished_at
                )
                return self._save(item)
            item.update(
                work_status=status, write_state="pending", finished_at=finished_at
            )
            item["report"] = {
                "schema_version": 1,
                "cycle_id": cycle_id,
                "project_id": item["project_id"],
                "identity": item["identity"],
                "work_status": status,
                "started_at": item["created_at"],
                "finished_at": finished_at,
                "read_state": item["read_state"],
                "read_receipt": item["read_receipt"],
                "deliveries": item["deliveries"],
                "evidence": selected,
                "scope": "selected_structured_report; not a conversation archive",
            }
            if item.get("initial_finish_rejection"):
                item["report"]["initial_finish_rejection"] = item[
                    "initial_finish_rejection"
                ]
                item["report"]["finish_rejection"] = item["finish_rejection"]
            item["report_sha256"] = digest(_json(item["report"]))
            return self._save(item)

    def _proposal(self, item: dict, raw: str) -> dict:
        page_id = "cycle-" + item["id"].removeprefix("cycle-")
        path = f"{corpus(item['project_id'])}/wiki/sources/{project_key(item['project_id'])}--{page_id}.md"
        # The source retains the complete selected report. The wiki page is a
        # bounded, readable extract, not a second copy of verbose test evidence.
        sections = []
        fields = (
            ("summary", "Resumo"),
            ("decisions", "Decisões"),
            ("changes", "Mudanças"),
            ("results", "Resultados"),
            ("limitations", "Limitações"),
            ("pending", "Pendências"),
            ("next_steps", "Próximos passos"),
        )
        for field, label in fields:
            value = item["report"]["evidence"].get(field)
            if value is None or value == [] or value == "":
                continue
            values = value if isinstance(value, list) else [value]
            paragraphs = []
            for part in values:
                text = part if isinstance(part, str) else _json(part)
                escaped = (
                    text.replace("[", "&#91;")
                    .replace("]", "&#93;")
                    .replace("<", "&lt;")
                    .replace("/", "&#47;")
                )
                paragraphs.append(("- " if isinstance(value, list) else "") + escaped)
            sections.append(f"## {label}\n\n" + "\n".join(paragraphs))
        summary = "\n\n".join(sections) or "Relatório estruturado disponível na fonte."
        if len(summary) > 12000:
            summary = (
                summary[:12000]
                + "\n\nSíntese abreviada; consulte a fonte para o conteúdo integral."
            )
        day = item["finished_at"][:10]
        from oraculo.presentation import markdown, plain

        evidence = item["report"]["evidence"]
        title = (
            plain(evidence.get("title") or evidence.get("summary"), 140)
            or "Memória da tarefa"
        )
        content = render_page(
            {
                "schema_version": 1,
                "page_id": page_id,
                "project_id": item["project_id"],
                "type": "source",
                "sources": [f"[[{raw}]]"],
                "tags": [],
                "aliases": [markdown(title)],
                "reviewed": False,
                "created": day,
                "updated": day,
            },
            f"# {markdown(title)}\n\nWork status: {item['work_status']}\n\n"
            f"Recorded: {item['finished_at']}\n\n{summary}\n\n"
            f"Structured evidence and provenance: [[{raw}]]\n",
        )
        return {
            "schema_version": 1,
            "batch_id": item["id"],
            "operation_id": item["id"],
            "project_id": item["project_id"],
            "base_commit": (item["read_receipt"] or {}).get("base_commit"),
            "source_hashes": {raw: item["report_sha256"]},
            "changes": [
                {
                    "page_id": page_id,
                    "path": path,
                    "base_sha256": None,
                    "content": content,
                }
            ],
            "citations": [f"[[{raw}]]"],
        }

    def publish_one(self, cycle_id: str) -> dict:
        item = self.get(cycle_id)
        if item["write_state"] == "saved":
            return item
        if item["write_state"] not in {"pending", "failed"}:
            raise MemoryError("memory_report_not_ready")
        try:
            if digest(_json(item["report"])) != item["report_sha256"]:
                raise ConflictError("memory_report_integrity_failure")
            raw = self.publisher.snapshot(
                item["project_id"],
                _json(item["report"]),
                {"cycle_id": cycle_id, **item["identity"]},
            )
            with self.state.transaction():
                item = self.get(cycle_id)
                if item["write_state"] == "saved":
                    return item
                if "proposal" not in item:
                    item["proposal"] = self._proposal(item, raw)
                item["write_attempts"] += 1
                self._save(item)
            receipt = self.publisher.publish(item["proposal"])
            if (
                receipt.get("state") != "confirmed"
                or receipt.get("operation_id") != cycle_id
                or not receipt.get("commit")
            ):
                raise MemoryError("memory_publication_unconfirmed")
            with self.state.transaction():
                item = self.get(cycle_id)
                if item["write_state"] == "saved":
                    return item
                item.update(
                    write_state="saved",
                    write_receipt={**receipt, "at": _now()},
                    write_error=None,
                    next_attempt_at=0,
                )
                return self._save(item)
        except Exception as exc:
            with self.state.transaction():
                item = self.get(cycle_id)
                if item["write_state"] == "saved":
                    return item
                # Failures before proposal construction also have bounded retry delay.
                item.update(
                    write_state="failed",
                    write_error=f"{type(exc).__name__}:{exc}"[:1000],
                    last_failure_at=_now(),
                    next_attempt_at=time.time()
                    + min(3600, 30 * 2 ** min(item["write_attempts"], 7)),
                )
                return self._save(item)

    def retry_write(self, cycle_id: str) -> dict:
        return self.publish_one(cycle_id)

    def publish_pending(self, limit: int = 10) -> list[dict]:
        if not 1 <= limit <= 100:
            raise MemoryError("invalid_memory_publish_limit")
        now = time.time()
        ready = [item for item in self.pending() if item["next_attempt_at"] <= now]
        return [self.publish_one(item["id"]) for item in ready[:limit]]
