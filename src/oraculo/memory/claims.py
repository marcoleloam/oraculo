"""Typed knowledge claims and mechanical evidence checks.

`supported` is never inferred from a matching quote. A quote proves only that
the cited bytes exist; an attributable assessment is a separate, auditable
claim about meaning. Dates that are absent stay unknown.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .schema import (
    MemoryError,
    corpus,
    digest,
    identifier,
    knowledge_domain,
    parse_page,
)

Support = Literal[
    "not_evaluated",
    "source_linked",
    "insufficient",
    "contested",
    "supported",
    "retired",
]
Kind = Literal["fact", "hypothesis", "question"]
Category = Literal["observation", "decision", "preference", "inference"]
_DATE_FIELDS = ("observed_at", "recorded_at", "valid_from", "valid_until")
_LINKED = {"source_linked", "supported"}
_MAX_CHAIN = 16


class SourceSpan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    quote: str


class ClaimAssessment(BaseModel):
    """Attributed semantic judgment. It does not prove the claim is true."""

    model_config = ConfigDict(extra="forbid")
    verdict: Support
    reviewer: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    assessed_at: str = Field(min_length=1)


class KnowledgeClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    project_id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    kind: Kind
    category: Category
    support: Support
    evidence: list[SourceSpan] = Field(default_factory=list)
    assessment: ClaimAssessment | None = None
    observed_at: str | None = None
    recorded_at: str | None = None
    valid_from: str | None = None
    valid_until: str | None = None
    supersedes: list[str] = Field(default_factory=list)
    subject: str | None = None
    predicate: str | None = None
    value: str | None = None


def validate_knowledge(
    content: str, project_id: str, files: dict[str, str]
) -> list[dict[str, Any]]:
    """Audit one page. Legacy pages are not_evaluated and are not promoted."""
    try:
        meta, _body = parse_page(content)
    except MemoryError:
        return [
            _finding(
                code="invalid_page",
                severity="error",
                project_id=project_id,
            )
        ]
    page_id = meta.get("page_id") if isinstance(meta.get("page_id"), str) else None
    path = _locate(content, files)
    try:
        domain = knowledge_domain(meta)
    except MemoryError:
        return [
            _finding(
                code="invalid_knowledge_schema",
                severity="error",
                project_id=project_id,
                page_id=page_id,
                path=path,
            )
        ]
    if domain is None:
        return [
            _finding(
                code="not_evaluated",
                severity="info",
                project_id=project_id,
                page_id=page_id,
                path=path,
                support="not_evaluated",
                reason="legacy_untyped",
                knowledge_schema=None,
            )
        ]
    origins = _origins(content, files)
    findings: list[dict[str, Any]] = []
    seen: set[str] = set()
    parsed: list[KnowledgeClaim] = []
    for index, raw in enumerate(domain["claims"]):
        claim, errors = _parse_claim(raw, project_id, page_id, path, index)
        findings.extend(errors)
        if claim is None:
            continue
        if claim.id in seen:
            findings.append(
                _finding(
                    code="duplicate_claim_id",
                    severity="error",
                    project_id=project_id,
                    page_id=page_id,
                    path=path,
                    claim_id=claim.id,
                )
            )
            continue
        seen.add(claim.id)
        parsed.append(claim)
    findings.extend(_supersedes(parsed, files, project_id, page_id=page_id, path=path))
    if not parsed and not any(item["severity"] == "error" for item in findings):
        findings.append(
            _finding(
                code="not_evaluated",
                severity="info",
                project_id=project_id,
                page_id=page_id,
                path=path,
                support="not_evaluated",
                reason="no_claims",
                knowledge_schema=1,
            )
        )
    for claim in parsed:
        findings.extend(
            _audit_claim(
                claim,
                files,
                project_id,
                origins,
                page_id=page_id,
                path=path,
            )
        )
    return _sort(findings)


def _parse_claim(
    raw: Any,
    project_id: str,
    page_id: str | None,
    path: str | None,
    index: int,
) -> tuple[KnowledgeClaim | None, list[dict[str, Any]]]:
    label = f"#{index}"
    if not isinstance(raw, dict):
        return None, [
            _finding(
                code="invalid_claim",
                severity="error",
                project_id=project_id,
                page_id=page_id,
                path=path,
                claim_id=label,
                detail="mapping_required",
            )
        ]
    claim_id = raw.get("id") if isinstance(raw.get("id"), str) else label
    try:
        prepared = _prepare_claim(raw)
        claim = KnowledgeClaim.model_validate(prepared)
        identifier(claim.id)
    except MemoryError as exc:
        code = str(exc)
        if code not in {
            "invalid_date",
            "semantic_assessment_required",
            "invalid_identifier",
        }:
            code = "invalid_claim"
        return None, [
            _finding(
                code=code,
                severity="error",
                project_id=project_id,
                page_id=page_id,
                path=path,
                claim_id=claim_id if isinstance(claim_id, str) else label,
                detail=str(exc),
            )
        ]
    except ValidationError:
        return None, [
            _finding(
                code="invalid_claim",
                severity="error",
                project_id=project_id,
                page_id=page_id,
                path=path,
                claim_id=claim_id if isinstance(claim_id, str) else label,
            )
        ]
    if claim.project_id != project_id:
        return None, [
            _finding(
                code="project_identity_mismatch",
                severity="error",
                project_id=project_id,
                page_id=page_id,
                path=path,
                claim_id=claim.id,
            )
        ]
    return claim, []


def _prepare_claim(raw: dict[str, Any]) -> dict[str, Any]:
    item = dict(raw)
    for key in _DATE_FIELDS:
        if key in item:
            item[key] = _coerce_date(item[key])
    assessment = item.get("assessment")
    if isinstance(assessment, dict):
        copied = dict(assessment)
        if "assessed_at" in copied:
            copied["assessed_at"] = _coerce_date(copied["assessed_at"], required=True)
        for key in ("reviewer", "reason"):
            if isinstance(copied.get(key), str):
                copied[key] = copied[key].strip()
        item["assessment"] = copied
    return item


def _coerce_date(value: Any, *, required: bool = False) -> str | None:
    if value is None or value == "":
        if required:
            raise MemoryError("semantic_assessment_required")
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if not isinstance(value, str):
        raise MemoryError("invalid_date")
    text = value.strip()
    if not text:
        if required:
            raise MemoryError("semantic_assessment_required")
        return None
    try:
        if "T" in text:
            datetime.fromisoformat(text.replace("Z", "+00:00"))
        else:
            date.fromisoformat(text)
    except ValueError as exc:
        raise MemoryError("invalid_date") from exc
    return text


def _audit_claim(
    claim: KnowledgeClaim,
    files: dict[str, str],
    project_id: str,
    origins: set[str],
    *,
    page_id: str | None,
    path: str | None,
) -> list[dict[str, Any]]:
    base = {
        "project_id": project_id,
        "page_id": page_id,
        "path": path,
        "claim_id": claim.id,
    }
    raw_paths: list[str] = []
    findings: list[dict[str, Any]] = []
    mechanical = True
    for span in claim.evidence:
        code, detail, reached = _follow(span, files, project_id, origins, stack=())
        if code:
            mechanical = False
            findings.append(
                _finding(
                    code=code,
                    severity="error",
                    detail=detail,
                    **base,
                )
            )
            continue
        raw_paths.extend(reached)
    if findings:
        return findings
    if claim.kind == "fact" and not raw_paths:
        return [
            _finding(
                code="fact_without_evidence",
                severity="error",
                **base,
            )
        ]
    if claim.support in _LINKED and not raw_paths:
        return [
            _finding(
                code="evidence_required",
                severity="error",
                **base,
                support=claim.support,
            )
        ]
    if claim.support == "supported":
        if (
            claim.assessment is None
            or claim.assessment.verdict != "supported"
            or not claim.assessment.reviewer.strip()
            or not claim.assessment.reason.strip()
        ):
            return [
                _finding(
                    code="semantic_assessment_required",
                    severity="error",
                    **base,
                    support=claim.support,
                )
            ]
    if claim.assessment is not None and claim.assessment.verdict != claim.support:
        return [
            _finding(
                code="assessment_conflict",
                severity="error",
                **base,
                support=claim.support,
                detail=claim.assessment.verdict,
            )
        ]
    finding = _finding(
        code=claim.support,
        severity="info",
        **base,
        support=claim.support,
        knowledge_schema=1,
    )
    if claim.evidence:
        finding["mechanical_quote"] = mechanical
    if claim.kind in {"hypothesis", "question"} and not claim.evidence:
        finding["reason"] = "evidence_absent"
    if not any(getattr(claim, key) for key in _DATE_FIELDS):
        finding["dates"] = "unknown"
    return [finding]


def _follow(
    span: SourceSpan,
    files: dict[str, str],
    project_id: str,
    origins: set[str],
    stack: tuple[str, ...],
) -> tuple[str | None, str, list[str]]:
    if len(stack) > _MAX_CHAIN:
        return "provenance_too_deep", "", []
    if span.path in origins or span.path in stack:
        # A back-edge is refused before hash matching. Two pages cannot each
        # embed the other's file hash, so the cycle is a path cycle.
        detail = "self" if span.path in origins and not stack else "cycle"
        return "circular_citation", detail, []
    code = _mechanical(span, files, project_id)
    if code == "utf8_split":
        return code, "boundary", []
    if code:
        return code, "", []
    if span.path in origins:
        return "circular_citation", "self", []
    if span.path in stack:
        return "circular_citation", "cycle", []
    if _is_raw(span.path, project_id):
        return None, "", [span.path]
    try:
        meta, _body = parse_page(files[span.path])
        domain = knowledge_domain(meta)
    except MemoryError:
        return "derived_without_origin", "", []
    if domain is None or not domain["claims"]:
        return "derived_without_origin", "", []
    owner = meta.get("project_id")
    if not isinstance(owner, str) or not owner:
        return "derived_without_origin", "", []
    reached: list[str] = []
    for raw in domain["claims"]:
        nested, errors = _parse_claim(raw, owner, None, span.path, 0)
        if errors or nested is None:
            code = errors[0]["code"] if errors else "derived_without_origin"
            return code, str(errors[0].get("detail") or "") if errors else "", []
        if not nested.evidence:
            continue
        for evidence in nested.evidence:
            child, detail, raw_paths = _follow(
                evidence,
                files,
                project_id,
                origins,
                stack + (span.path,),
            )
            if child:
                return child, detail, []
            reached.extend(raw_paths)
    if not reached:
        return "derived_without_origin", "", []
    return None, "", reached


def _mechanical(span: SourceSpan, files: dict[str, str], project_id: str) -> str | None:
    if not _in_scope(span.path, project_id) or span.path not in files:
        return "out_of_scope"
    text = files[span.path]
    if digest(text) != span.sha256:
        return "hash_mismatch"
    if _is_raw(span.path, project_id):
        name = PurePosixPath(span.path).name
        if name != f"{span.sha256}.md":
            return "hash_mismatch"
    data = text.encode("utf-8")
    if span.end <= span.start or span.end > len(data):
        return "invalid_span"
    try:
        data[: span.start].decode("utf-8")
        chunk = data[span.start : span.end]
        quote = chunk.decode("utf-8")
    except UnicodeDecodeError:
        return "utf8_split"
    if quote != span.quote:
        return "quote_mismatch"
    return None


def _in_scope(path: str, project_id: str) -> bool:
    if (
        not isinstance(path, str)
        or not path
        or path.startswith(("/", "~"))
        or "\\" in path
        or ".." in PurePosixPath(path).parts
    ):
        return False
    prefix = corpus(project_id)
    return path.startswith(prefix + "/") or path.startswith("shared/")


def _is_raw(path: str, project_id: str) -> bool:
    parent = PurePosixPath(path).parent.as_posix()
    allowed = {f"{corpus(project_id)}/raw", "shared/raw"}
    return parent in allowed and path.endswith(".md")


def _supersedes(
    claims: list[KnowledgeClaim],
    files: dict[str, str],
    project_id: str,
    *,
    page_id: str | None,
    path: str | None,
) -> list[dict[str, Any]]:
    by_id = {claim.id: claim for claim in claims}
    index = _project_index(files)
    findings: list[dict[str, Any]] = []
    for claim in claims:
        for target in claim.supersedes:
            if target == claim.id or _supersedes_cycle(claim.id, by_id):
                findings.append(
                    _finding(
                        code="circular_supersedes",
                        severity="error",
                        project_id=project_id,
                        page_id=page_id,
                        path=path,
                        claim_id=claim.id,
                        detail=target,
                    )
                )
                break
            owners = index.get(target, set())
            if owners and owners != {project_id}:
                findings.append(
                    _finding(
                        code="project_identity_mismatch",
                        severity="error",
                        project_id=project_id,
                        page_id=page_id,
                        path=path,
                        claim_id=claim.id,
                        detail=target,
                    )
                )
    return findings


def _supersedes_cycle(start: str, by_id: dict[str, KnowledgeClaim]) -> bool:
    seen: set[str] = set()
    stack = list(by_id[start].supersedes)
    while stack:
        current = stack.pop()
        if current == start:
            return True
        if current in seen or current not in by_id:
            continue
        seen.add(current)
        stack.extend(by_id[current].supersedes)
    return False


def _project_index(files: dict[str, str]) -> dict[str, set[str]]:
    index: dict[str, set[str]] = {}
    for text in files.values():
        try:
            meta, _body = parse_page(text)
            domain = knowledge_domain(meta)
        except MemoryError:
            continue
        if domain is None:
            continue
        for raw in domain["claims"]:
            if not isinstance(raw, dict):
                continue
            claim_id = raw.get("id")
            owner = raw.get("project_id")
            if isinstance(claim_id, str) and isinstance(owner, str):
                index.setdefault(claim_id, set()).add(owner)
    return index


def _origins(content: str, files: dict[str, str]) -> set[str]:
    return {path for path, text in files.items() if text == content}


def _locate(content: str, files: dict[str, str]) -> str | None:
    matches = [path for path, text in files.items() if text == content]
    wiki = sorted(path for path in matches if "/raw/" not in path)
    pool = wiki or sorted(matches)
    return pool[0] if pool else None


def _finding(**fields: Any) -> dict[str, Any]:
    return {key: value for key, value in fields.items() if value is not None}


def _sort(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        findings,
        key=lambda item: (
            item.get("path") or "",
            item.get("claim_id") or "",
            item.get("code") or "",
            item.get("detail") or "",
        ),
    )
