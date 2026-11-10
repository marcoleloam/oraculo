"""Navigation regressions found by the independent F6 review."""

import pytest
from test_memory_compiler import publisher, snapshot

from oraculo.memory.compiler import DEFAULT_NIGHTLY_CONCEPT, compile_knowledge
from oraculo.memory.schema import MemoryError


@pytest.mark.parametrize(
    "reference",
    [
        "[[reviews/session/page.md]]",
        "[review](reviews/session/page.md)",
        "![review](reviews/session/page.md)",
        "![review](REVIEWS/session/page.md)",
        "![review](reviews\\session\\page.md)",
        "![review](./reviews/session/page.md)",
        "[review][ref]\n[ref]: reviews/session/page.md",
        '<a href="reviews/session/page.md">review</a>',
        '<img src="reviews/session/page.png">',
        "![review](%72eviews%2Fsession/page.md)",
    ],
)
def test_review_navigation_is_rejected_including_embeds(tmp_path, reference):
    pub = publisher(tmp_path)
    raw = snapshot(pub, "alpha", {"summary": "Verificação independente."})
    proposal = compile_knowledge(pub, "alpha", [raw], concept=DEFAULT_NIGHTLY_CONCEPT)[
        "proposal"
    ]
    proposal["changes"][0]["content"] += "\n" + reference + "\n"
    before = pub.git.head()
    with pytest.raises(MemoryError):
        pub.publish(proposal)
    assert pub.git.head() == before


@pytest.mark.parametrize("field", ["sources", "source_note", "citations"])
@pytest.mark.parametrize(
    "target",
    [
        "reviews/session/page.md",
        "REVIEWS/session/page.md",
        "reviews\\session\\page.md",
        "./reviews/session/page.md",
    ],
)
def test_review_metadata_and_citations_are_rejected(tmp_path, field, target):
    from oraculo.memory.schema import parse_page, render_page

    pub = publisher(tmp_path)
    raw = snapshot(pub, "alpha", {"summary": "Metadados de navegação."})
    proposal = compile_knowledge(pub, "alpha", [raw], concept=DEFAULT_NIGHTLY_CONCEPT)[
        "proposal"
    ]
    if field == "citations":
        proposal["citations"].append(f"[[{target}]]")
    else:
        change = proposal["changes"][0]
        meta, body = parse_page(change["content"])
        meta[field] = [f"[[{target}]]"] if field == "sources" else target
        change["content"] = render_page(meta, body)
    with pytest.raises(MemoryError):
        pub.publish(proposal)


@pytest.mark.parametrize(
    "text",
    [
        "Menção textual a reviews/session/page.md.",
        "Texto capturado: [[reviews/session/page.md]]",
        "Texto capturado: [review](reviews/session/page.md)",
        'Texto capturado: <img src="reviews/session/page.md">',
    ],
)
def test_captured_claim_text_remains_data(tmp_path, text):
    pub = publisher(tmp_path)
    raw = snapshot(pub, "alpha", {"summary": text})
    result = compile_knowledge(
        pub, "alpha", [raw], concept=DEFAULT_NIGHTLY_CONCEPT, apply=True
    )
    assert result["state"] == "confirmed"
