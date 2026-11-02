import pytest
from test_memory_schema import page, proposal, publisher

from oraculo.memory import ReviewManager
from oraculo.memory.claims import (
    KnowledgeClaim,
    SourceSpan,
    validate_knowledge,
)
from oraculo.memory.schema import MemoryError, digest, parse_page, render_page


def meta(project="alpha", name="decision", **extra):
    payload = {
        "type": "concept",
        "created": "2026-10-18",
        "updated": "2026-10-18",
        "sources": [],
        "tags": [],
        "aliases": [],
        "reviewed": False,
        "page_id": f"{project}--{name}",
        "project_id": project,
        "schema_version": 1,
    }
    payload.update(extra)
    return payload


def document(claims, body="Nota sintética.", **extra):
    return render_page(meta(claims=claims, knowledge_schema=1, **extra), body)


def span(path, text, quote=None, start=None, end=None, sha=None):
    raw = text.encode()
    if quote is None:
        quote = text
        start, end = 0, len(raw)
    elif start is None:
        encoded = quote.encode()
        start = raw.find(encoded)
        end = start + len(encoded)
    return {
        "path": path,
        "sha256": sha or digest(text),
        "start": start,
        "end": end,
        "quote": quote,
    }


def claim(**extra):
    payload = {
        "id": "claim-1",
        "project_id": "alpha",
        "text": "A válvula fechou.",
        "kind": "fact",
        "category": "observation",
        "support": "source_linked",
        "evidence": [],
    }
    payload.update(extra)
    return payload


def codes(findings):
    return [item["code"] for item in findings]


def test_models_round_trip_contract_fields():
    evidence = SourceSpan(
        path="projects/alpha/raw/ab.md",
        sha256="a" * 64,
        start=0,
        end=4,
        quote="abcd",
    )
    parsed = KnowledgeClaim.model_validate(
        claim(
            evidence=[evidence.model_dump()],
            subject="válvula",
            predicate="fechou",
            value="sim",
            supersedes=["claim-0"],
        )
    )
    assert parsed.kind == "fact"
    assert parsed.evidence[0].end == 4
    assert parsed.observed_at is None


def test_legacy_page_is_not_evaluated_and_not_promoted():
    content = page()
    findings = validate_knowledge(
        content, "alpha", {"projects/alpha/wiki/x.md": content}
    )
    assert findings == [
        {
            "code": "not_evaluated",
            "severity": "info",
            "project_id": "alpha",
            "page_id": "alpha--decision",
            "path": "projects/alpha/wiki/x.md",
            "support": "not_evaluated",
            "reason": "legacy_untyped",
        }
    ]
    assert "supported" not in codes(findings)


def test_hypothesis_and_question_may_omit_evidence():
    content = document(
        [
            claim(id="h1", kind="hypothesis", support="not_evaluated", text="Talvez."),
            claim(id="q1", kind="question", support="not_evaluated", text="Quando?"),
        ]
    )
    findings = validate_knowledge(content, "alpha", {})
    assert codes(findings) == ["not_evaluated", "not_evaluated"]
    assert {item["reason"] for item in findings} == {"evidence_absent"}
    assert {item["dates"] for item in findings} == {"unknown"}


def test_fact_without_evidence_is_refused():
    content = document([claim(evidence=[])])
    findings = validate_knowledge(content, "alpha", {})
    assert findings[0]["code"] == "fact_without_evidence"
    assert findings[0]["severity"] == "error"


def test_tampered_hash_quote_utf8_and_out_of_scope():
    text = "xxáyy"
    path = f"projects/alpha/raw/{digest(text)}.md"
    files = {path: text}
    valid = span(path, text, quote="xxá")
    content = document([claim(evidence=[valid])])
    assert codes(validate_knowledge(content, "alpha", files | {"p": content})) == [
        "source_linked"
    ]

    bad_hash = dict(valid, sha256="b" * 64)
    hashed = document([claim(evidence=[bad_hash])])
    assert codes(validate_knowledge(hashed, "alpha", files)) == ["hash_mismatch"]

    bad_quote = dict(valid, quote="xxb")
    quoted = document([claim(evidence=[bad_quote])])
    assert codes(validate_knowledge(quoted, "alpha", files)) == ["quote_mismatch"]

    split = dict(valid, start=2, end=3, quote="?")
    split_page = document([claim(evidence=[split])])
    assert codes(validate_knowledge(split_page, "alpha", files)) == ["utf8_split"]

    foreign = f"projects/beta/raw/{digest(text)}.md"
    outside = document([claim(evidence=[span(foreign, text)])])
    assert codes(validate_knowledge(outside, "alpha", {foreign: text})) == [
        "out_of_scope"
    ]


def test_circular_and_self_citation_are_refused():
    raw_text = "leitura primária"
    raw_path = f"projects/alpha/raw/{digest(raw_text)}.md"
    derived_path = "projects/alpha/wiki/concepts/alpha--derived.md"
    derived = document(
        [claim(id="inner", evidence=[span(raw_path, raw_text)])],
        body="síntese",
        name="derived",
    )
    outer_path = "projects/alpha/wiki/concepts/alpha--outer.md"
    outer = document(
        [claim(id="outer", evidence=[span(derived_path, derived, quote="síntese")])],
        body="usa a síntese",
        name="outer",
    )
    files = {raw_path: raw_text, derived_path: derived, outer_path: outer}
    assert codes(validate_knowledge(outer, "alpha", files)) == ["source_linked"]

    self_path = "projects/alpha/wiki/concepts/alpha--self.md"
    mirrored = document(
        [
            claim(
                evidence=[
                    {
                        "path": self_path,
                        "sha256": "ab" * 32,
                        "start": 0,
                        "end": 1,
                        "quote": "x",
                    }
                ]
            )
        ],
        name="self",
    )
    refused = validate_knowledge(mirrored, "alpha", {self_path: mirrored})
    assert refused[0]["code"] == "circular_citation"
    assert refused[0]["detail"] == "self"

    loop_a = "projects/alpha/wiki/concepts/alpha--a.md"
    loop_b = "projects/alpha/wiki/concepts/alpha--b.md"
    page_b = document(
        [
            claim(
                id="from-b",
                evidence=[
                    {
                        "path": loop_a,
                        "sha256": "cd" * 32,
                        "start": 0,
                        "end": 1,
                        "quote": "y",
                    }
                ],
            )
        ],
        name="b",
    )
    page_a = document(
        [claim(id="from-a", evidence=[span(loop_b, page_b, quote="Nota sintética.")])],
        name="a",
    )
    loop = validate_knowledge(page_a, "alpha", {loop_a: page_a, loop_b: page_b})
    assert loop[0]["code"] == "circular_citation"
    assert loop[0]["detail"] == "cycle"


def test_matching_quote_can_stay_insufficient():
    text = "A válvula fechou às 10:00."
    path = f"projects/alpha/raw/{digest(text)}.md"
    assessed = claim(
        support="insufficient",
        evidence=[span(path, text)],
        text="A válvula foi substituída.",
        assessment={
            "verdict": "insufficient",
            "reviewer": "revisora-sintetica",
            "reason": "O trecho confirma o fechamento, não a substituição.",
            "assessed_at": "2026-11-01T12:00:00+00:00",
        },
    )
    content = document([assessed])
    findings = validate_knowledge(content, "alpha", {path: text, "page": content})
    assert findings[0]["code"] == "insufficient"
    assert findings[0]["severity"] == "info"
    assert findings[0]["mechanical_quote"] is True
    assert findings[0]["support"] == "insufficient"

    conflicted = claim(
        support="insufficient",
        evidence=[span(path, text)],
        assessment={
            "verdict": "supported",
            "reviewer": "revisora-sintetica",
            "reason": "Tentativa de promover o trecho.",
            "assessed_at": "2026-11-01",
        },
    )
    refused = validate_knowledge(document([conflicted]), "alpha", {path: text})
    assert refused[0]["code"] == "assessment_conflict"
    assert "supported" not in codes(refused)


def test_supported_requires_assessment_and_dates_are_not_linted():
    text = "A válvula fechou às 10:00."
    path = f"projects/alpha/raw/{digest(text)}.md"
    files = {path: text}
    bare = document([claim(support="supported", evidence=[span(path, text)])])
    assert codes(validate_knowledge(bare, "alpha", files)) == [
        "semantic_assessment_required"
    ]
    dated = claim(
        support="source_linked",
        evidence=[span(path, text)],
        valid_from="2099-01-01",
    )
    dated_findings = validate_knowledge(document([dated]), "alpha", files)
    assert dated_findings[0]["code"] == "source_linked"
    assert "dates" not in dated_findings[0]
    assert not any(item["code"].startswith("temporal") for item in dated_findings)

    accepted = claim(
        support="supported",
        evidence=[span(path, text)],
        assessment={
            "verdict": "supported",
            "reviewer": "revisora-sintetica",
            "reason": "O trecho afirma o mesmo fechamento.",
            "assessed_at": "2026-11-01T12:00:00Z",
        },
    )
    supported = validate_knowledge(document([accepted]), "alpha", files)
    assert supported[0]["code"] == "supported"
    assert supported[0]["severity"] == "info"


def test_claim_project_identity_is_local():
    text = "fato"
    path = f"projects/alpha/raw/{digest(text)}.md"
    foreign = document([claim(project_id="beta", evidence=[span(path, text)])])
    assert codes(validate_knowledge(foreign, "alpha", {path: text})) == [
        "project_identity_mismatch"
    ]


def test_publisher_audits_legacy_and_refuses_unsupported_facts(tmp_path):
    pub = publisher(tmp_path)
    legacy = pub.publish(proposal())
    assert legacy["knowledge_findings"][0]["reason"] == "legacy_untyped"
    assert pub.publish(proposal()) == legacy

    target = proposal()["changes"][0]["path"]
    with pytest.raises(MemoryError, match="fact_without_evidence"):
        pub.publish(
            proposal(
                document([claim()], name="unsupported"),
                operation="unsupported",
                name="unsupported",
            )
        )
    assert not (pub.vault / target.replace("decision", "unsupported")).exists()

    text = "A válvula fechou às 10:00."
    raw = pub.snapshot("alpha", text)
    insufficient = document(
        [
            claim(
                support="insufficient",
                evidence=[span(raw, text)],
                text="A válvula foi substituída.",
                assessment={
                    "verdict": "insufficient",
                    "reviewer": "revisora-sintetica",
                    "reason": "Citação literal não sustenta a substituição.",
                    "assessed_at": "2026-11-01T00:00:00+00:00",
                },
            )
        ],
        name="insuficiente",
        sources=[f"[[{raw.removesuffix('.md')}]]"],
    )
    receipt = pub.publish(
        proposal(insufficient, operation="insuficiente", name="insuficiente")
    )
    stored = (
        pub.vault / proposal(name="insuficiente")["changes"][0]["path"]
    ).read_text()
    assert parse_page(stored)[0]["claims"][0]["support"] == "insufficient"
    assert receipt["knowledge_findings"][0]["mechanical_quote"] is True
    with pytest.raises(MemoryError, match="hash_mismatch"):
        tampered = claim(evidence=[span(raw, text, sha="c" * 64)])
        pub.publish(
            proposal(
                document([tampered], name="tampered"),
                operation="tampered",
                name="tampered",
            )
        )


def test_review_preserves_claim_quotes_that_look_like_links(tmp_path):
    pub = publisher(tmp_path)
    source = "[[keep-literal]] não é autorização."
    raw = pub.snapshot("alpha", source)
    content = document(
        [claim(evidence=[span(raw, source, quote="[[keep-literal]]")])],
        sources=[f"[[{raw.removesuffix('.md')}]]"],
        body="Decisão registrada.",
    )
    pub.publish(proposal(content))
    manager = ReviewManager(pub)
    opened = manager.open("alpha", "claims")
    review_file = next((pub.vault / opened["wiki_folder"]).glob("concepts/*.md"))
    review_file.write_text(
        review_file.read_text().replace("Decisão registrada.", "Revisão humana.")
    )
    result = manager.close("claims")
    assert result["state"] == "imported"
    canonical = (pub.vault / proposal()["changes"][0]["path"]).read_text()
    assert "Revisão humana." in canonical
    assert parse_page(canonical)[0]["claims"][0]["evidence"][0]["quote"] == (
        "[[keep-literal]]"
    )
    assert result["knowledge_findings"][0]["code"] == "source_linked"
    assert "reviews/" not in canonical
