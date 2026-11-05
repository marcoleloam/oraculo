import json
import unicodedata
from datetime import datetime, timezone

import pytest

from oraculo.memory.claims import KnowledgeClaim, validate_knowledge
from oraculo.memory.compiler import CLAIM_FIELDS, render_knowledge_page
from oraculo.memory.freshness import build_review_proposal, freshness_audit
from oraculo.memory.schema import digest, parse_page, render_page


def claim(**overrides):
    value = dict.fromkeys(CLAIM_FIELDS, None)
    value.update(
        id="k-base",
        project_id="alpha",
        text="afirmativa",
        kind="fact",
        category="observation",
        support="source_linked",
        evidence=[],
        assessment=None,
    )
    value.update(overrides)
    return value


def concept(claims, *, concept="fila", reviewed=False, updated="2020-01-01", path=None):
    page_id = "calphaqueue"
    relative = path or f"projects/alpha/wiki/concepts/alpha--{page_id}.md"
    content = render_knowledge_page(
        project_id="alpha",
        page_id=page_id,
        concept=concept,
        title=concept,
        claims=claims,
        source_paths=["projects/alpha/raw/" + "a" * 64 + ".md"],
        aliases=[concept],
        tags=[],
        reviewed=reviewed,
        compiler={
            "version": "1",
            "operation_id": "cmp-test",
            "sources": [
                {
                    "path": "projects/alpha/raw/" + "a" * 64 + ".md",
                    "sha256": "a" * 64,
                }
            ],
        },
        preserved="",
        created="2020-01-01",
        updated=updated,
    )
    return relative, content


def test_absent_dates_timezones_and_updated_are_not_revalidation():
    raw = "projects/alpha/raw/" + "b" * 64 + ".md"
    body = "observação com café"
    start = body.encode().find("café".encode())
    span = {
        "path": raw,
        "sha256": digest(body),
        "start": start,
        "end": start + len("café".encode()),
        "quote": "café",
    }
    aware = claim(
        id="k-aware",
        text="café",
        observed_at="2024-06-01T12:00:00-03:00",
        recorded_at="2024-06-01T15:00:00Z",
        evidence=[span],
        subject="fila",
        predicate="estado",
        value="aberta",
    )
    missing = claim(id="k-missing", observed_at=None, evidence=[])
    naive = claim(id="k-naive", observed_at="2024-06-01T12:00:00", kind="hypothesis")
    invalid = claim(id="k-invalid", valid_until="ontem", kind="hypothesis")
    path, content = concept([aware, missing, naive, invalid], updated="2020-01-01")
    changed = content.replace("updated: '2020-01-01'", "updated: '2026-11-01'", 1)
    if changed == content:
        changed = content.replace("updated: 2020-01-01", "updated: 2026-11-01", 1)
    assert changed != content
    files = {path: content, raw: body}
    audit = freshness_audit(
        files, "alpha", now=datetime(2026, 11, 1, tzinfo=timezone.utc)
    )
    statuses = [item for item in audit if item["code"] == "claim_status"]
    by_id = {item["claim_id"]: item for item in statuses}
    assert by_id["k-missing"]["current"] is None
    assert by_id["k-missing"]["observed_at"] is None
    assert by_id["k-missing"]["page_updated"] == "2020-01-01"
    assert by_id["k-missing"]["updated_revalidates"] is False
    assert by_id["k-aware"]["recorded_at"] == "2024-06-01T15:00:00Z"
    assert by_id["k-aware"]["observed_at"] == "2024-06-01T12:00:00-03:00"
    assert by_id["k-aware"]["current"] is None
    codes = {item["code"] for item in audit}
    assert "observed_at_unknown" in codes
    assert "timezone_unknown" in codes
    assert "invalid_date" in codes
    assert any(
        item["code"] == "invalid_date" and item["severity"] == "error" for item in audit
    )
    assert any(
        item["code"] == "timezone_unknown" and item["severity"] == "insufficient"
        for item in audit
    )
    again = freshness_audit(
        {path: changed, raw: body},
        "alpha",
        now=datetime(2026, 11, 1, tzinfo=timezone.utc),
    )
    again_status = next(
        item
        for item in again
        if item["code"] == "claim_status" and item["claim_id"] == "k-aware"
    )
    assert again_status["observed_at"] == by_id["k-aware"]["observed_at"]
    assert again_status["page_updated"] == "2026-11-01"
    assert again_status["support"] == "source_linked"
    assert again_status["updated_revalidates"] is False


def test_contradiction_does_not_let_the_newer_claim_win():
    older = claim(
        id="k-old",
        subject="limit",
        predicate="max",
        value="1",
        observed_at="2020-01-01T00:00:00Z",
        kind="hypothesis",
    )
    newer = claim(
        id="k-new",
        subject="limit",
        predicate="max",
        value="3",
        observed_at="2026-06-01T00:00:00+02:00",
        kind="hypothesis",
    )
    path, content = concept([older, newer])
    audit = freshness_audit(
        {path: content}, "alpha", now=datetime(2026, 11, 1, tzinfo=timezone.utc)
    )
    found = next(item for item in audit if item["code"] == "possible_contradiction")
    assert found["severity"] == "contradiction"
    assert found["winner"] is None
    assert found["newer_wins"] is False
    assert {item["value"] for item in found["claims"]} == {"1", "3"}
    assert "validity_elapsed" not in {item["code"] for item in audit}


def test_quote_mismatch_is_a_mechanical_error():
    raw = "projects/alpha/raw/" + "c" * 64 + ".md"
    body = "texto real"
    bad = claim(
        id="k-bad",
        evidence=[
            {
                "path": raw,
                "sha256": digest(body),
                "start": 0,
                "end": 5,
                "quote": "falso",
            }
        ],
    )
    path, content = concept([bad])
    audit = freshness_audit({path: content, raw: body}, "alpha")
    assert any(
        item["code"] == "evidence_quote_mismatch" and item["severity"] == "error"
        for item in audit
    )
    assert not any(item["code"] == "possible_contradiction" for item in audit)


def test_review_state_supersession_and_scope_homonyms():
    raw = "projects/alpha/raw/" + "d" * 64 + ".md"
    previous = claim(
        id="k-previous",
        text="estado antigo",
        subject="fila",
        predicate="estado",
        value="antiga",
        kind="hypothesis",
        evidence=[
            {
                "path": raw,
                "sha256": digest("bruto"),
                "start": 0,
                "end": 5,
                "quote": "bruto",
            }
        ],
    )
    successor = claim(
        id="k-next",
        text="ponteiro novo",
        subject="fila",
        predicate="estado",
        value="nova",
        kind="hypothesis",
        supersedes=["k-previous"],
        observed_at="2026-01-01T00:00:00Z",
    )
    pending, pending_body = concept([previous, successor], reviewed=False)
    reviewed_claim = claim(id="k-reviewed", text="já revisada", kind="hypothesis")
    reviewed, reviewed_body = concept(
        [reviewed_claim],
        concept="outra",
        reviewed=True,
        path="projects/alpha/wiki/concepts/alpha--reviewed.md",
    )
    cafe = claim(
        id="k-cafe",
        subject="café",
        predicate="estado",
        value="um",
        kind="hypothesis",
        project_id="alpha",
    )
    cafe_page, cafe_body = concept(
        [cafe], concept="café", path="projects/alpha/wiki/concepts/alpha--cafe.md"
    )
    decomposed = unicodedata.normalize("NFD", "café")
    other = claim(
        id="k-decomposed",
        subject=decomposed,
        predicate="estado",
        value="dois",
        kind="hypothesis",
    )
    other_page, other_body = concept(
        [other],
        concept="cafe-nfd",
        path="projects/alpha/wiki/concepts/alpha--nfd.md",
    )
    foreign = "projects/beta/wiki/concepts/beta--same.md"
    files = {
        pending: pending_body,
        reviewed: reviewed_body,
        cafe_page: cafe_body,
        other_page: other_body,
        raw: "bruto",
        foreign: "beta-secret não pode entrar",
        "projects/Alpha/wiki/concepts/Alpha--same.md": pending_body,
    }
    before = json.dumps(files, ensure_ascii=False)
    audit = freshness_audit(files, "alpha")
    assert before == json.dumps(files, ensure_ascii=False)
    statuses = {
        item["claim_id"]: item for item in audit if item["code"] == "claim_status"
    }
    assert statuses["k-reviewed"]["review_state"] == "reviewed"
    assert statuses["k-previous"]["review_state"] == "pending"
    assert statuses["k-previous"]["current"] is False
    history = next(item for item in audit if item["code"] == "supersession")
    assert history["previous_present"] is True
    assert raw in history["evidence_paths"]
    assert history["canonical_gc"] is False
    assert history["winner"] is None
    assert not any(
        item["code"] == "possible_contradiction"
        and item["subject"] in {"fila", "café", decomposed}
        for item in audit
    )
    assert any(item["code"] == "homonym_not_fused" for item in audit)
    assert any(item["code"] == "out_of_scope_ignored" for item in audit)
    assert "beta-secret" not in json.dumps(audit, ensure_ascii=False)
    limits = next(item for item in audit if item["code"] == "audit_limits")
    assert limits["network"] is False
    assert limits["canonical_gc"] is False
    assert limits["newer_wins"] is False


def test_preferences_do_not_expire_for_inactivity_and_retired_sources_alert():
    preference = claim(
        id="k-pref",
        text="prefiro filas explícitas",
        category="preference",
        kind="hypothesis",
        observed_at="2001-01-01",
        support="source_linked",
    )
    active = claim(
        id="k-active",
        text="ainda citada",
        kind="hypothesis",
        support="source_linked",
        evidence=[
            {
                "path": "projects/alpha/raw/" + "e" * 64 + ".md",
                "sha256": digest("fonte"),
                "start": 0,
                "end": 5,
                "quote": "fonte",
            }
        ],
    )
    path, content = concept([preference, active], updated="2001-01-01")
    retired = render_page(
        {
            "schema_version": 1,
            "type": "source",
            "page_id": "alpha--old-source",
            "project_id": "alpha",
            "created": "2001-01-01",
            "updated": "2001-01-01",
            "sources": [["projects/alpha/raw/" + "e" * 64 + ".md"]],
            "tags": [],
            "aliases": ["fonte antiga"],
            "reviewed": False,
            "status": "retired",
        },
        "Fonte aposentada.\n",
    )
    # render_page will dump the nested list incorrectly if sources is not strings.
    meta, _body = parse_page(retired)
    meta["sources"] = ["[[projects/alpha/raw/" + "e" * 64 + ".md]]"]
    meta["status"] = "retired"
    retired = render_page(meta, "Fonte aposentada.\n")
    files = {
        path: content,
        "projects/alpha/wiki/sources/alpha--old-source.md": retired,
        "projects/alpha/raw/" + "e" * 64 + ".md": "fonte",
    }
    audit = freshness_audit(
        files, "alpha", now=datetime(2026, 11, 1, tzinfo=timezone.utc)
    )
    codes = [item["code"] for item in audit]
    assert not any("expir" in code or "inactiv" in code for code in codes)
    preference_status = next(
        item
        for item in audit
        if item.get("claim_id") == "k-pref" and item["code"] == "claim_status"
    )
    assert preference_status["current"] is None
    assert preference_status["category"] == "preference"
    alert = next(
        item for item in audit if item["code"] == "retired_source_supports_active_claim"
    )
    assert alert["severity"] == "alert"
    assert alert["claim_id"] == "k-active"
    assert "projects/alpha/raw/" + "e" * 64 + ".md" in alert["sources"]


def test_validity_requires_now_and_does_not_choose_a_winner():
    bounded = claim(
        id="k-window",
        kind="hypothesis",
        valid_from="2020-01-01",
        valid_until="2021-01-01",
        subject="limit",
        predicate="max",
        value="1",
    )
    later = claim(
        id="k-later",
        kind="hypothesis",
        observed_at="2026-01-01T00:00:00Z",
        subject="limit",
        predicate="max",
        value="9",
    )
    path, content = concept([bounded, later])
    without_now = freshness_audit({path: content}, "alpha")
    assert any(item["code"] == "validity_not_compared" for item in without_now)
    assert not any(item["code"] == "validity_elapsed" for item in without_now)
    compared = freshness_audit(
        {path: content}, "alpha", now=datetime(2026, 11, 1, tzinfo=timezone.utc)
    )
    assert any(item["code"] == "validity_elapsed" for item in compared)
    status = next(
        item
        for item in compared
        if item["code"] == "claim_status" and item["claim_id"] == "k-window"
    )
    assert status["current"] is False
    contradiction = next(
        item for item in compared if item["code"] == "possible_contradiction"
    )
    assert contradiction["winner"] is None
    with pytest.raises(Exception, match="naive_now"):
        freshness_audit({path: content}, "alpha", now=datetime(2026, 11, 1))


def test_review_proposal_is_pure_and_keeps_history():
    text = "valor antigo"
    raw = "projects/alpha/raw/" + digest(text) + ".md"
    original = claim(
        id="k-original",
        text=text,
        subject="limit",
        predicate="max",
        value="1",
        evidence=[
            {
                "path": raw,
                "sha256": digest(text),
                "start": 0,
                "end": len(text.encode()),
                "quote": text,
            }
        ],
    )
    path, content = concept([original])
    files = {path: content, raw: text}
    snapshot = json.dumps(files)
    empty = build_review_proposal(files, "alpha", [])
    assert empty["state"] == "noop"
    retired = build_review_proposal(
        files, "alpha", [{"claim_id": "k-original", "action": "retire"}]
    )
    assert retired["state"] == "proposed"
    proposal = retired["proposal"]
    meta, body = parse_page(proposal["changes"][0]["content"])
    kept = next(item for item in meta["claims"] if item["id"] == "k-original")
    assert kept["support"] == "retired"
    assert kept["evidence"][0]["path"] == raw
    assert meta["sources"] == ["[[projects/alpha/raw/" + "a" * 64 + ".md]]"]
    assert "valor antigo" in body
    retired_content = proposal["changes"][0]["content"]
    retired_findings = validate_knowledge(
        retired_content,
        "alpha",
        {proposal["changes"][0]["path"]: retired_content, raw: text},
    )
    assert not any(item["severity"] == "error" for item in retired_findings)
    assert retired["limits"]["raw_deleted"] is False
    assert retired["limits"]["canonical_gc"] is False
    repeated = build_review_proposal(
        files, "alpha", [{"claim_id": "k-original", "action": "retire"}]
    )
    assert repeated["operation_id"] == retired["operation_id"]
    reobserved = build_review_proposal(
        files,
        "alpha",
        [
            {
                "claim_id": "k-original",
                "action": "reobserve",
                "observed_at": "2026-11-01T00:00:00Z",
            }
        ],
    )
    reparsed = parse_page(reobserved["proposal"]["changes"][0]["content"])[0]
    assert reparsed["claims"][0]["observed_at"] == "2026-11-01T00:00:00Z"
    assert reparsed["claims"][0]["support"] == "source_linked"
    assert reparsed["claims"][0]["assessment"] is None
    pointed = build_review_proposal(
        files,
        "alpha",
        [{"claim_id": "k-original", "action": "pointer", "pointer": "system:queues"}],
    )
    pointed_claims = parse_page(pointed["proposal"]["changes"][0]["content"])[0][
        "claims"
    ]
    assert any(
        item["id"] == "k-original" and item["value"] == "1" for item in pointed_claims
    )
    assert any(
        item["supersedes"] == ["k-original"] and item["support"] == "not_evaluated"
        for item in pointed_claims
    )
    for item in pointed_claims:
        KnowledgeClaim.model_validate(item)
        assert item["recorded_at"] is None
        assert item["observed_at"] is None
    pointed_content = pointed["proposal"]["changes"][0]["content"]
    pointed_findings = validate_knowledge(
        pointed_content,
        "alpha",
        {path: pointed_content, raw: text},
    )
    assert not any(item["severity"] == "error" for item in pointed_findings)
    superseded = build_review_proposal(
        files,
        "alpha",
        [
            {
                "claim_id": "k-original",
                "action": "supersede",
                "text": "valor substituído",
                "subject": "limit",
                "predicate": "max",
                "value": "2",
            }
        ],
    )
    superseded_claims = parse_page(superseded["proposal"]["changes"][0]["content"])[0][
        "claims"
    ]
    assert {item["value"] for item in superseded_claims} == {"1", "2"}
    reviewed_meta = parse_page(content)[0]
    reviewed_meta["reviewed"] = True
    reviewed_files = {
        path: render_page(reviewed_meta, parse_page(content)[1]),
        raw: text,
    }
    suggestion = build_review_proposal(
        reviewed_files, "alpha", [{"claim_id": "k-original", "action": "retire"}]
    )
    assert suggestion["state"] == "suggestion"
    assert "proposal" not in suggestion
    assert suggestion["suggestion"][0]["path"] == path
    suggested = parse_page(suggestion["suggestion"][0]["content"])[0]
    assert suggested["reviewed"] is True
    assert json.dumps(files) == snapshot


def test_n_memory_freshness_eval_2_malformed_concept_rejects_review():
    """N-memory-freshness-eval-2: a stray concept page does not crash review."""

    path, content = concept([claim(id="k-original", text="valor antigo")])
    broken = "projects/alpha/wiki/concepts/alpha--broken.md"
    files = {
        path: content,
        "projects/alpha/wiki/concepts/notes.md": "sem frontmatter\n",
        broken: (
            "---\nproject_id: alpha\ntype: concept\nschema_version: 1\n---\n"
            "# sem identificador\n"
        ),
    }
    result = build_review_proposal(
        files, "alpha", [{"claim_id": "k-original", "action": "retire"}]
    )
    assert result["state"] == "rejected"
    assert result.get("operation_id") is None
    assert "proposal" not in result
    assert {item["code"] for item in result["findings"]} == {"invalid_page"}
    assert any(item["detail"] == "frontmatter_required" for item in result["findings"])
    assert any(
        item["detail"] == "page_id_and_claims_required" for item in result["findings"]
    )


def test_n_memory_freshness_eval_3_duplicate_decisions_reject():
    """N-memory-freshness-eval-3: repeated decisions are not a publishable page."""

    from oraculo.memory.freshness import _derived_id

    path, content = concept([claim(id="k-original", text="valor antigo")])
    files = {path: content}
    decision = {
        "claim_id": "k-original",
        "action": "pointer",
        "pointer": "system:queues",
    }
    repeated = build_review_proposal(files, "alpha", [decision, dict(decision)])
    assert repeated["state"] == "rejected"
    assert repeated.get("operation_id") is None
    assert "proposal" not in repeated
    assert any(item["code"] == "duplicate_decision" for item in repeated["findings"])
    derived = _derived_id(
        {"meta": {"project_id": "alpha"}},
        "k-original",
        "pointer",
        "system:queues",
    )
    occupied, occupied_content = concept(
        [
            claim(id="k-original", text="valor antigo"),
            claim(id=derived, text="system:queues", kind="hypothesis"),
        ]
    )
    collided = build_review_proposal(
        {occupied: occupied_content}, "alpha", [decision]
    )
    assert collided["state"] == "rejected"
    assert any(item["code"] == "duplicate_decision" for item in collided["findings"])
