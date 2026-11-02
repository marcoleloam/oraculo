"""Explicit local capture and derived answers; Publisher is the only vault writer."""

from __future__ import annotations

import os
import stat
from datetime import datetime, timezone
from pathlib import Path

from oraculo.memory.context import Context
from oraculo.memory.schema import (
    ConflictError,
    MemoryError,
    corpus,
    digest,
    parse_page,
    project_key,
    render_page,
)
from oraculo.protocol import content_hash

MAX_CAPTURE_BYTES = 2 * 1024 * 1024


def _selected_file(root: Path, value: str) -> tuple[Path, os.stat_result]:
    root = Path(root).resolve()
    path = Path(value).expanduser().absolute()
    if ".." in path.parts:
        raise MemoryError("capture_file_outside_project")
    if path.is_symlink():
        raise MemoryError("capture_symlink")
    canonical = path.resolve(strict=True)
    if not canonical.is_relative_to(root) or canonical == root:
        raise MemoryError("capture_file_outside_project")
    for item in (path, *path.parents):
        if item.is_symlink():
            # System aliases above the registered root (e.g. macOS /tmp) do
            # not select extra content. Links inside the project remain refused.
            if (
                item == path
                or item.parent.resolve().is_relative_to(root)
                or not root.is_relative_to(item.resolve())
            ):
                raise MemoryError("capture_symlink")
        if item.resolve() == root:
            break
    path = canonical
    info = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode):
        raise MemoryError("capture_regular_file_required")
    if path.suffix.lower() not in {".md", ".txt"}:
        raise MemoryError("capture_markdown_or_text_required")
    return path, info


def _stamp(info: os.stat_result) -> dict:
    return {
        "bytes": info.st_size,
        "mtime_ns": info.st_mtime_ns,
        "ctime_ns": info.st_ctime_ns,
        "device": info.st_dev,
        "inode": info.st_ino,
    }


def capture_plan(
    project_id: str,
    project_root: Path,
    paths: list[str],
    *,
    source_kind: str = "document",
    sanitized: bool = False,
) -> dict:
    """Stat exactly the selected files. Do not read or collect their content."""
    project_key(project_id)
    if type(sanitized) is not bool:
        raise MemoryError("capture_sanitized_boolean_required")
    if source_kind not in {"document", "selected_chat"}:
        raise MemoryError("capture_source_kind")
    if source_kind == "selected_chat" and not sanitized:
        raise MemoryError("mixed_chat_requires_selection_and_sanitization")
    if not isinstance(paths, list) or not 1 <= len(paths) <= 20:
        raise MemoryError("capture_select_one_to_twenty_files")
    files = []
    for value in paths:
        if not isinstance(value, str):
            raise MemoryError("capture_file_path_required")
        path, info = _selected_file(project_root, value)
        files.append({"path": str(path), **_stamp(info)})
    files.sort(key=lambda item: item["path"])
    if len({item["path"] for item in files}) != len(files):
        raise MemoryError("capture_duplicate_file")
    if sum(item["bytes"] for item in files) > MAX_CAPTURE_BYTES:
        raise MemoryError("capture_too_large")
    body = {
        "schema_version": 1,
        "project_id": project_id,
        "source_kind": source_kind,
        "sanitized": sanitized,
        "files": files,
        "content_collected": False,
    }
    return body | {
        "manifest_sha256": content_hash(body),
        "state": "confirmation_required",
    }


def capture_apply(
    publisher, project_id: str, project_root: Path, plan: dict, confirmation: str
) -> dict:
    if not isinstance(plan, dict) or plan.get("project_id") != project_id:
        raise MemoryError("capture_project_mismatch")
    body = {
        key: value
        for key, value in plan.items()
        if key not in {"state", "manifest_sha256"}
    }
    if (
        plan.get("schema_version") != 1
        or confirmation != plan.get("manifest_sha256")
        or confirmation != content_hash(body)
    ):
        raise MemoryError("capture_manifest_confirmation_required")
    refreshed = capture_plan(
        project_id,
        project_root,
        [item["path"] for item in plan["files"]],
        source_kind=plan["source_kind"],
        sanitized=plan["sanitized"],
    )
    if refreshed["manifest_sha256"] != confirmation:
        raise ConflictError("capture_selection_changed")
    collected = []
    for item in plan["files"]:
        path, _ = _selected_file(project_root, item["path"])
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as stream:
            before = os.fstat(stream.fileno())
            expected = {key: item[key] for key in _stamp(before)}
            if _stamp(before) != expected:
                raise ConflictError("capture_selection_changed")
            raw = stream.read(MAX_CAPTURE_BYTES + 1)
            after = os.fstat(stream.fileno())
        if (
            _stamp(before) != expected
            or _stamp(after) != expected
            or len(raw) != item["bytes"]
        ):
            raise ConflictError("capture_selection_changed")
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise MemoryError("capture_utf8_required") from None
        if len(raw) > MAX_CAPTURE_BYTES:
            raise MemoryError("capture_too_large")
        collected.append((item, content))
    # Validate the complete selection before creating any immutable snapshot.
    result = []
    for item, content in collected:
        path = publisher.snapshot(
            project_id,
            content,
            {
                "kind": "explicit_selected_capture",
                "source_kind": plan["source_kind"],
                "manifest_sha256": confirmation,
                "filename": Path(item["path"]).name,
            },
        )
        result.append(
            {"path": path, "sha256": digest(content), "bytes": len(content.encode())}
        )
    return {
        "state": "captured",
        "project_id": project_id,
        "manifest_sha256": confirmation,
        "sources": result,
    }


def capture_text_plan(
    project_id: str, text: str, *, source_kind="document", sanitized=False
) -> dict:
    project_key(project_id)
    if type(sanitized) is not bool:
        raise MemoryError("capture_sanitized_boolean_required")
    if (
        not isinstance(text, str)
        or not text.strip()
        or len(text.encode()) > MAX_CAPTURE_BYTES
    ):
        raise MemoryError("capture_text_size")
    if source_kind not in {"document", "selected_chat"}:
        raise MemoryError("capture_source_kind")
    if source_kind == "selected_chat" and not sanitized:
        raise MemoryError("mixed_chat_requires_selection_and_sanitization")
    body = {
        "schema_version": 1,
        "project_id": project_id,
        "sha256": digest(text),
        "bytes": len(text.encode()),
        "source_kind": source_kind,
        "sanitized": sanitized,
    }
    return body | {
        "state": "confirmation_required",
        "manifest_sha256": content_hash(body),
    }


def capture_text_apply(
    publisher, project_id: str, text: str, confirmation: str, **options
) -> dict:
    plan = capture_text_plan(project_id, text, **options)
    if confirmation != plan["manifest_sha256"]:
        raise MemoryError("capture_manifest_confirmation_required")
    raw = publisher.snapshot(
        project_id,
        text,
        {"kind": "explicit_text_capture", "manifest_sha256": confirmation},
    )
    return {
        "state": "captured",
        "project_id": project_id,
        "sources": [{"path": raw, "sha256": digest(text), "bytes": len(text.encode())}],
        "manifest_sha256": confirmation,
    }


def _evidence(
    files: dict[str, str], project_id: str, evidence: list[dict]
) -> list[dict]:
    if not isinstance(evidence, list) or len(evidence) > 20:
        raise MemoryError("answer_evidence_limit")
    accepted = []
    for span in evidence:
        if not isinstance(span, dict):
            raise MemoryError("answer_evidence_required")
        path = span.get("path")
        if (
            not isinstance(path, str)
            or not path.startswith((corpus(project_id) + "/raw/", "shared/raw/"))
            or path not in files
        ):
            raise MemoryError("answer_evidence_outside_scope")
        raw = files[path].encode()
        start, end = span.get("start"), span.get("end")
        if (
            type(start) is not int
            or type(end) is not int
            or not 0 <= start < end <= len(raw)
        ):
            raise MemoryError("answer_evidence_locator")
        if span.get("sha256") != digest(raw) or Path(path).stem != digest(raw):
            raise MemoryError("answer_evidence_hash")
        quote = span.get("quote")
        if not isinstance(quote, str) or raw[start:end] != quote.encode():
            raise MemoryError("answer_evidence_quote")
        accepted.append(dict(span))
    return accepted


def save_answer(
    publisher,
    project_id: str,
    *,
    title: str,
    text: str,
    evidence: list[dict],
    as_hypothesis=False,
    apply=False,
) -> dict:
    """Return a proposal unless explicitly saving; no automatic semantic support."""
    if type(as_hypothesis) is not bool or type(apply) is not bool:
        raise MemoryError("answer_explicit_boolean_required")
    if not isinstance(title, str) or not title.strip() or len(title) > 300:
        raise MemoryError("answer_title_required")
    if not isinstance(text, str) or not text.strip() or len(text.encode()) > 512 * 1024:
        raise MemoryError("answer_text_required")
    with publisher.locked():
        publisher._check_integrity()
        files = Context(publisher.vault).files(project_id)
        sources = _evidence(files, project_id, evidence)
        if not sources and not as_hypothesis:
            raise MemoryError("answer_sources_or_explicit_hypothesis_required")
        fingerprint = content_hash(
            {
                "project_id": project_id,
                "title": title,
                "text": text,
                "evidence": sources,
                "hypothesis": bool(as_hypothesis),
            }
        )
        page_id = project_key(project_id) + "--answer-" + digest(title)[:20]
        path = f"{corpus(project_id)}/wiki/concepts/{page_id}.md"
        previous = files.get(path)
        if previous:
            old, _ = parse_page(previous)
            if old.get("answer_fingerprint") == fingerprint:
                return {
                    "state": "no_changes",
                    "project_id": project_id,
                    "path": path,
                    "operation_id": "answer-" + fingerprint,
                }
            if old.get("reviewed"):
                return {
                    "state": "review_required",
                    "project_id": project_id,
                    "path": path,
                    "findings": [{"code": "reviewed_page_protected"}],
                }
        now = datetime.now(timezone.utc).isoformat()
        claim = {
            "id": "answer-" + fingerprint[:24],
            "project_id": project_id,
            "text": text,
            "kind": "hypothesis" if as_hypothesis else "fact",
            "category": "inference",
            "support": "source_linked" if sources else "not_evaluated",
            "evidence": sources,
            "recorded_at": now,
        }
        metadata = {
            "schema_version": 1,
            "knowledge_schema": 1,
            "page_id": page_id,
            "project_id": project_id,
            "type": "concept",
            "sources": list(
                dict.fromkeys("[[" + item["path"] + "]]" for item in sources)
            ),
            "tags": [],
            "aliases": [title],
            "reviewed": False,
            "created": now[:10],
            "updated": now[:10],
            "answer_fingerprint": fingerprint,
            "derivation": "explicit_saved_answer",
            "claims": [claim],
        }
        content = render_page(
            metadata,
            "# "
            + title
            + "\n\n"
            + text
            + "\n\nEstado de suporte: "
            + claim["support"]
            + ". Resposta derivada; não é corroboração independente de si mesma.\n",
        )
        proposal = {
            "schema_version": 1,
            "project_id": project_id,
            "batch_id": "answer-" + fingerprint,
            "operation_id": "answer-" + fingerprint,
            "base_commit": publisher.git.head(),
            "source_hashes": {item["path"]: item["sha256"] for item in sources},
            "changes": [
                {
                    "page_id": page_id,
                    "path": path,
                    "base_sha256": digest(previous) if previous else None,
                    "content": content,
                }
            ],
            "citations": metadata["sources"],
            "findings": [{"code": "semantic_support_not_automatically_assessed"}],
        }
    if not apply:
        return {
            "state": "proposal",
            "project_id": project_id,
            "path": path,
            "proposal": proposal,
        }
    receipt = publisher.publish(proposal)
    return {
        "state": "saved",
        "project_id": project_id,
        "path": path,
        "receipt": receipt,
    }


def knowledge_report(publisher, project_id: str) -> dict:
    with publisher.locked():
        publisher._check_integrity()
        files = Context(publisher.vault).files(project_id)
    entries = []
    for path, content in sorted(files.items()):
        if "/wiki/" not in path or "/schema/" in path:
            continue
        try:
            meta, _ = parse_page(content)
        except MemoryError:
            continue
        claims = meta.get("claims") or []
        entries.append(
            {
                "path": path,
                "project_id": meta.get("project_id"),
                "reviewed": meta.get("reviewed") is True,
                "knowledge_schema": meta.get("knowledge_schema"),
                "claims": [
                    {
                        key: claim.get(key)
                        for key in (
                            "id",
                            "text",
                            "kind",
                            "category",
                            "support",
                            "observed_at",
                            "recorded_at",
                            "valid_from",
                            "valid_until",
                            "supersedes",
                        )
                    }
                    for claim in claims
                    if isinstance(claim, dict)
                ],
            }
        )
    return {
        "schema_version": 1,
        "project_id": project_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "documents": len(files),
        "entries": entries,
        "claim_counts": {
            support: sum(
                claim.get("support") == support
                for entry in entries
                for claim in entry["claims"]
            )
            for support in (
                "not_evaluated",
                "source_linked",
                "insufficient",
                "contested",
                "supported",
                "retired",
            )
        },
        "canonical_changed": False,
        "limitations": [
            "Relatório determinístico; estados de suporte são declarações atribuídas, não verdade automaticamente verificada."
        ],
    }


def validate_nightly_proposal(publisher, project_id, proposal, selected):
    """New nightly evidence must belong to its frozen selection; old claims stay."""
    if (
        not isinstance(selected, list)
        or not 1 <= len(selected) <= 20
        or not all(isinstance(path, str) for path in selected)
    ):
        raise MemoryError("knowledge_routine_selection_required")
    with publisher.locked():
        publisher._check_integrity()
        files = Context(publisher.vault).files(project_id)
    frozen = set(selected)
    if any(
        not isinstance(path, str)
        or not path.startswith(corpus(project_id) + "/raw/")
        or path not in files
        for path in selected
    ):
        raise MemoryError("knowledge_routine_selection_outside_scope")
    for change in proposal.get("changes", []):
        metadata, _ = parse_page(change["content"])
        claims = metadata.get("claims")
        if (
            metadata.get("knowledge_schema") != 1
            or not isinstance(claims, list)
            or not claims
        ):
            raise MemoryError("knowledge_routine_requires_typed_claims")
        previous = files.get(change["path"])
        old_claims = parse_page(previous)[0].get("claims", []) if previous else []
        if not isinstance(old_claims, list):
            raise MemoryError("knowledge_routine_existing_claims_invalid")
        for claim in claims:
            if claim in old_claims:
                continue
            if not isinstance(claim, dict):
                raise MemoryError("knowledge_routine_requires_typed_claims")
            evidence = claim.get("evidence", [])
            if not isinstance(evidence, list) or any(
                not isinstance(span, dict) or span.get("path") not in frozen
                for span in evidence
            ):
                raise MemoryError("knowledge_routine_evidence_outside_selection")
            if claim.get("support") == "supported":
                raise MemoryError("knowledge_routine_semantic_review_required")
