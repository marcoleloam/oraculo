"""Deterministic freshness audit and curatorial review proposals.

Page ``updated`` is not an observation and is not a revalidation. Nothing in
this module opens the network, deletes raw, or writes the vault.
"""

from __future__ import annotations

import json
import re
import unicodedata
from datetime import date, datetime, timezone
from typing import Any, Mapping

from oraculo.memory.compiler import (
    canonical_claim,
    render_knowledge_page,
    supersedes_ids,
)
from oraculo.memory.schema import (
    MemoryError,
    MemoryProposal,
    corpus,
    digest,
    parse_page,
    project_key,
)

SUPPORTS = frozenset(
    {
        "not_evaluated",
        "source_linked",
        "supported",
        "insufficient",
        "contested",
        "retired",
    }
)
KINDS = frozenset({"fact", "hypothesis", "question"})
CATEGORIES = frozenset({"observation", "decision", "preference", "inference"})
ACTIVE = frozenset(
    {"not_evaluated", "source_linked", "supported", "insufficient", "contested"}
)
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_NAIVE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?$")
_AWARE = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)
_ACTIONS = frozenset({"retire", "reobserve", "pointer", "supersede"})


def freshness_audit(
    files: Mapping[str, str],
    project_id: str,
    *,
    now: datetime | date | None = None,
) -> list[dict[str, Any]]:
    """Report mechanical errors, possible contradictions and missing context.

    Recency does not choose a winner. A preference is not expired because it
    has not been accessed. Explicit supersession keeps both claims.
    """

    project_key(project_id)
    clock = _clock(now)
    if not isinstance(files, Mapping):
        raise MemoryError("invalid_files")
    prefix = corpus(project_id) + "/"
    findings: list[dict[str, Any]] = []
    ignored = 0
    pages = []
    for path, content in sorted(files.items()):
        if not isinstance(path, str) or not (
            path.startswith(prefix) or path.startswith("shared/")
        ):
            ignored += 1
            continue
        if not isinstance(content, str) or not path.endswith(".md"):
            continue
        if not content.startswith("---\n"):
            continue
        try:
            meta, body = parse_page(content)
        except MemoryError as exc:
            findings.append(
                _error(path, "invalid_page", detail=str(exc), project_id=project_id)
            )
            continue
        if meta.get("project_id") not in {project_id, "shared"}:
            findings.append(
                _error(
                    path,
                    "page_project_mismatch",
                    detail="page project is outside the audited scope",
                    project_id=project_id,
                )
            )
            continue
        pages.append((path, meta, body, content))
    if ignored:
        findings.append(
            {
                "code": "out_of_scope_ignored",
                "severity": "record",
                "project_id": project_id,
                "count": ignored,
                "canonical_gc": False,
            }
        )
    retired_paths = _retired_paths(pages)
    records = []
    for path, meta, _body, _content in pages:
        records.extend(_page_claims(path, meta, files, clock, findings))
    findings.extend(records)
    findings.extend(_contradictions(records))
    findings.extend(_homonyms(records))
    findings.extend(_supersessions(records))
    findings.extend(_retired_alerts(records, retired_paths))
    findings.append(
        {
            "code": "audit_limits",
            "severity": "record",
            "project_id": project_id,
            "network": False,
            "canonical_gc": False,
            "garbage_collected": False,
            "updated_revalidates": False,
            "newer_wins": False,
        }
    )
    return findings


def build_review_proposal(
    files: Mapping[str, str],
    project_id: str,
    decisions: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build a pure curatorial proposal. It does not publish or delete raw."""

    project_key(project_id)
    if not isinstance(files, Mapping) or not isinstance(decisions, list):
        raise MemoryError("invalid_review_request")
    if not decisions:
        return _review_result("noop", None, [], project_id)
    pages, page_problems = _concept_pages(files, project_id)
    if page_problems:
        return _review_result("rejected", None, page_problems, project_id)
    located, problems = _bind_decisions(pages, decisions)
    if problems:
        return _review_result("rejected", None, problems, project_id)
    edited, findings = _apply_decisions(pages, located)
    if any(item["meta"].get("reviewed") is True for item in edited.values()):
        return _review_result(
            "suggestion",
            None,
            findings
            + [
                {
                    "code": "reviewed_suggestion",
                    "severity": "record",
                    "detail": "reviewed pages were not mutated",
                }
            ],
            project_id,
            suggestion=_rendered(edited),
        )
    proposal = _curation_proposal(project_id, files, edited, findings)
    return _review_result(
        "proposed",
        proposal.model_dump(mode="json"),
        findings,
        project_id,
        operation_id=proposal.operation_id,
    )


def _clock(now: datetime | date | None) -> datetime | date | None:
    if now is None:
        return None
    if isinstance(now, datetime):
        if now.tzinfo is None or now.tzinfo.utcoffset(now) is None:
            raise MemoryError("naive_now")
        return now
    if isinstance(now, date):
        return now
    raise MemoryError("invalid_now")


def _error(path: str, code: str, *, detail: str, project_id: str) -> dict[str, Any]:
    return {
        "code": code,
        "severity": "error",
        "project_id": project_id,
        "path": path,
        "detail": detail,
        "canonical_gc": False,
        "updated_revalidates": False,
        "winner": None,
    }


def _page_claims(
    path: str,
    meta: dict[str, Any],
    files: Mapping[str, str],
    clock: datetime | date | None,
    findings: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    claims = meta.get("claims")
    if "claims" not in meta and meta.get("knowledge_schema") != 1:
        if meta.get("type") in {"entity", "concept", "source"}:
            findings.append(
                {
                    "code": "legacy_not_evaluated",
                    "severity": "insufficient",
                    "project_id": meta.get("project_id"),
                    "path": path,
                    "detail": "legacy page is not evaluated and not supported",
                    "support": "not_evaluated",
                    "canonical_gc": False,
                    "updated_revalidates": False,
                    "winner": None,
                }
            )
        return []
    if not isinstance(claims, list):
        findings.append(
            _error(
                path,
                "invalid_claims",
                detail="claims must be a list",
                project_id=str(meta.get("project_id")),
            )
        )
        return []
    concept = meta.get("concept") if isinstance(meta.get("concept"), str) else None
    review_state = "reviewed" if meta.get("reviewed") is True else "pending"
    page_updated = _time_text(meta.get("updated"))
    page_created = _time_text(meta.get("created"))
    records = []
    for claim in claims:
        if not isinstance(claim, dict):
            findings.append(
                _error(
                    path,
                    "invalid_claim",
                    detail="claim must be an object",
                    project_id=str(meta.get("project_id")),
                )
            )
            continue
        records.append(
            _claim_record(
                path,
                meta,
                concept,
                review_state,
                page_updated,
                page_created,
                claim,
                files,
                clock,
                findings,
            )
        )
    return records


def _claim_record(
    path: str,
    meta: dict[str, Any],
    concept: str | None,
    review_state: str,
    page_updated: str | None,
    page_created: str | None,
    claim: dict[str, Any],
    files: Mapping[str, str],
    clock: datetime | date | None,
    findings: list[dict[str, Any]],
) -> dict[str, Any]:
    project_id = claim.get("project_id")
    times = {
        key: _classify_time(claim.get(key))
        for key in (
            "observed_at",
            "recorded_at",
            "valid_from",
            "valid_until",
        )
    }
    for key, (text, kind) in times.items():
        raw = claim.get(key)
        if raw not in (None, "") and kind == "invalid":
            findings.append(
                _error(
                    path,
                    "invalid_date",
                    detail=f"{key} is not an explicit date",
                    project_id=str(project_id),
                )
            )
        elif kind == "naive":
            findings.append(
                {
                    "code": "timezone_unknown",
                    "severity": "insufficient",
                    "project_id": project_id,
                    "path": path,
                    "claim_id": claim.get("id"),
                    "field": key,
                    "detail": "datetime without a timezone is not comparable",
                    "canonical_gc": False,
                    "updated_revalidates": False,
                    "winner": None,
                }
            )
    if claim.get("kind") not in KINDS or claim.get("category") not in CATEGORIES:
        findings.append(
            _error(
                path,
                "invalid_claim_type",
                detail="kind or category is outside the contract",
                project_id=str(project_id),
            )
        )
    if claim.get("support") not in SUPPORTS:
        findings.append(
            _error(
                path,
                "invalid_support",
                detail="support is outside the contract",
                project_id=str(project_id),
            )
        )
    if claim.get("support") == "supported" and not _assessment_complete(
        claim.get("assessment")
    ):
        findings.append(
            _error(
                path,
                "support_without_assessment",
                detail="supported requires an explicit assessment",
                project_id=str(project_id),
            )
        )
    _check_evidence(path, claim, files, findings)
    if times["observed_at"][1] == "absent":
        findings.append(
            {
                "code": "observed_at_unknown",
                "severity": "insufficient",
                "project_id": project_id,
                "path": path,
                "claim_id": claim.get("id"),
                "page_updated": page_updated,
                "detail": "updated is not an observation and does not revalidate",
                "canonical_gc": False,
                "updated_revalidates": False,
                "winner": None,
            }
        )
    window = _window_state(times, clock)
    if window == "not_compared" and (
        times["valid_from"][1] not in {"absent", "invalid", "naive"}
        or times["valid_until"][1] not in {"absent", "invalid", "naive"}
    ):
        findings.append(
            {
                "code": "validity_not_compared",
                "severity": "insufficient",
                "project_id": project_id,
                "path": path,
                "claim_id": claim.get("id"),
                "detail": "provided validity was not compared without an explicit now",
                "canonical_gc": False,
                "updated_revalidates": False,
                "winner": None,
            }
        )
    elif window in {"elapsed", "not_yet"}:
        findings.append(
            {
                "code": "validity_elapsed" if window == "elapsed" else "not_yet_valid",
                "severity": "insufficient",
                "project_id": project_id,
                "path": path,
                "claim_id": claim.get("id"),
                "detail": "provided validity does not make another claim the winner",
                "canonical_gc": False,
                "updated_revalidates": False,
                "winner": None,
            }
        )
    current: bool | None
    if claim.get("support") == "retired" or window in {"elapsed", "not_yet"}:
        current = False
    elif window == "inside":
        current = True
    else:
        current = None
    return {
        "code": "claim_status",
        "severity": "record",
        "project_id": project_id,
        "path": path,
        "page_project_id": meta.get("project_id"),
        "concept": concept,
        "claim_id": claim.get("id"),
        "category": claim.get("category"),
        "kind": claim.get("kind"),
        "support": claim.get("support"),
        "subject": claim.get("subject"),
        "predicate": claim.get("predicate"),
        "value": claim.get("value"),
        "supersedes": supersedes_ids(claim.get("supersedes")),
        "review_state": review_state,
        "recorded_at": times["recorded_at"][0],
        "observed_at": times["observed_at"][0],
        "valid_from": times["valid_from"][0],
        "valid_until": times["valid_until"][0],
        "page_updated": page_updated,
        "page_created": page_created,
        "updated_revalidates": False,
        "page_updated_is_observed": False,
        "current": current,
        "winner": None,
        "newer_wins": False,
        "canonical_gc": False,
        "evidence": claim.get("evidence")
        if isinstance(claim.get("evidence"), list)
        else [],
    }


def _check_evidence(
    path: str,
    claim: dict[str, Any],
    files: Mapping[str, str],
    findings: list[dict[str, Any]],
) -> None:
    evidence = claim.get("evidence")
    if evidence in (None, []):
        if claim.get("kind") == "fact":
            findings.append(
                {
                    "code": "fact_without_evidence",
                    "severity": "insufficient",
                    "project_id": claim.get("project_id"),
                    "path": path,
                    "claim_id": claim.get("id"),
                    "detail": "a fact without a span is not certified",
                    "canonical_gc": False,
                    "updated_revalidates": False,
                    "winner": None,
                }
            )
        return
    if not isinstance(evidence, list):
        findings.append(
            _error(
                path,
                "invalid_evidence",
                detail="evidence must be a list",
                project_id=str(claim.get("project_id")),
            )
        )
        return
    for span in evidence:
        if not isinstance(span, dict):
            findings.append(
                _error(
                    path,
                    "invalid_evidence",
                    detail="span must be an object",
                    project_id=str(claim.get("project_id")),
                )
            )
            continue
        code = _span_problem(span, files)
        if code == "evidence_unavailable":
            findings.append(
                {
                    "code": code,
                    "severity": "insufficient",
                    "project_id": claim.get("project_id"),
                    "path": path,
                    "claim_id": claim.get("id"),
                    "evidence_path": span.get("path"),
                    "detail": "raw bytes were not in the audited files",
                    "canonical_gc": False,
                    "updated_revalidates": False,
                    "winner": None,
                }
            )
        elif code:
            findings.append(
                _error(
                    path,
                    code,
                    detail="evidence bytes do not match the span",
                    project_id=str(claim.get("project_id")),
                )
            )


def _span_problem(span: dict[str, Any], files: Mapping[str, str]) -> str | None:
    source = span.get("path")
    if not isinstance(source, str) or source not in files:
        return "evidence_unavailable"
    content = files[source]
    if not isinstance(content, str):
        return "evidence_unavailable"
    raw = content.encode()
    if span.get("sha256") != digest(raw):
        return "evidence_hash_mismatch"
    start, end = span.get("start"), span.get("end")
    if (
        not isinstance(start, int)
        or not isinstance(end, int)
        or start < 0
        or end < start
        or end > len(raw)
    ):
        return "evidence_span_invalid"
    try:
        quote = raw[start:end].decode("utf-8")
    except UnicodeDecodeError:
        return "evidence_quote_mismatch"
    if quote != span.get("quote"):
        return "evidence_quote_mismatch"
    return None


def _classify_time(value: Any) -> tuple[str | None, str]:
    if value in (None, ""):
        return None, "absent"
    if isinstance(value, datetime):
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            return value.isoformat(), "naive"
        return value.isoformat(), "aware"
    if isinstance(value, date):
        return value.isoformat(), "date"
    if not isinstance(value, str):
        return None, "invalid"
    if _DATE.fullmatch(value):
        return value, "date"
    if _NAIVE.fullmatch(value):
        return value, "naive"
    if _AWARE.fullmatch(value):
        return value, "aware"
    return None, "invalid"


def _time_text(value: Any) -> str | None:
    text, kind = _classify_time(value)
    if kind in {"date", "aware", "naive"}:
        return text
    return None


def _window_state(
    times: dict[str, tuple[str | None, str]], clock: datetime | date | None
) -> str | None:
    start, start_kind = times["valid_from"]
    end, end_kind = times["valid_until"]
    if start_kind in {"invalid", "naive"} or end_kind in {"invalid", "naive"}:
        return None
    if start_kind == "absent" and end_kind == "absent":
        return None
    if clock is None:
        return "not_compared"
    if start is not None and _before(clock, start, start_kind):
        return "not_yet"
    if end is not None and _after(clock, end, end_kind):
        return "elapsed"
    return "inside"


def _as_instant(value: str, kind: str) -> datetime | date:
    if kind == "date":
        return date.fromisoformat(value)
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    return datetime.fromisoformat(text)


def _before(clock: datetime | date, value: str, kind: str) -> bool:
    parsed = _as_instant(value, kind)
    if isinstance(clock, datetime) and isinstance(parsed, datetime):
        return clock < parsed
    return _calendar(clock) < _calendar(parsed)


def _after(clock: datetime | date, value: str, kind: str) -> bool:
    parsed = _as_instant(value, kind)
    if isinstance(clock, datetime) and isinstance(parsed, datetime):
        return clock > parsed
    return _calendar(clock) > _calendar(parsed)


def _calendar(value: datetime | date) -> date:
    if isinstance(value, datetime):
        return value.date()
    return value


def _assessment_complete(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    return all(
        isinstance(value.get(key), str) and value[key]
        for key in ("verdict", "reviewer", "reason", "assessed_at")
    )


def _contradictions(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for record in records:
        if record.get("support") == "retired":
            continue
        subject, predicate = record.get("subject"), record.get("predicate")
        value = record.get("value")
        if not all(
            isinstance(item, str) and item for item in (subject, predicate, value)
        ):
            continue
        key = (record.get("project_id"), record.get("concept"), subject, predicate)
        groups.setdefault(key, []).append(record)
    findings = []
    for (project_id, concept, subject, predicate), group in groups.items():
        values = {record.get("value") for record in group}
        if len(values) < 2:
            continue
        ids = {record.get("claim_id") for record in group}
        if any(set(supersedes_ids(record.get("supersedes"))) & ids for record in group):
            continue
        findings.append(
            {
                "code": "possible_contradiction",
                "severity": "contradiction",
                "project_id": project_id,
                "concept": concept,
                "subject": subject,
                "predicate": predicate,
                "winner": None,
                "newer_wins": False,
                "values": sorted(value for value in values if isinstance(value, str)),
                "claims": [
                    {
                        "claim_id": record.get("claim_id"),
                        "value": record.get("value"),
                        "observed_at": record.get("observed_at"),
                        "recorded_at": record.get("recorded_at"),
                        "path": record.get("path"),
                        "support": record.get("support"),
                    }
                    for record in group
                ],
                "canonical_gc": False,
                "updated_revalidates": False,
            }
        )
    return findings


def _homonyms(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    folded: dict[tuple[str, str, str], set[tuple[Any, ...]]] = {}
    for record in records:
        subject, predicate = record.get("subject"), record.get("predicate")
        if not isinstance(subject, str) or not isinstance(predicate, str):
            continue
        key = (_fold(subject), _fold(predicate))
        folded.setdefault(key, set()).add(
            (record.get("project_id"), record.get("concept"), subject, predicate)
        )
    findings = []
    for identities in folded.values():
        if len(identities) < 2:
            continue
        findings.append(
            {
                "code": "homonym_not_fused",
                "severity": "insufficient",
                "detail": "case, project or unicode differences stay separate",
                "identities": [
                    {
                        "project_id": project,
                        "concept": concept,
                        "subject": subject,
                        "predicate": predicate,
                    }
                    for project, concept, subject, predicate in sorted(
                        identities,
                        key=lambda item: json.dumps(item, ensure_ascii=False),
                    )
                ],
                "winner": None,
                "newer_wins": False,
                "canonical_gc": False,
                "updated_revalidates": False,
            }
        )
    return findings


def _fold(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text)
    stripped = "".join(char for char in normalized if not unicodedata.combining(char))
    return stripped.casefold()


def _supersessions(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id = {record.get("claim_id"): record for record in records}
    findings = []
    for record in records:
        for target in supersedes_ids(record.get("supersedes")):
            previous = by_id.get(target)
            findings.append(
                {
                    "code": "supersession",
                    "severity": "history",
                    "project_id": record.get("project_id"),
                    "claim_id": record.get("claim_id"),
                    "supersedes": target,
                    "previous_present": previous is not None,
                    "previous_path": None if previous is None else previous.get("path"),
                    "evidence_paths": _evidence_paths(record)
                    + (_evidence_paths(previous) if previous else []),
                    "detail": "history, old links and raw stay",
                    "winner": None,
                    "newer_wins": False,
                    "canonical_gc": False,
                    "updated_revalidates": False,
                }
            )
            if previous is not None:
                previous["current"] = False
    return findings


def _evidence_paths(record: dict[str, Any] | None) -> list[str]:
    if not record:
        return []
    paths = []
    for span in record.get("evidence") or []:
        if isinstance(span, dict) and isinstance(span.get("path"), str):
            paths.append(span["path"])
    return paths


def _retired_paths(pages: list[tuple[str, dict[str, Any], str, str]]) -> set[str]:
    retired = set()
    for path, meta, _body, _content in pages:
        status = meta.get("status")
        if (
            meta.get("rejected") is True
            or meta.get("retired") is True
            or status
            in {
                "rejected",
                "retired",
            }
        ):
            retired.add(path)
            for link in meta.get("sources") or []:
                if (
                    isinstance(link, str)
                    and link.startswith("[[")
                    and link.endswith("]]")
                ):
                    retired.add(link[2:-2])
            note = meta.get("source_note")
            if isinstance(note, str) and note.startswith("[[") and note.endswith("]]"):
                retired.add(note[2:-2])
    return retired


def _retired_alerts(
    records: list[dict[str, Any]], retired_paths: set[str]
) -> list[dict[str, Any]]:
    findings = []
    for record in records:
        if record.get("support") not in ACTIVE:
            continue
        cited = [path for path in _evidence_paths(record) if path in retired_paths]
        if record.get("path") in retired_paths:
            cited.append(record["path"])
        if not cited:
            continue
        findings.append(
            {
                "code": "retired_source_supports_active_claim",
                "severity": "alert",
                "project_id": record.get("project_id"),
                "path": record.get("path"),
                "claim_id": record.get("claim_id"),
                "support": record.get("support"),
                "sources": sorted(set(cited)),
                "detail": "rejected or retired source still supports an active claim",
                "winner": None,
                "canonical_gc": False,
                "updated_revalidates": False,
            }
        )
    return findings


def _concept_pages(
    files: Mapping[str, str], project_id: str
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    prefix = corpus(project_id) + "/wiki/concepts/"
    pages = {}
    problems = []
    for path, content in sorted(files.items()):
        if not path.startswith(prefix) or not isinstance(content, str):
            continue
        if not content.startswith("---\n"):
            problems.append(
                _error(
                    path,
                    "invalid_page",
                    detail="frontmatter_required",
                    project_id=project_id,
                )
            )
            continue
        try:
            meta, body = parse_page(content)
        except MemoryError as exc:
            problems.append(
                _error(path, "invalid_page", detail=str(exc), project_id=project_id)
            )
            continue
        if meta.get("project_id") != project_id:
            continue
        page_id = meta.get("page_id")
        claims = meta.get("claims")
        if not isinstance(page_id, str) or not page_id or not isinstance(claims, list):
            problems.append(
                _error(
                    path,
                    "invalid_page",
                    detail="page_id_and_claims_required",
                    project_id=project_id,
                )
            )
            continue
        pages[path] = {
            "path": path,
            "content": content,
            "meta": meta,
            "body": body,
            "claims": [dict(claim) for claim in claims if isinstance(claim, dict)],
        }
    return pages, problems


def _bind_decisions(
    pages: dict[str, dict[str, Any]], decisions: list[dict[str, Any]]
) -> tuple[list[tuple[str, dict[str, Any]]], list[dict[str, Any]]]:
    index: dict[str, str] = {}
    problems = []
    for path, page in pages.items():
        for claim in page["claims"]:
            claim_id = claim.get("id")
            if not isinstance(claim_id, str):
                continue
            if claim_id in index:
                problems.append(
                    {
                        "code": "ambiguous_claim",
                        "severity": "error",
                        "claim_id": claim_id,
                    }
                )
            index[claim_id] = path
    bound = []
    seen: set[tuple[str, str, str]] = set()
    reserved = set(index)
    for decision in decisions:
        if not isinstance(decision, dict) or decision.get("action") not in _ACTIONS:
            problems.append({"code": "invalid_decision", "severity": "error"})
            continue
        claim_id = decision.get("claim_id")
        if claim_id not in index:
            problems.append(
                {
                    "code": "unknown_claim",
                    "severity": "error",
                    "claim_id": claim_id,
                }
            )
            continue
        action = decision["action"]
        if (
            action == "reobserve"
            and _provided_time(decision.get("observed_at")) is None
        ):
            problems.append(
                {
                    "code": "observed_at_required",
                    "severity": "error",
                    "claim_id": claim_id,
                }
            )
            continue
        if action == "pointer" and not _plain(decision.get("pointer")):
            problems.append(
                {"code": "pointer_required", "severity": "error", "claim_id": claim_id}
            )
            continue
        if action == "supersede" and not _plain(decision.get("text")):
            problems.append(
                {"code": "text_required", "severity": "error", "claim_id": claim_id}
            )
            continue
        material = _decision_material(decision)
        signature = (str(claim_id), action, material)
        if signature in seen:
            problems.append(
                {
                    "code": "duplicate_decision",
                    "severity": "error",
                    "claim_id": claim_id,
                }
            )
            continue
        if action in {"pointer", "supersede"}:
            derived = _derived_id(pages[index[claim_id]], claim_id, action, material)
            if derived in reserved:
                problems.append(
                    {
                        "code": "duplicate_decision",
                        "severity": "error",
                        "claim_id": claim_id,
                        "detail": derived,
                    }
                )
                continue
            reserved.add(derived)
        seen.add(signature)
        bound.append((index[claim_id], decision))
    return bound, problems


def _apply_decisions(
    pages: dict[str, dict[str, Any]],
    bound: list[tuple[str, dict[str, Any]]],
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    edited = {
        path: {**page, "claims": [dict(claim) for claim in page["claims"]]}
        for path, page in pages.items()
    }
    findings = []
    touched = set()
    for path, decision in bound:
        page = edited[path]
        claim = next(
            item for item in page["claims"] if item.get("id") == decision["claim_id"]
        )
        action = decision["action"]
        touched.add(path)
        if action == "retire":
            claim["support"] = "retired"
            findings.append(_curator(action, claim, "claim retired; evidence kept"))
        elif action == "reobserve":
            claim["observed_at"] = decision["observed_at"]
            if claim.get("support") == "supported":
                claim["support"] = "source_linked"
            findings.append(
                _curator(action, claim, "reobservation is not semantic certification")
            )
        elif action == "pointer":
            page["claims"].append(_derived_claim(page, claim, decision, "pointer"))
            findings.append(
                _curator(action, claim, "pointer added; previous claim and raw kept")
            )
        else:
            page["claims"].append(_derived_claim(page, claim, decision, "supersede"))
            findings.append(
                _curator(action, claim, "new claim supersedes; previous text kept")
            )
    return {path: edited[path] for path in touched}, findings


def _decision_material(decision: dict[str, Any]) -> str:
    action = decision.get("action")
    if action == "pointer":
        value = decision.get("pointer")
    elif action == "supersede":
        value = decision.get("text")
    else:
        value = ""
    return value if isinstance(value, str) else ""


def _derived_id(page: dict[str, Any], claim_id: Any, action: str, text: str) -> str:
    return (
        "k"
        + digest(
            "\0".join(
                [
                    str(page["meta"].get("project_id")),
                    action,
                    str(claim_id),
                    text,
                ]
            )
        )[:32]
    )


def _derived_claim(
    page: dict[str, Any],
    claim: dict[str, Any],
    decision: dict[str, Any],
    action: str,
) -> dict[str, Any]:
    text = decision["pointer"] if action == "pointer" else decision["text"]
    subject = (
        decision.get("subject") if isinstance(decision.get("subject"), str) else None
    )
    predicate = (
        decision.get("predicate")
        if isinstance(decision.get("predicate"), str)
        else None
    )
    value = decision.get("value") if isinstance(decision.get("value"), str) else None
    new_id = _derived_id(page, claim.get("id"), action, text)
    return {
        "id": new_id,
        "project_id": page["meta"].get("project_id"),
        "text": text,
        "kind": "hypothesis",
        "category": "observation",
        "support": "not_evaluated",
        "evidence": [],
        "assessment": None,
        "observed_at": _provided_time(decision.get("observed_at")),
        "recorded_at": claim.get("recorded_at"),
        "valid_from": None,
        "valid_until": None,
        "supersedes": supersedes_ids(claim.get("id")),
        "subject": subject,
        "predicate": predicate,
        "value": value,
    }


def _curator(action: str, claim: dict[str, Any], detail: str) -> dict[str, Any]:
    return {
        "code": "curator_decision",
        "severity": "record",
        "action": action,
        "claim_id": claim.get("id"),
        "detail": detail,
        "canonical_gc": False,
        "raw_deleted": False,
        "network": False,
    }


def _rendered(pages: dict[str, dict[str, Any]]) -> list[dict[str, str]]:
    return [
        {"path": path, "content": _render_curated(page)}
        for path, page in sorted(pages.items())
    ]


def _curation_proposal(
    project_id: str,
    files: Mapping[str, str],
    pages: dict[str, dict[str, Any]],
    findings: list[dict[str, Any]],
) -> MemoryProposal:
    changes = []
    hashes = {}
    for path, page in sorted(pages.items()):
        content = _render_curated(page)
        changes.append(
            {
                "page_id": page["meta"]["page_id"],
                "path": path,
                "base_sha256": digest(page["content"]),
                "content": content,
            }
        )
        for link in page["meta"].get("sources") or []:
            if isinstance(link, str) and link.startswith("[[") and link.endswith("]]"):
                source = link[2:-2]
                if source in files and "/raw/" in source:
                    hashes[source] = digest(files[source])
    payload = json.dumps(
        {
            "bases": {change["path"]: change["base_sha256"] for change in changes},
            "contents": {
                change["path"]: digest(change["content"]) for change in changes
            },
            "project_id": project_id,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    operation_id = "cur-" + digest(payload)[:40]
    return MemoryProposal.model_validate(
        {
            "schema_version": 1,
            "batch_id": operation_id,
            "operation_id": operation_id,
            "project_id": project_id,
            "base_commit": None,
            "source_hashes": hashes,
            "changes": changes,
            "citations": [f"[[{path}]]" for path in sorted(hashes)],
            "findings": findings,
        }
    )


def _render_curated(page: dict[str, Any]) -> str:
    meta = page["meta"]
    compiler = (
        meta.get("compiler")
        if isinstance(meta.get("compiler"), dict)
        else {
            "version": "curation",
            "operation_id": "curation",
            "sources": [],
        }
    )
    sources = []
    for link in meta.get("sources") or []:
        if isinstance(link, str) and link.startswith("[[") and link.endswith("]]"):
            sources.append(link[2:-2])
    aliases = [alias for alias in meta.get("aliases") or [] if isinstance(alias, str)]
    tags = [tag for tag in meta.get("tags") or [] if isinstance(tag, str)]
    page_id = meta.get("page_id")
    project = meta.get("project_id")
    if not isinstance(page_id, str) or not isinstance(project, str):
        raise MemoryError("invalid_page")
    concept = (
        meta.get("concept") if isinstance(meta.get("concept"), str) else page_id
    )
    title = aliases[0] if aliases else concept
    preserved = ""
    marker = "## Contexto anterior\n"
    if marker in page["body"]:
        preserved = page["body"].split(marker, 1)[1].strip()
    return render_knowledge_page(
        project_id=project,
        page_id=page_id,
        concept=concept,
        title=title,
        claims=[canonical_claim(claim) for claim in page["claims"]],
        source_paths=sources,
        aliases=aliases or [title],
        tags=tags,
        reviewed=bool(meta.get("reviewed")),
        compiler=compiler,
        preserved=preserved,
        created=str(meta.get("created") or "1970-01-01"),
        updated=str(meta.get("updated") or "1970-01-01"),
    )


def _review_result(
    state: str,
    proposal: dict[str, Any] | None,
    findings: list[dict[str, Any]],
    project_id: str,
    *,
    suggestion: list[dict[str, str]] | None = None,
    operation_id: str | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "state": state,
        "operation_id": operation_id,
        "project_id": project_id,
        "findings": findings,
        "limits": {
            "network": False,
            "canonical_gc": False,
            "raw_deleted": False,
            "semantic_certification": False,
            "max_pages": 20,
        },
    }
    if proposal is not None:
        result["proposal"] = proposal
    if suggestion is not None:
        result["suggestion"] = suggestion
    return result


def _provided_time(value: Any) -> str | None:
    text, kind = _classify_time(value)
    if kind in {"date", "aware"}:
        return text
    return None


def _plain(value: Any) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and not any(ord(char) < 32 or ord(char) == 127 for char in value)
    )


# Imported so callers can pin an explicit zone without constructing one ad hoc.
UTC = timezone.utc
