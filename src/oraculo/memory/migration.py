"""Dry-run migration of legacy pages to explicit not_evaluated knowledge.

Apply is approved by the plan digest and writes only through Publisher.
Raw bytes, reviewed pages, typed claims, and page ids stay in place.
Nothing here promotes a claim to supported or talks to a live vault.
"""

from __future__ import annotations

import base64
import json
from datetime import date
from pathlib import PurePosixPath
from typing import Any

from pydantic import ValidationError

from . import publish as publish_mod
from .context import Context
from .publish import Publisher
from .schema import (
    ConflictError,
    MemoryError,
    MemoryProposal,
    corpus,
    digest,
    parse_page,
    project_key,
    render_page,
    safe_path,
)

MAX_PAGES = 20
MAX_BYTES = 2 * 1024 * 1024
_FOLDERS = {"entities", "concepts", "sources"}
_DIGEST_EXCLUDED = {"plan_hash", "approval"}


def migration_digest(plan: dict[str, Any]) -> str:
    """Stable digest of a plan. Approval must echo plan_hash, not this call's input approval."""
    body = {key: value for key, value in plan.items() if key not in _DIGEST_EXCLUDED}
    try:
        payload = json.dumps(
            body, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        )
    except (TypeError, ValueError):
        raise MemoryError("migration_plan_invalid") from None
    return digest(payload)


def plan_migration(publisher: Publisher, project_id: str) -> dict[str, Any]:
    """Read-only inventory. Repeated calls on an unchanged vault match."""
    project_key(project_id)
    with publisher.locked():
        files = Context(publisher.vault).files(project_id)
        head = publisher.git.head()
    plan = _build_plan(project_id, files, head)
    plan["plan_hash"] = migration_digest(plan)
    return plan


def apply_migration(publisher: Publisher, plan: dict[str, Any]) -> dict[str, Any]:
    """Apply an approved plan. Replay returns the same publisher receipts."""
    project_id = _validated_plan(plan)
    batches = plan["batches"]
    _assert_stable(publisher, project_id, plan, applied=_applied_paths(publisher, plan))
    receipts = []
    for batch in batches:
        proposal = _batch_proposal(batch)
        _enforce_limits(proposal)
        if not _already_applied(publisher, proposal):
            for change in proposal["changes"]:
                _guard_change(publisher, project_id, change)
        receipts.append(publisher.publish(proposal))
    _assert_stable(publisher, project_id, plan, applied=set(), migrated=True)
    return {
        "state": "confirmed",
        "project_id": project_id,
        "plan_hash": plan["plan_hash"],
        "receipts": receipts,
        "pending_review": list(plan.get("pending_review") or []),
        "rollback": plan.get("rollback") or {"prepared": True, "restores": []},
    }


def _build_plan(
    project_id: str, files: dict[str, str], head: str | None
) -> dict[str, Any]:
    prefix = corpus(project_id)
    selected = [path for path in sorted(files) if _is_wiki_page(path, project_id)]
    raw_hashes = {
        path: digest(files[path])
        for path in sorted(files)
        if _is_raw_path(path, prefix)
    }
    operations: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    preserved: list[dict[str, Any]] = []
    deferred: list[dict[str, Any]] = []
    for path in selected:
        try:
            meta, body = parse_page(files[path])
        except MemoryError:
            deferred.append(_deferred(path, path, None, "invalid_page"))
            continue
        page_id = meta.get("page_id") if isinstance(meta.get("page_id"), str) else path
        base = digest(files[path])
        version = meta.get("knowledge_schema", None)
        if "knowledge_schema" in meta or "claims" in meta:
            if version != 1 or not isinstance(meta.get("claims"), list):
                reason = (
                    "schema_downgrade_refused"
                    if isinstance(version, int) and version != 1
                    else "invalid_knowledge_schema"
                )
                deferred.append(_deferred(path, page_id, base, reason))
                continue
            reason = (
                "supported_preserved"
                if _has_support(meta.get("claims"), "supported")
                else "already_typed"
            )
            preserved.append(
                {
                    "path": path,
                    "page_id": page_id,
                    "base_sha256": base,
                    "reason": reason,
                }
            )
            continue
        proposed = _mark_not_evaluated(meta, body)
        try:
            parsed, _parsed_body = parse_page(proposed)
            # Keep the original reviewed flag. A reviewed page is pending, not a
            # failed identity check, and is never written by this plan.
            if parsed.get("page_id") != page_id or parsed.get("reviewed") != meta.get(
                "reviewed"
            ):
                raise MemoryError("migration_identity_changed")
        except MemoryError as exc:
            deferred.append(_deferred(path, page_id, base, str(exc)))
            continue
        record = {
            "page_id": page_id,
            "path": path,
            "base_sha256": base,
            "content": proposed,
        }
        if meta.get("reviewed") is True:
            pending.append(
                {
                    "path": path,
                    "page_id": page_id,
                    "base_sha256": base,
                    "reason": "reviewed_page_protected",
                    "proposed_sha256": digest(proposed),
                }
            )
            continue
        operations.append(record)
    batches, oversized = _pack(project_id, head, operations)
    deferred.extend(oversized)
    operation_paths = {item["path"] for item in operations} - {
        item["path"] for item in oversized
    }
    restores = [
        {
            "path": change["path"],
            "page_id": change["page_id"],
            "base_sha256": change["base_sha256"],
            "operation_id": batch["operation_id"],
        }
        for batch in batches
        for change in batch["proposal"]["changes"]
    ]
    base_hashes = {
        path: digest(files[path])
        for path in sorted(files)
        if path in raw_hashes
        or path in operation_paths
        or any(item["path"] == path for item in pending)
        or any(item["path"] == path for item in preserved)
        or any(item["path"] == path for item in deferred)
    }
    return {
        "kind": "knowledge-migration-v1",
        "project_id": project_id,
        "base_commit": head,
        "limits": {"max_pages": MAX_PAGES, "max_bytes": MAX_BYTES},
        "limitations": [
            "legacy_marked_not_evaluated_not_supported",
            "reviewed_pages_pending_not_edited",
            "temporal_lint_left_to_freshness",
            "no_live_vault_migration",
        ],
        "base_hashes": base_hashes,
        "raw_hashes": raw_hashes,
        "preserved": preserved,
        "pending_review": pending,
        "deferred": deferred,
        "operations": [
            {
                "path": item["path"],
                "page_id": item["page_id"],
                "base_sha256": item["base_sha256"],
            }
            for item in operations
            if item["path"] in operation_paths
        ],
        "batches": batches,
        "rollback": {"prepared": True, "restores": restores},
    }


def _pack(
    project_id: str, head: str | None, operations: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    batches: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []
    deferred: list[dict[str, Any]] = []

    def flush() -> None:
        nonlocal current
        if not current:
            return
        batches.append(_batch(project_id, head, current, len(batches)))
        current = []

    for change in operations:
        trial = current + [change]
        if (
            len(trial) > MAX_PAGES
            or _proposal_bytes(project_id, head, trial, len(batches)) > MAX_BYTES
        ):
            flush()
            alone = _proposal_bytes(project_id, head, [change], len(batches))
            if alone > MAX_BYTES:
                deferred.append(
                    _deferred(
                        change["path"],
                        change["page_id"],
                        change["base_sha256"],
                        "proposal_too_large",
                    )
                )
                continue
            current = [change]
        else:
            current = trial
    flush()
    return batches, deferred


def _batch(
    project_id: str, head: str | None, changes: list[dict[str, Any]], index: int
) -> dict[str, Any]:
    operation_id = _operation_id(project_id, changes, index)
    return {
        "operation_id": operation_id,
        "proposal": _proposal(project_id, operation_id, head, changes),
    }


def _proposal(
    project_id: str,
    operation_id: str,
    head: str | None,
    changes: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "batch_id": operation_id,
        "operation_id": operation_id,
        "project_id": project_id,
        "base_commit": head,
        "source_hashes": {},
        "changes": [
            {
                "page_id": change["page_id"],
                "path": change["path"],
                "base_sha256": change["base_sha256"],
                "content": change["content"],
            }
            for change in changes
        ],
        "citations": [],
        "findings": [
            {
                "code": "not_evaluated",
                "severity": "info",
                "project_id": project_id,
                "page_id": change["page_id"],
                "path": change["path"],
                "support": "not_evaluated",
                "reason": "migration_legacy",
            }
            for change in changes
        ],
    }


def _proposal_bytes(
    project_id: str, head: str | None, changes: list[dict[str, Any]], index: int
) -> int:
    operation_id = _operation_id(project_id, changes, index)
    model = MemoryProposal.model_validate(
        _proposal(project_id, operation_id, head, changes)
    )
    return len(model.model_dump_json().encode())


def _operation_id(project_id: str, changes: list[dict[str, Any]], index: int) -> str:
    payload = json.dumps(
        {
            "changes": [
                {
                    "base_sha256": change["base_sha256"],
                    "content_sha256": digest(change["content"]),
                    "page_id": change["page_id"],
                    "path": change["path"],
                }
                for change in changes
            ],
            "index": index,
            "project_id": project_id,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return "m" + digest(payload)[:30]


def _mark_not_evaluated(meta: dict[str, Any], body: str) -> str:
    marked = dict(meta)
    marked["knowledge_schema"] = 1
    marked["claims"] = []
    return render_page(marked, body)


def _validated_plan(plan: Any) -> str:
    """Shape, batch limits, and digest. No model validation and no writes."""
    if not isinstance(plan, dict):
        raise MemoryError("migration_plan_invalid")
    approval = plan.get("approval")
    plan_hash = plan.get("plan_hash")
    if not isinstance(approval, str) or approval != plan_hash:
        raise MemoryError("migration_approval_required")
    if plan.get("kind") != "knowledge-migration-v1":
        raise MemoryError("migration_plan_invalid")
    project_id = plan.get("project_id")
    if not isinstance(project_id, str):
        raise MemoryError("migration_plan_invalid")
    batches = plan.get("batches")
    if not isinstance(batches, list):
        raise MemoryError("migration_plan_invalid")
    for batch in batches:
        _bounded_batch(batch, project_id)
    if migration_digest(plan) != plan_hash:
        raise MemoryError("migration_plan_tampered")
    project_key(project_id)
    return project_id


def _bounded_batch(batch: Any, project_id: str) -> None:
    if not isinstance(batch, dict):
        raise MemoryError("migration_plan_invalid")
    operation_id = batch.get("operation_id")
    proposal = batch.get("proposal")
    if not isinstance(operation_id, str) or not isinstance(proposal, dict):
        raise MemoryError("migration_plan_invalid")
    if proposal.get("operation_id") != operation_id:
        raise MemoryError("migration_plan_tampered")
    if proposal.get("project_id") != project_id or proposal.get("schema_version") != 1:
        raise MemoryError("migration_plan_invalid")
    changes = proposal.get("changes")
    if not isinstance(changes, list) or not 1 <= len(changes) <= MAX_PAGES:
        raise MemoryError("migration_batch_limit")
    for change in changes:
        if not isinstance(change, dict):
            raise MemoryError("migration_plan_invalid")
        content = change.get("content")
        path = change.get("path")
        page_id = change.get("page_id")
        if (
            not isinstance(content, str)
            or not isinstance(path, str)
            or not isinstance(page_id, str)
        ):
            raise MemoryError("migration_plan_invalid")
        if "/raw/" in path:
            raise MemoryError("raw_immutable")
    _json_size(proposal, MAX_BYTES)


def _json_size(value: Any, ceiling: int) -> int:
    size = _json_size_at(value, ceiling)
    if size > ceiling:
        raise MemoryError("migration_batch_limit")
    return size


def _json_size_at(value: Any, ceiling: int) -> int:
    """Compact UTF-8 JSON size. Stops once the value cannot fit under ceiling."""
    if isinstance(value, str):
        if len(value.encode("utf-8")) + 2 > ceiling:
            return ceiling + 1
        return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))
    if value is None:
        return 4
    if isinstance(value, bool):
        return 4 if value else 5
    if isinstance(value, int):
        return len(str(value))
    if isinstance(value, float):
        return len(json.dumps(value).encode("utf-8"))
    if isinstance(value, list):
        total = 2
        for index, item in enumerate(value):
            if index:
                total += 1
            total += _json_size_at(item, ceiling)
            if total > ceiling:
                return total
        return total
    if isinstance(value, dict):
        total = 2
        for index, (key, item) in enumerate(value.items()):
            if not isinstance(key, str):
                raise MemoryError("migration_plan_invalid")
            if index:
                total += 1
            total += len(json.dumps(key, ensure_ascii=False).encode("utf-8")) + 1
            total += _json_size_at(item, ceiling)
            if total > ceiling:
                return total
        return total
    raise MemoryError("migration_plan_invalid")


def _proposal_model(proposal: dict[str, Any]) -> MemoryProposal:
    try:
        model = MemoryProposal.model_validate(proposal)
    except ValidationError:
        raise MemoryError("migration_plan_invalid") from None
    if len(model.model_dump_json().encode()) > MAX_BYTES:
        raise MemoryError("migration_batch_limit")
    return model


def _canonical_frontmatter(meta: dict[str, Any]) -> dict[str, Any]:
    def norm(value: Any) -> Any:
        if isinstance(value, date):
            return value.isoformat()
        if isinstance(value, dict):
            return {key: norm(item) for key, item in value.items()}
        if isinstance(value, list):
            return [norm(item) for item in value]
        return value

    return {key: norm(value) for key, value in meta.items()}


def _publisher_stamp() -> str:
    # Same clock Publisher uses when it rewrites updated during commit.
    return publish_mod.date.today().isoformat()


def _journal_before(publisher: Publisher, operation_id: str, path: str) -> str:
    journal_path = publisher.journals / f"{operation_id}.json"
    try:
        stored = json.loads(journal_path.read_bytes().decode("utf-8"))
        encoded = stored.get("before", {}).get(path)
        if not isinstance(encoded, str):
            raise MemoryError("migration_identity_changed")
        return base64.b64decode(encoded, validate=True).decode("utf-8")
    except MemoryError:
        raise
    except (OSError, json.JSONDecodeError, UnicodeError, ValueError, TypeError):
        raise MemoryError("migration_identity_changed") from None


def _batch_proposal(batch: Any) -> dict[str, Any]:
    if not isinstance(batch, dict) or not isinstance(batch.get("proposal"), dict):
        raise MemoryError("migration_plan_invalid")
    proposal = batch["proposal"]
    if proposal.get("operation_id") != batch.get("operation_id"):
        raise MemoryError("migration_plan_tampered")
    return proposal


def _enforce_limits(proposal: dict[str, Any]) -> None:
    changes = proposal.get("changes")
    if not isinstance(changes, list) or not 1 <= len(changes) <= MAX_PAGES:
        raise MemoryError("migration_batch_limit")
    _proposal_model(proposal)


def _already_applied(publisher: Publisher, proposal: dict[str, Any]) -> bool:
    model = _proposal_model(proposal)
    payload = digest(model.model_dump_json())
    journal = publisher.journals / f"{model.operation_id}.json"
    if not journal.is_file():
        return False
    stored = json.loads(journal.read_bytes().decode("utf-8"))
    if stored.get("payload_hash") != payload:
        raise ConflictError("operation_id_reused")
    if stored.get("state") != "confirmed":
        publisher.recover()
        stored = json.loads(journal.read_bytes().decode("utf-8"))
        if stored.get("state") != "confirmed":
            raise ConflictError("operation_pending_recovery")
    return True


def _guard_change(
    publisher: Publisher, project_id: str, change: dict[str, Any]
) -> None:
    path = change["path"]
    if not _is_wiki_page(path, project_id):
        raise MemoryError("invalid_page_path")
    if "/raw/" in path:
        raise MemoryError("raw_immutable")
    target = safe_path(publisher.vault, path)
    if not target.is_file():
        raise ConflictError("base_hash_mismatch")
    current = target.read_bytes().decode("utf-8")
    if digest(current) != change["base_sha256"]:
        raise ConflictError("base_hash_mismatch")
    meta, body = parse_page(current)
    if meta.get("reviewed") is True:
        raise ConflictError("reviewed_page_protected")
    if meta.get("page_id") != change["page_id"]:
        raise MemoryError("page_id_changed")
    # Typed pages stay as written. Legacy means both keys are absent.
    if "knowledge_schema" in meta or "claims" in meta:
        if "knowledge_schema" in meta and meta.get("knowledge_schema") != 1:
            raise MemoryError("schema_downgrade_refused")
        raise MemoryError("migration_downgrade_refused")
    new_meta, new_body = parse_page(change["content"])
    if new_meta.get("page_id") != meta.get("page_id"):
        raise MemoryError("page_id_changed")
    if new_meta.get("project_id") != project_id:
        raise MemoryError("project_identity_mismatch")
    if new_meta.get("schema_version") != 1:
        raise MemoryError("schema_downgrade_refused")
    if new_meta.get("reviewed") is not False:
        raise ConflictError("reviewed_page_protected")
    if new_body != body:
        raise MemoryError("migration_body_changed")
    if new_meta.get("knowledge_schema") != 1 or new_meta.get("claims") != []:
        raise MemoryError("migration_promotion_refused")
    expected = dict(meta)
    expected["knowledge_schema"] = 1
    expected["claims"] = []
    if new_meta != expected:
        raise MemoryError("migration_identity_changed")


def _applied_paths(publisher: Publisher, plan: dict[str, Any]) -> set[str]:
    applied = set()
    for batch in plan.get("batches") or []:
        if not isinstance(batch, dict):
            continue
        proposal = batch.get("proposal")
        if isinstance(proposal, dict) and _already_applied(publisher, proposal):
            for change in proposal.get("changes") or []:
                applied.add(change["path"])
    return applied


def _assert_stable(
    publisher: Publisher,
    project_id: str,
    plan: dict[str, Any],
    *,
    applied: set[str],
    migrated: bool = False,
) -> None:
    files = Context(publisher.vault).files(project_id)
    for path, expected in (plan.get("raw_hashes") or {}).items():
        if path not in files or digest(files[path]) != expected:
            raise ConflictError("raw_changed")
    for collection, code in (
        ("pending_review", "reviewed_changed"),
        ("preserved", "concurrent_edit"),
    ):
        for item in plan.get(collection) or []:
            path = item["path"]
            if path not in files or digest(files[path]) != item["base_sha256"]:
                raise ConflictError(code)
    for batch in plan.get("batches") or []:
        for change in batch["proposal"]["changes"]:
            path = change["path"]
            if not migrated and path in applied:
                continue
            if path not in files:
                raise ConflictError("base_hash_mismatch")
            if not migrated:
                if digest(files[path]) != change["base_sha256"]:
                    raise ConflictError("base_hash_mismatch")
                continue
            _assert_migrated_page(
                publisher, batch.get("operation_id"), change, files[path]
            )


def _assert_migrated_page(
    publisher: Publisher, operation_id: Any, change: dict[str, Any], stored_text: str
) -> None:
    if not isinstance(operation_id, str):
        raise MemoryError("migration_plan_invalid")
    meta, body = parse_page(stored_text)
    if meta.get("page_id") != change.get("page_id"):
        raise MemoryError("page_id_changed")
    if meta.get("reviewed") is not False:
        raise ConflictError("reviewed_page_protected")
    if meta.get("schema_version") != 1:
        raise MemoryError("schema_downgrade_refused")
    if meta.get("knowledge_schema") != 1 or meta.get("claims") != []:
        raise MemoryError("migration_promotion_refused")
    before = _journal_before(publisher, operation_id, change["path"])
    old_meta, old_body = parse_page(before)
    if "knowledge_schema" in old_meta or "claims" in old_meta:
        raise MemoryError("migration_downgrade_refused")
    if body != old_body:
        raise MemoryError("migration_body_changed")
    expected = dict(old_meta)
    expected["knowledge_schema"] = 1
    expected["claims"] = []
    stored = _canonical_frontmatter(meta)
    wanted = _canonical_frontmatter(expected)
    stored_updated = stored.pop("updated", None)
    wanted_updated = wanted.pop("updated", None)
    if stored != wanted:
        raise MemoryError("migration_identity_changed")
    # Publisher always rewrites updated. Any other date is a real edit.
    if stored_updated not in {wanted_updated, _publisher_stamp()}:
        raise MemoryError("migration_identity_changed")


def _is_wiki_page(path: str, project_id: str) -> bool:
    relative = PurePosixPath(path)
    prefix = corpus(project_id)
    return (
        relative.parent.name in _FOLDERS
        and relative.parent.parent.as_posix() == f"{prefix}/wiki"
        and relative.name.startswith(project_key(project_id) + "--")
        and relative.suffix == ".md"
    )


def _is_raw_path(path: str, prefix: str) -> bool:
    parent = PurePosixPath(path).parent.as_posix()
    return parent in {f"{prefix}/raw", "shared/raw"} and path.endswith(".md")


def _has_support(claims: Any, support: str) -> bool:
    if not isinstance(claims, list):
        return False
    return any(
        isinstance(item, dict) and item.get("support") == support for item in claims
    )


def _deferred(path: str, page_id: str, base: str | None, reason: str) -> dict[str, Any]:
    item = {"path": path, "page_id": page_id, "reason": reason}
    if base is not None:
        item["base_sha256"] = base
    return item
