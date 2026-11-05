"""Incremental concept compilation from explicitly selected raw sources.

The compiler proposes one concept page per project and exact concept string.
It never writes the vault itself: ``apply=True`` goes through ``Publisher``
only. Semantic support is not granted here.
"""

from __future__ import annotations

import json
import re
from pathlib import PurePosixPath
from typing import Any

from oraculo.memory.claims import _coerce_date
from oraculo.memory.schema import (
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

MAX_SOURCES = 20
MAX_PAGES = 20
MAX_PROPOSAL_BYTES = 2 * 1024 * 1024
PLACEHOLDER_DATE = "1970-01-01"
CLAIM_FIELDS = (
    "id",
    "project_id",
    "text",
    "kind",
    "category",
    "support",
    "evidence",
    "assessment",
    "observed_at",
    "recorded_at",
    "valid_from",
    "valid_until",
    "supersedes",
    "subject",
    "predicate",
    "value",
)
SPAN_FIELDS = ("path", "sha256", "start", "end", "quote")
KINDS = frozenset({"fact", "hypothesis", "question"})
CATEGORIES = frozenset({"observation", "decision", "preference", "inference"})
SUPPORTS = frozenset(
    {
        "not_evaluated",
        "source_linked",
        "insufficient",
        "contested",
        "supported",
        "retired",
    }
)
DATE_FIELDS = ("observed_at", "recorded_at", "valid_from", "valid_until")
_PROMOTED = frozenset({"summary", "decisions", "statements", "claims", "limitations"})
_NOT_PROMOTED = (
    "next_steps",
    "pending",
    "checks",
    "results",
    "changes",
    "lessons",
    "operations",
    "artifacts",
    "criteria",
    "instructions",
    "content",
    "body",
)
_INSTRUCTION = re.compile(
    r"(?i)("
    r"ignore (?:all |any )?(?:previous |prior |above )?instructions"
    r"|ignore as instruções"
    r"|disregard (?:your |the )?(?:rules|instructions)"
    r"|you are now"
    r"|você agora é"
    r"|system prompt"
    r"|</?(?:system|instructions)>"
    r"|do not tell the (?:user|curator)"
    r")"
)
_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,40}")


def concept_page_id(project_id: str, concept: str) -> str:
    """Stable page id scoped to one project and the exact concept string."""

    return "c" + digest(project_id + "\0" + concept)[:40]


def concept_page_path(project_id: str, page_id: str) -> str:
    key = project_key(project_id)
    return f"{corpus(project_id)}/wiki/concepts/{key}--{page_id}.md"


def compile_knowledge(
    publisher: Any,
    project_id: str,
    sources: list[str],
    *,
    concept: str,
    title: str | None = None,
    version: str = "1",
    apply: bool = False,
) -> dict[str, Any]:
    """Compile ``concept`` from selected raw paths.

    ``sources`` must already be immutable raw snapshots in this project or in
    shared. The same project, concept, compiler version and source hashes do
    not publish again. A lost response retries as that same operation.
    """

    project_key(project_id)
    concept = _explicit_text(concept, "invalid_concept")
    if title is not None:
        title = _explicit_text(title, "invalid_title")
    if not isinstance(version, str) or not _VERSION.fullmatch(version):
        raise MemoryError("invalid_compiler_version")
    if not isinstance(apply, bool):
        raise MemoryError("invalid_apply")
    selected = _select_sources(publisher, project_id, sources)
    page_id = concept_page_id(project_id, concept)
    path = concept_page_path(project_id, page_id)
    current = _load_current(publisher, project_id, concept, page_id, path)
    if current.get("state"):
        return _result(
            current["state"],
            current.get("operation_id"),
            version,
            findings=current.get("findings", []),
            proposal=current.get("proposal"),
        )
    recorded = current["recorded"]
    if recorded and _already_incorporated(recorded, version, selected):
        findings = [
            {
                "code": "sources_already_incorporated",
                "path": current["path"],
                "detail": "requested raw hashes are already on this concept",
            }
        ]
        if apply:
            try:
                publisher.recover()
            except ConflictError as exc:
                return _result(
                    "conflict",
                    recorded["operation_id"],
                    version,
                    findings=findings + [{"code": "conflict", "detail": str(exc)}],
                    page_id=page_id,
                    path=current["path"],
                )
        return _result(
            "noop",
            recorded["operation_id"],
            version,
            findings=findings,
            page_id=page_id,
            path=current["path"],
        )
    missing = _missing_recorded(publisher, recorded)
    if missing:
        return _result(
            "conflict",
            recorded["operation_id"] if recorded else None,
            version,
            findings=[
                {
                    "code": "recorded_source_missing",
                    "paths": missing,
                    "detail": "recorded raw is missing or no longer matches its hash",
                }
            ],
            page_id=page_id,
            path=current["path"],
        )
    union = _union_sources(recorded, selected)
    operation_id = _operation_id(project_id, concept, version, union)
    extracted, findings = _extract_all(project_id, concept, selected, current["claims"])
    findings = list(current.get("claim_findings") or []) + findings
    claims = _merge_claims(current["claims"], extracted)
    claims, contest_findings = _mark_contests(claims)
    findings.extend(contest_findings)
    proposal = _proposal(
        publisher,
        project_id=project_id,
        concept=concept,
        title=title or concept,
        version=version,
        operation_id=operation_id,
        page_id=current["page_id"],
        path=current["path"],
        base=current["content"],
        claims=claims,
        union=union,
        prior=current["meta"],
        preserved=current["preserved"],
        findings=findings,
    )
    encoded = proposal.model_dump_json().encode()
    if len(proposal.changes) > MAX_PAGES or len(encoded) > MAX_PROPOSAL_BYTES:
        return _result(
            "rejected",
            operation_id,
            version,
            findings=findings
            + [
                {
                    "code": "proposal_too_large",
                    "pages": len(proposal.changes),
                    "bytes": len(encoded),
                }
            ],
            page_id=page_id,
            path=current["path"],
        )
    payload = proposal.model_dump(mode="json")
    if current["meta"].get("reviewed") is True:
        return _result(
            "suggestion",
            operation_id,
            version,
            findings=findings
            + [
                {
                    "code": "reviewed_suggestion",
                    "path": current["path"],
                    "detail": "reviewed page was not modified",
                }
            ],
            suggestion=payload,
            page_id=current["page_id"],
            path=current["path"],
        )
    if not apply:
        return _result(
            "proposed",
            operation_id,
            version,
            findings=findings,
            proposal=payload,
            page_id=current["page_id"],
            path=current["path"],
        )
    try:
        receipt = publisher.publish(proposal)
    except ConflictError as exc:
        return _result(
            "conflict",
            operation_id,
            version,
            findings=findings + [{"code": "conflict", "detail": str(exc)}],
            proposal=payload,
            page_id=current["page_id"],
            path=current["path"],
        )
    except MemoryError as exc:
        return _result(
            "rejected",
            operation_id,
            version,
            findings=findings + [{"code": str(exc)}],
            proposal=payload,
            page_id=current["page_id"],
            path=current["path"],
        )
    return _result(
        "confirmed",
        operation_id,
        version,
        findings=findings,
        proposal=payload,
        receipt=receipt,
        page_id=current["page_id"],
        path=current["path"],
    )


def render_knowledge_page(
    *,
    project_id: str,
    page_id: str,
    concept: str,
    title: str,
    claims: list[dict[str, Any]],
    source_paths: list[str],
    aliases: list[str],
    tags: list[str],
    reviewed: bool,
    compiler: dict[str, Any],
    preserved: str,
    extra_sources: list[str] | None = None,
    created: str = PLACEHOLDER_DATE,
    updated: str = PLACEHOLDER_DATE,
) -> str:
    links = _source_links(source_paths, extra_sources or [])
    metadata = {
        "schema_version": 1,
        "knowledge_schema": 1,
        "type": "concept",
        "page_id": page_id,
        "project_id": project_id,
        "concept": concept,
        "created": created,
        "updated": updated,
        "sources": links,
        "tags": tags,
        "aliases": aliases,
        "reviewed": reviewed,
        "compiler": compiler,
        "claims": claims,
    }
    return render_page(metadata, _body(title, claims, preserved))


def _result(
    state: str,
    operation_id: str | None,
    version: str,
    *,
    findings: list[dict[str, Any]],
    proposal: dict[str, Any] | None = None,
    suggestion: dict[str, Any] | None = None,
    receipt: dict[str, Any] | None = None,
    page_id: str | None = None,
    path: str | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "state": state,
        "operation_id": operation_id,
        "findings": findings,
        "limits": {
            "max_sources": MAX_SOURCES,
            "max_pages": MAX_PAGES,
            "max_proposal_bytes": MAX_PROPOSAL_BYTES,
            "compiler_version": version,
            "semantic_certification": False,
            "default_support": ["source_linked", "not_evaluated"],
            "network": False,
            "canonical_gc": False,
            "task_completion_coupled": False,
            "auto_ingest_concept": False,
        },
    }
    if proposal is not None:
        result["proposal"] = proposal
    if suggestion is not None:
        result["suggestion"] = suggestion
    if receipt is not None:
        result["receipt"] = receipt
    if page_id is not None:
        result["page_id"] = page_id
    if path is not None:
        result["path"] = path
    return result


def _explicit_text(value: str, code: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MemoryError(code)
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise MemoryError(code)
    if len(value.encode("utf-8")) > 512:
        raise MemoryError(code)
    return value


def _select_sources(publisher: Any, project_id: str, sources: Any) -> list[dict]:
    if isinstance(sources, str) or not isinstance(sources, list):
        raise MemoryError("invalid_sources")
    if len(sources) > MAX_SOURCES:
        raise MemoryError("too_many_sources")
    if not sources:
        raise MemoryError("source_required")
    prefix = corpus(project_id) + "/raw/"
    allowed = (prefix, "shared/raw/")
    ordered: list[str] = []
    for item in sources:
        if not isinstance(item, str) or item in ordered:
            if not isinstance(item, str):
                raise MemoryError("invalid_sources")
            continue
        name = item.rsplit("/", 1)[-1]
        if "/wiki/" in item or "/concepts/" in item:
            raise MemoryError("source_not_raw")
        if (
            ".." in PurePosixPath(item).parts
            or not item.startswith(allowed)
            or not re.fullmatch(r"[0-9a-f]{64}\.md", name)
        ):
            raise MemoryError("source_outside_scope")
        ordered.append(item)
    if not ordered:
        raise MemoryError("source_required")
    loaded = []
    for relative in ordered:
        if not relative.startswith(allowed):
            raise MemoryError("source_outside_scope")
        target = safe_path(publisher.vault, relative)
        if not target.is_file():
            raise MemoryError("source_missing")
        data = target.read_bytes()
        file_hash = digest(data)
        if file_hash != relative.rsplit("/", 1)[-1].removesuffix(".md"):
            raise MemoryError("raw_hash_mismatch")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise MemoryError("invalid_source_encoding") from exc
        loaded.append(
            {"path": relative, "sha256": file_hash, "text": text, "raw": data}
        )
    return loaded


def _load_current(
    publisher: Any,
    project_id: str,
    concept: str,
    page_id: str,
    path: str,
) -> dict[str, Any]:
    matches = []
    root_rel = f"{corpus(project_id)}/wiki/concepts"
    root = publisher.vault / root_rel
    if root.exists():
        for candidate in sorted(root.glob("*.md")):
            relative = candidate.relative_to(publisher.vault).as_posix()
            safe_path(publisher.vault, relative)
            content = candidate.read_text(encoding="utf-8")
            try:
                meta, body = parse_page(content)
            except MemoryError:
                continue
            if meta.get("project_id") != project_id:
                continue
            compiler = meta.get("compiler")
            owned = isinstance(compiler, dict) and meta.get("concept") == concept
            if meta.get("page_id") == page_id or owned:
                matches.append((relative, meta, body, content))
    if len({item[0] for item in matches}) > 1:
        return {
            "state": "rejected",
            "operation_id": None,
            "findings": [
                {
                    "code": "ambiguous_concept_page",
                    "detail": "more than one page owns this concept",
                }
            ],
        }
    if not matches:
        return {
            "path": path,
            "page_id": page_id,
            "meta": {},
            "claims": [],
            "recorded": None,
            "preserved": "",
            "content": None,
        }
    relative, meta, body, content = matches[0]
    if meta.get("page_id") != page_id or (
        meta.get("concept") not in (None, concept) and meta.get("compiler")
    ):
        if meta.get("page_id") != page_id or meta.get("concept") != concept:
            return {
                "state": "conflict",
                "operation_id": None,
                "findings": [
                    {
                        "code": "page_identity_conflict",
                        "path": relative,
                        "detail": "stable concept id does not match the page",
                    }
                ],
            }
    recorded = _recorded(meta, concept)
    claims, claim_findings = _existing_claims(meta)
    return {
        "path": relative,
        "page_id": str(meta.get("page_id") or page_id),
        "meta": meta,
        "claims": claims,
        "claim_findings": claim_findings,
        "recorded": recorded,
        "preserved": _preserved(meta, body),
        "content": content,
    }


def _recorded(meta: dict[str, Any], concept: str) -> dict[str, Any] | None:
    compiler = meta.get("compiler")
    if not isinstance(compiler, dict) or meta.get("concept") != concept:
        return None
    rows = compiler.get("sources")
    if not isinstance(rows, list):
        return None
    sources = []
    for row in rows:
        if not isinstance(row, dict):
            return None
        path = row.get("path")
        sha = row.get("sha256")
        if not isinstance(path, str) or not isinstance(sha, str):
            return None
        sources.append((path, sha))
    operation = compiler.get("operation_id")
    version = compiler.get("version")
    if not isinstance(operation, str) or not isinstance(version, str):
        return None
    return {"version": version, "operation_id": operation, "sources": sources}


def _existing_claims(
    meta: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    claims = meta.get("claims")
    if not isinstance(claims, list):
        return [], []
    normalized: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    for claim in claims:
        if not isinstance(claim, dict):
            continue
        canonical = canonical_claim(claim)
        previous = claim.get("support")
        if (
            isinstance(previous, str)
            and previous in SUPPORTS
            and canonical["support"] != previous
        ):
            findings.append(
                {
                    "code": "existing_claim_downgraded",
                    "claim_id": canonical["id"],
                    "support": previous,
                    "detail": canonical["support"],
                }
            )
        normalized.append(canonical)
    return normalized, findings


def _preserved(meta: dict[str, Any], body: str) -> str:
    marker = "## Contexto anterior\n"
    if marker in body:
        return body.split(marker, 1)[1].strip()
    if meta.get("knowledge_schema") != 1 and body.strip():
        return (
            body.replace("&", "&amp;")
            .replace("[", "&#91;")
            .replace("]", "&#93;")
            .replace("<", "&lt;")
            .strip()
        )
    return ""


def _missing_recorded(publisher: Any, recorded: dict[str, Any] | None) -> list[str]:
    if not recorded:
        return []
    missing = []
    for path, sha in recorded["sources"]:
        try:
            target = safe_path(publisher.vault, path)
        except MemoryError:
            missing.append(path)
            continue
        if not target.is_file() or digest(target.read_bytes()) != sha:
            missing.append(path)
    return missing


def _already_incorporated(
    recorded: dict[str, Any], version: str, selected: list[dict]
) -> bool:
    if recorded["version"] != version:
        return False
    have = set(recorded["sources"])
    requested = {(item["path"], item["sha256"]) for item in selected}
    return requested <= have


def _union_sources(
    recorded: dict[str, Any] | None, selected: list[dict]
) -> list[tuple[str, str]]:
    rows = list(recorded["sources"]) if recorded else []
    known = {path for path, _sha in rows}
    for item in selected:
        if item["path"] not in known:
            rows.append((item["path"], item["sha256"]))
            known.add(item["path"])
    return sorted(rows)


def _operation_id(
    project_id: str,
    concept: str,
    version: str,
    sources: list[tuple[str, str]],
) -> str:
    payload = json.dumps(
        {
            "compiler": version,
            "concept": concept,
            "project_id": project_id,
            "sources": [{"path": path, "sha256": sha} for path, sha in sources],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "cmp-" + digest(payload)[:40]


def _extract_all(
    project_id: str,
    concept: str,
    selected: list[dict],
    existing: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    claims: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    known = {claim.get("id") for claim in existing}
    for source in sorted(selected, key=lambda item: item["path"]):
        payload, recorded_at, parse_findings = _parse_report(source["text"])
        for finding in parse_findings:
            finding["path"] = source["path"]
            findings.append(finding)
        if payload is None:
            claims.append(_unstructured_claim(project_id, concept, source, known))
            findings.append(
                {
                    "code": "unstructured_source",
                    "path": source["path"],
                    "detail": "no structured summary or decisions",
                }
            )
            continue
        if _directive(payload, project_id, concept):
            findings.append(
                {
                    "code": "source_directive_ignored",
                    "path": source["path"],
                    "detail": "source project or concept directives are data",
                }
            )
        for statement in _statements(payload, recorded_at):
            claim, claim_findings = _claim_from_statement(
                project_id, concept, source, statement, known
            )
            findings.extend(claim_findings)
            if claim is not None:
                claims.append(claim)
    return claims, findings


def _parse_report(
    text: str,
) -> tuple[dict[str, Any] | None, str | None, list[dict[str, Any]]]:
    try:
        loaded = json.loads(text.strip())
    except json.JSONDecodeError:
        return None, None, []
    if not isinstance(loaded, dict):
        return (
            None,
            None,
            [{"code": "unstructured_source", "detail": "json_not_object"}],
        )
    evidence = loaded.get("evidence")
    payload = evidence if isinstance(evidence, dict) else loaded
    recorded = _explicit_date(
        loaded.get("recorded_at")
        or loaded.get("finished_at")
        or payload.get("recorded_at")
    )
    findings = []
    for key in _NOT_PROMOTED:
        value = payload.get(key)
        if value not in (None, "", [], {}):
            findings.append(
                {
                    "code": "not_promoted",
                    "field": key,
                    "detail": "field kept as source data and not compiled",
                }
            )
    if not any(key in payload for key in _PROMOTED):
        return None, recorded, findings
    return payload, recorded, findings


def _directive(payload: dict[str, Any], project_id: str, concept: str) -> bool:
    foreign_project = payload.get("project_id")
    foreign_concept = payload.get("concept")
    return (isinstance(foreign_project, str) and foreign_project != project_id) or (
        isinstance(foreign_concept, str) and foreign_concept != concept
    )


def _statements(
    payload: dict[str, Any], recorded_at: str | None
) -> list[dict[str, Any]]:
    items = []
    summary = payload.get("summary")
    if isinstance(summary, str) and summary.strip():
        items.append(
            {
                "text": summary,
                "category": "observation",
                "origin": "summary",
                "recorded_at": recorded_at,
            }
        )
    decisions = payload.get("decisions")
    if isinstance(decisions, list):
        for decision in decisions:
            items.extend(_structured_item(decision, "decision", recorded_at))
    statements = payload.get("statements")
    if isinstance(statements, list):
        for statement in statements:
            items.extend(_structured_item(statement, "observation", recorded_at))
    claims = payload.get("claims")
    if isinstance(claims, list):
        for claim in claims:
            items.extend(_structured_item(claim, "observation", recorded_at))
    limitations = payload.get("limitations")
    if isinstance(limitations, str) and limitations.strip():
        items.append(
            {
                "text": limitations,
                "category": "observation",
                "kind": "hypothesis",
                "origin": "limitations",
                "recorded_at": recorded_at,
            }
        )
    elif isinstance(limitations, list):
        for item in limitations:
            if isinstance(item, str) and item.strip():
                items.append(
                    {
                        "text": item,
                        "category": "observation",
                        "kind": "hypothesis",
                        "origin": "limitations",
                        "recorded_at": recorded_at,
                    }
                )
    return items


def _structured_item(
    value: Any, default_category: str, recorded_at: str | None
) -> list[dict[str, Any]]:
    if isinstance(value, str) and value.strip():
        return [
            {
                "text": value,
                "category": default_category,
                "origin": default_category,
                "recorded_at": recorded_at,
            }
        ]
    if not isinstance(value, dict):
        return []
    text = value.get("text") or value.get("summary") or value.get("decision")
    item = {
        "text": text if isinstance(text, str) else "",
        "category": value.get("category") or default_category,
        "kind": value.get("kind"),
        "origin": default_category,
        "recorded_at": _explicit_date(value.get("recorded_at")) or recorded_at,
        "reported_support": value.get("support"),
        "reported_assessment": value.get("assessment"),
    }
    for key in ("subject", "predicate", "value", "supersedes", *DATE_FIELDS):
        if key == "recorded_at":
            continue
        field = value.get(key)
        if key == "supersedes":
            ids = supersedes_ids(field)
            if ids:
                item[key] = ids
            continue
        if isinstance(field, str):
            item[key] = field
    if not item["text"] and all(
        isinstance(item.get(key), str) and item[key]
        for key in ("subject", "predicate", "value")
    ):
        item["text"] = f"{item['subject']} {item['predicate']} {item['value']}"
        item["text_synthesized"] = True
    return [item]


def _claim_from_statement(
    project_id: str,
    concept: str,
    source: dict,
    statement: dict[str, Any],
    known: set[Any],
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    findings = []
    text = statement.get("text") or ""
    if not isinstance(text, str) or not text.strip():
        return None, findings
    instruction = bool(_INSTRUCTION.search(text))
    category = statement.get("category")
    if instruction or category not in CATEGORIES:
        category = (
            "decision"
            if not instruction and statement.get("origin") == "decision"
            else "observation"
        )
    kind = statement.get("kind") if statement.get("kind") in KINDS else None
    if kind is None:
        kind = "question" if text.rstrip().endswith("?") else "fact"
    if instruction:
        kind = "hypothesis"
        category = "observation"
        findings.append(
            {
                "code": "instruction_not_authority",
                "path": source["path"],
                "detail": "embedded instructions remain data",
            }
        )
    span = None if statement.get("text_synthesized") else _span(source, text)
    if span is None and not statement.get("text_synthesized"):
        span = _span(source, json.dumps(text, ensure_ascii=False)[1:-1])
    if statement.get("reported_support") not in (
        None,
        "source_linked",
        "not_evaluated",
    ):
        findings.append(
            {
                "code": "support_not_accepted",
                "path": source["path"],
                "detail": "source cannot certify its own support",
            }
        )
    if statement.get("reported_assessment") not in (None, "", {}, []):
        findings.append(
            {
                "code": "assessment_not_accepted",
                "path": source["path"],
                "detail": "source assessment is not a review",
            }
        )
    dates = {key: _explicit_date(statement.get(key)) for key in DATE_FIELDS}
    if dates["recorded_at"] is None:
        dates["recorded_at"] = _explicit_date(statement.get("recorded_at"))
    for key in DATE_FIELDS:
        raw_value = statement.get(key)
        if isinstance(raw_value, str) and raw_value and dates[key] is None:
            findings.append(
                {"code": "unparsed_date", "field": key, "path": source["path"]}
            )
    triple = _triple(statement)
    if kind == "fact" and span is None:
        kind = "hypothesis"
    support = "not_evaluated" if instruction or span is None else "source_linked"
    start = 0 if span is None else span["start"]
    end = 0 if span is None else span["end"]
    claim_id = (
        "k"
        + digest(
            "\0".join(
                [
                    project_id,
                    concept,
                    source["path"],
                    str(start),
                    str(end),
                    category,
                    text,
                ]
            )
        )[:32]
    )
    if claim_id in known:
        return None, findings
    known.add(claim_id)
    claim = canonical_claim(
        {
            "id": claim_id,
            "project_id": project_id,
            "text": text,
            "kind": kind,
            "category": category,
            "support": support,
            "evidence": [] if span is None else [span],
            "assessment": None,
            "observed_at": dates["observed_at"],
            "recorded_at": dates["recorded_at"],
            "valid_from": dates["valid_from"],
            "valid_until": dates["valid_until"],
            "supersedes": supersedes_ids(statement.get("supersedes")),
            "subject": triple[0] if triple else None,
            "predicate": triple[1] if triple else None,
            "value": triple[2] if triple else None,
        }
    )
    return claim, findings


def _unstructured_claim(
    project_id: str, concept: str, source: dict, known: set[Any]
) -> dict[str, Any]:
    excerpt = (
        source["text"].strip().splitlines()[0][:240] if source["text"].strip() else ""
    )
    span = _span(source, excerpt) if excerpt else None
    text = "Fonte sem relatório estruturado."
    start = 0 if span is None else span["start"]
    end = 0 if span is None else span["end"]
    claim_id = (
        "k"
        + digest(
            "\0".join([project_id, concept, source["path"], str(start), str(end), text])
        )[:32]
    )
    known.add(claim_id)
    return canonical_claim(
        {
            "id": claim_id,
            "project_id": project_id,
            "text": text,
            "kind": "hypothesis",
            "category": "observation",
            "support": "not_evaluated" if span is None else "source_linked",
            "evidence": [] if span is None else [span],
            "assessment": None,
            "observed_at": None,
            "recorded_at": None,
            "valid_from": None,
            "valid_until": None,
            "supersedes": [],
            "subject": None,
            "predicate": None,
            "value": None,
        }
    )


def _triple(statement: dict[str, Any]) -> tuple[str, str, str] | None:
    subject = statement.get("subject")
    predicate = statement.get("predicate")
    value = statement.get("value")
    if (
        isinstance(subject, str)
        and isinstance(predicate, str)
        and isinstance(value, str)
        and subject
        and predicate
        and value
    ):
        return subject, predicate, value
    return None


def _explicit_date(value: Any) -> str | None:
    """Accept the same dates ``claims._coerce_date`` publishes.

    YAML may yield ``date`` or ``datetime`` objects, and a reviewer may write a
    naive ISO timestamp. Those stay on the claim. Unparseable values stay null.
    """

    try:
        return _coerce_date(value)
    except MemoryError:
        return None


def supersedes_ids(value: Any) -> list[str]:
    """Contract form of ``supersedes``: a list of claim ids.

    A single id is accepted and stored as one element. Missing, null and
    non-strings become an empty list. The page date placeholder is not an id.
    """

    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    ids: list[str] = []
    for item in value:
        if isinstance(item, str) and item and item not in ids:
            ids.append(item)
    return ids


def canonical_claim(raw: dict[str, Any]) -> dict[str, Any]:
    """Return one ``KnowledgeClaim`` mapping.

    Dates that were not explicitly recorded stay null. ``1970-01-01`` on the
    page envelope is never copied into ``observed_at`` or ``recorded_at``.
    """

    if not isinstance(raw, dict):
        raise MemoryError("invalid_claim")
    text = raw.get("text") if isinstance(raw.get("text"), str) else ""
    kind = raw.get("kind") if raw.get("kind") in KINDS else "hypothesis"
    category = (
        raw.get("category") if raw.get("category") in CATEGORIES else "observation"
    )
    support = raw.get("support") if raw.get("support") in SUPPORTS else "not_evaluated"
    evidence = []
    for span in raw.get("evidence") or []:
        normalized = _canonical_span(span)
        if normalized is not None:
            evidence.append(normalized)
    assessment = _canonical_assessment(raw.get("assessment"), support)
    if support == "supported" and assessment is None:
        support = "source_linked" if evidence else "not_evaluated"
    dates = {key: _explicit_date(raw.get(key)) for key in DATE_FIELDS}

    def _text(key: str) -> str | None:
        value = raw.get(key)
        return value if isinstance(value, str) and value else None

    return {
        "id": raw.get("id") if isinstance(raw.get("id"), str) else "",
        "project_id": raw.get("project_id")
        if isinstance(raw.get("project_id"), str)
        else "",
        "text": text,
        "kind": kind,
        "category": category,
        "support": support,
        "evidence": evidence,
        "assessment": assessment,
        "observed_at": dates["observed_at"],
        "recorded_at": dates["recorded_at"],
        "valid_from": dates["valid_from"],
        "valid_until": dates["valid_until"],
        "supersedes": supersedes_ids(raw.get("supersedes")),
        "subject": _text("subject"),
        "predicate": _text("predicate"),
        "value": _text("value"),
    }


def _canonical_span(span: Any) -> dict[str, Any] | None:
    if not isinstance(span, dict):
        return None
    path = span.get("path")
    sha = span.get("sha256")
    quote = span.get("quote")
    start = span.get("start")
    end = span.get("end")
    if not all(isinstance(item, str) and item for item in (path, sha, quote)):
        return None
    if isinstance(start, bool) or isinstance(end, bool):
        return None
    if not isinstance(start, int) or not isinstance(end, int):
        return None
    if not re.fullmatch(r"[0-9a-f]{64}", sha):
        return None
    return {
        "path": path,
        "sha256": sha,
        "start": start,
        "end": end,
        "quote": quote,
    }


def _canonical_assessment(value: Any, support: str) -> dict[str, str] | None:
    if not isinstance(value, dict) or value.get("verdict") != support:
        return None
    reviewer = value.get("reviewer")
    reason = value.get("reason")
    assessed_at = _explicit_date(value.get("assessed_at"))
    if not all(isinstance(item, str) and item.strip() for item in (reviewer, reason)):
        return None
    if assessed_at is None:
        return None
    return {
        "verdict": support,
        "reviewer": reviewer.strip(),
        "reason": reason.strip(),
        "assessed_at": assessed_at,
    }


def _span(source: dict, quote: str) -> dict[str, Any] | None:
    if not quote:
        return None
    encoded = quote.encode()
    start = source["raw"].find(encoded)
    if start < 0:
        return None
    end = start + len(encoded)
    if source["raw"][start:end].decode("utf-8") != quote:
        return None
    return {
        "path": source["path"],
        "sha256": source["sha256"],
        "start": start,
        "end": end,
        "quote": quote,
    }


def _merge_claims(
    existing: list[dict[str, Any]], extracted: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    merged = [dict(claim) for claim in existing]
    known = {claim.get("id") for claim in merged}
    for claim in extracted:
        if claim["id"] not in known:
            merged.append(claim)
            known.add(claim["id"])
    return merged


def _mark_contests(
    claims: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for claim in claims:
        subject = claim.get("subject")
        predicate = claim.get("predicate")
        value = claim.get("value")
        if not (
            isinstance(subject, str)
            and isinstance(predicate, str)
            and isinstance(value, str)
        ):
            continue
        if claim.get("support") == "retired":
            continue
        groups.setdefault((subject, predicate), []).append(claim)
    findings = []
    for (subject, predicate), group in groups.items():
        values = {claim["value"] for claim in group}
        if len(values) < 2:
            continue
        ids = {claim.get("id") for claim in group}
        if any(set(supersedes_ids(claim.get("supersedes"))) & ids for claim in group):
            findings.append(
                {
                    "code": "explicit_supersession",
                    "subject": subject,
                    "predicate": predicate,
                    "detail": "both values stay; supersession is not a silent overwrite",
                }
            )
            continue
        superseded = []
        for claim in group:
            assessment = claim.get("assessment")
            verdict = assessment.get("verdict") if isinstance(assessment, dict) else None
            if isinstance(assessment, dict) and verdict != "contested":
                superseded.append(claim.get("id"))
                claim["assessment"] = None
            claim["support"] = "contested"
        findings.append(
            {
                "code": "contested",
                "subject": subject,
                "predicate": predicate,
                "values": sorted(values),
                "claim_ids": [claim["id"] for claim in group],
                "detail": "distinct values stay contested",
            }
        )
        if superseded:
            findings.append(
                {
                    "code": "assessment_superseded_by_contest",
                    "subject": subject,
                    "predicate": predicate,
                    "claim_ids": superseded,
                    "detail": (
                        "reviewer assessment cleared because the triple is now contested"
                    ),
                }
            )
    return claims, findings


def _proposal(
    publisher: Any,
    *,
    project_id: str,
    concept: str,
    title: str,
    version: str,
    operation_id: str,
    page_id: str,
    path: str,
    base: str | None,
    claims: list[dict[str, Any]],
    union: list[tuple[str, str]],
    prior: dict[str, Any],
    preserved: str,
    findings: list[dict[str, Any]],
) -> MemoryProposal:
    tags = [tag for tag in prior.get("tags", []) if isinstance(tag, str)]
    aliases = []
    for alias in (title, concept, *prior.get("aliases", [])):
        if isinstance(alias, str) and alias not in aliases:
            aliases.append(alias)
    extra = [
        link[2:-2]
        for link in prior.get("sources", [])
        if isinstance(link, str) and link.startswith("[[") and link.endswith("]]")
    ]
    content = render_knowledge_page(
        project_id=project_id,
        page_id=page_id,
        concept=concept,
        title=title,
        claims=claims,
        source_paths=[item[0] for item in union],
        aliases=aliases,
        tags=tags,
        reviewed=bool(prior.get("reviewed")),
        compiler={
            "version": version,
            "operation_id": operation_id,
            "sources": [{"path": item[0], "sha256": item[1]} for item in union],
        },
        preserved=preserved,
        extra_sources=extra,
    )
    base_hash = digest(base) if base is not None else None
    links = _source_links([item[0] for item in union], extra)
    return MemoryProposal.model_validate(
        {
            "schema_version": 1,
            "batch_id": operation_id,
            "operation_id": operation_id,
            "project_id": project_id,
            "base_commit": None,
            "source_hashes": {item[0]: item[1] for item in union},
            "changes": [
                {
                    "page_id": page_id,
                    "path": path,
                    "base_sha256": base_hash,
                    "content": content,
                }
            ],
            "citations": links,
            "findings": findings,
        }
    )


def _source_links(paths: list[str], extra: list[str]) -> list[str]:
    links = []
    for path in [*paths, *extra]:
        link = f"[[{path}]]"
        if link not in links and "/wiki/concepts/" not in path:
            links.append(link)
    return links


def _body(title: str, claims: list[dict[str, Any]], preserved: str) -> str:
    lines = [
        f"# {_visible(title)}",
        "",
        "## Definição",
        "",
        "Síntese compilada de fontes explicitamente selecionadas. "
        "O compilador não certifica sustentação semântica.",
        "",
        "## Afirmações",
        "",
    ]
    if not claims:
        lines.append("Nenhuma afirmação extraída.")
    for claim in claims:
        lines.append(f"- ({claim.get('support')}) {_visible(str(claim.get('text')))}")
        refs = " ".join(f"[[{span['path']}]]" for span in claim.get("evidence") or [])
        if refs:
            lines.append(f"  - Fontes: {refs}")
    lines.extend(["", "## Contradições", ""])
    contested = [claim for claim in claims if claim.get("support") == "contested"]
    if not contested:
        lines.append("Nenhuma contestação explícita registrada neste conceito.")
    else:
        for claim in contested:
            lines.append(
                "- "
                + _visible(str(claim.get("subject")))
                + " / "
                + _visible(str(claim.get("predicate")))
                + ": "
                + _visible(str(claim.get("value")))
                + f" ({claim.get('id')})"
            )
    if preserved:
        lines.extend(["", "## Contexto anterior", "", preserved])
    lines.append("")
    return "\n".join(lines)


def _visible(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("[", "&#91;")
        .replace("]", "&#93;")
        .replace("<", "&lt;")
    )
