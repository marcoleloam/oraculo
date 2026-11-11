"""Boundary and progress checks for the nightly rollover."""

from types import SimpleNamespace

import pytest
from test_memory_compiler import publisher, snapshot

from oraculo.memory import compiler
from oraculo.memory.routines import KnowledgeRoutines
from oraculo.memory.schema import MemoryError, MemoryProposal, digest


def test_shard_admission_accepts_exact_serialized_limit(tmp_path, monkeypatch):
    pub = publisher(tmp_path)
    raw = snapshot(pub, "alpha", {"summary": "limite exato"})
    text = (pub.vault / raw).read_text()
    shard = compiler.nightly_shard_concept([(raw, digest(text))])
    proposal = compiler.compile_knowledge(pub, "alpha", [raw], concept=shard)[
        "proposal"
    ]
    size = len(MemoryProposal.model_validate(proposal).model_dump_json().encode())
    assert compiler.MAX_PROPOSAL_BYTES == 2_097_152
    monkeypatch.setattr(compiler, "MAX_PROPOSAL_BYTES", size)
    assert compiler.nightly_source_fits("alpha", raw, text)
    assert (
        compiler.compile_knowledge(pub, "alpha", [raw], concept=shard)["state"]
        == "proposed"
    )
    monkeypatch.setattr(compiler, "MAX_PROPOSAL_BYTES", size - 1)
    assert not compiler.nightly_source_fits("alpha", raw, text)
    assert (
        compiler.compile_knowledge(pub, "alpha", [raw], concept=shard)["state"]
        == "rejected"
    )


def test_rollover_drains_all_batches_and_retains_old_page(tmp_path, monkeypatch):
    pub = publisher(tmp_path)
    old = snapshot(pub, "alpha", {"summary": "histórico " * 300})
    saved = compiler.compile_knowledge(
        pub, "alpha", [old], concept=compiler.DEFAULT_NIGHTLY_CONCEPT, apply=True
    )
    original = (pub.vault / saved["path"]).read_bytes()
    monkeypatch.setattr(compiler, "MAX_PROPOSAL_BYTES", len(original))
    sources = {
        snapshot(pub, "alpha", {"summary": f"nova decisão {i}"}) for i in range(20)
    }
    routines = KnowledgeRoutines(SimpleNamespace(publisher=pub, state=None))
    published, identities = set(), set()
    for _ in range(20):
        selected, metrics = routines.pending_sources("alpha", 20)
        if not selected:
            break
        before = compiler.compile_knowledge(
            pub, "alpha", selected, concept=compiler.DEFAULT_NIGHTLY_CONCEPT
        )
        reverse = compiler.compile_knowledge(
            pub,
            "alpha",
            list(reversed(selected)),
            concept=compiler.DEFAULT_NIGHTLY_CONCEPT,
        )
        assert before == reverse
        assert before["page_id"] not in identities
        identities.add(before["page_id"])
        batch = set(before["compiled_sources"])
        assert batch and not batch & published
        published |= batch
        pub.publish(before["proposal"])
    assert published == sources
    assert routines.pending_sources("alpha", 20)[0] == []
    assert (pub.vault / saved["path"]).read_bytes() == original


def test_empty_selection_is_explicit_error(tmp_path):
    with pytest.raises(MemoryError, match="source_required"):
        compiler.compile_knowledge(
            publisher(tmp_path), "alpha", [], concept=compiler.DEFAULT_NIGHTLY_CONCEPT
        )


def test_actual_two_mebibyte_proposal_boundary(tmp_path, monkeypatch):
    pub = publisher(tmp_path)
    limit = compiler.MAX_PROPOSAL_BYTES
    monkeypatch.setattr(compiler, "MAX_PROPOSAL_BYTES", limit * 2)

    def preview(length, title="t"):
        raw = snapshot(pub, "alpha", {"summary": "x" * length})
        result = compiler.compile_knowledge(
            pub, "alpha", [raw], concept="Limite explícito", title=title
        )
        size = len(
            MemoryProposal.model_validate(result["proposal"]).model_dump_json().encode()
        )
        return raw, result, size

    _, _, small = preview(1000)
    _, _, next_size = preview(1001)
    slope = next_size - small
    length = 1000 + (limit - small) // slope - 10
    raw, result, size = preview(length)
    # Title occurs twice (alias and heading); vary source length for parity.
    if (limit - size) % 2:
        length -= 1
        raw, result, size = preview(length)
    title = "t" * (1 + (limit - size) // 2)
    raw, result, size = preview(length, title)
    assert size == 2_097_152
    monkeypatch.setattr(compiler, "MAX_PROPOSAL_BYTES", limit)
    exact = compiler.compile_knowledge(
        pub, "alpha", [raw], concept="Limite explícito", title=title
    )
    assert exact["state"] == "proposed"
    over = compiler.compile_knowledge(
        pub, "alpha", [raw], concept="Limite explícito", title=title + "t"
    )
    assert over["state"] == "rejected"
    assert over["findings"][-1]["code"] == "proposal_too_large"
