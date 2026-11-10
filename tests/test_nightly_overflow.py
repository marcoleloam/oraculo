"""Regression: informational fields and a full target must not stall nights."""

import json
from types import SimpleNamespace

import pytest
from test_memory_compiler import publisher, snapshot

from oraculo.memory.compiler import compile_knowledge
from oraculo.memory.maintenance import (
    DEFAULT_NIGHTLY_CONCEPT,
    nightly_compilation_receipt,
)
from oraculo.memory.routines import KnowledgeRoutines
from oraculo.memory.schema import parse_page


def test_full_target_rolls_over_and_two_nights_progress(tmp_path, monkeypatch):
    import oraculo.memory.compiler as compiler

    pub = publisher(tmp_path)
    old = snapshot(pub, "alpha", {"summary": "histórico " * 300})
    saved = compile_knowledge(
        pub, "alpha", [old], concept=DEFAULT_NIGHTLY_CONCEPT, apply=True
    )
    old_bytes = (pub.vault / saved["path"]).read_bytes()
    monkeypatch.setattr(compiler, "MAX_PROPOSAL_BYTES", len(old_bytes))
    pending = [
        snapshot(
            pub,
            "alpha",
            {"summary": f"Nova decisão {i}", "checks": ["pass"], "results": ["ok"]},
        )
        for i in range(20)
    ]
    preview = compile_knowledge(pub, "alpha", pending, concept=DEFAULT_NIGHTLY_CONCEPT)
    assert preview["state"] == "proposed"
    assert preview["path"] != saved["path"]
    assert any(f["code"] == "proposal_too_large" for f in preview["findings"])
    assert any(f["code"] == "not_promoted" for f in preview["findings"])
    assert preview["proposal"]["base_commit"] is None
    assert len(json.dumps(preview["proposal"]).encode()) < compiler.MAX_PROPOSAL_BYTES
    receipt = nightly_compilation_receipt(
        pub, "alpha", {"knowledge_sources": pending}, {"base_commit": pub.git.head()}
    )
    assert receipt["status"] == "ready"
    assert receipt["proposal_base_commit"] is None
    assert receipt["coverage"]["corpus_acceptance"] is False
    assert receipt["compiled_sources"]
    assert receipt["deferred_sources"]
    assert (
        compile_knowledge(pub, "alpha", pending, concept=DEFAULT_NIGHTLY_CONCEPT)
        == preview
    )
    pub.publish(preview["proposal"])
    routines = KnowledgeRoutines(SimpleNamespace(publisher=pub, state=None))
    next_sources, _ = routines.pending_sources("alpha", 20)
    assert set(next_sources) == set(pending) - set(preview["compiled_sources"])
    second = compile_knowledge(
        pub, "alpha", next_sources, concept=DEFAULT_NIGHTLY_CONCEPT
    )
    assert second["state"] == "proposed"
    pub.publish(second["proposal"])
    after, _ = routines.pending_sources("alpha", 20)
    assert len(after) < len(next_sources)
    assert (pub.vault / saved["path"]).read_bytes() == old_bytes
    for result in (preview, second):
        meta, _ = parse_page(result["proposal"]["changes"][0]["content"])
        assert all(c["support"] != "supported" for c in meta["claims"])


def test_oversized_source_does_not_starve_eligible_source(tmp_path, monkeypatch):
    import oraculo.memory.compiler as compiler

    pub = publisher(tmp_path)
    huge = snapshot(pub, "alpha", {"summary": "grande " * 10000})
    small = snapshot(pub, "alpha", {"summary": "pequena", "checks": ["pass"]})
    monkeypatch.setattr(compiler, "MAX_PROPOSAL_BYTES", 10000)
    result = compile_knowledge(
        pub, "alpha", [huge, small], concept=DEFAULT_NIGHTLY_CONCEPT
    )
    assert result["state"] == "proposed"
    assert result["compiled_sources"] == [small]
    assert [x["path"] for x in result["deferred_sources"]] == [huge]
    pub.publish(result["proposal"])
    result = compile_knowledge(pub, "alpha", [huge], concept=DEFAULT_NIGHTLY_CONCEPT)
    assert result["state"] == "deferred"
    assert result["deferred_sources"][0]["path"] == huge
    receipt = nightly_compilation_receipt(
        pub, "alpha", {"knowledge_sources": [huge]}, {"base_commit": pub.git.head()}
    )
    assert receipt["status"] == "deferred"
    assert receipt["code"] == "proposal_too_large"
    assert receipt["proposal_sha256"] is None


def test_many_not_promoted_fields_are_not_rejections(tmp_path):
    pub = publisher(tmp_path)
    sources = [
        snapshot(
            pub,
            "alpha",
            {
                "summary": f"Fato {i}",
                "checks": ["ok"],
                "next_steps": ["revisar"],
                "artifacts": ["x"],
            },
        )
        for i in range(20)
    ]
    receipt = nightly_compilation_receipt(
        pub, "alpha", {"knowledge_sources": sources}, {"base_commit": pub.git.head()}
    )
    assert receipt["status"] == "ready"
    assert receipt["compiler_finding_counts"]["not_promoted"] == 60
    assert receipt["new_claim_count"] == 20


def test_too_large_error_is_not_hidden_by_not_promoted(tmp_path, monkeypatch):
    import oraculo.memory.compiler as compiler

    pub = publisher(tmp_path)
    raw = snapshot(pub, "alpha", {"summary": "x" * 10000, "checks": ["ok"]})
    monkeypatch.setattr(compiler, "MAX_PROPOSAL_BYTES", 1000)
    receipt = nightly_compilation_receipt(
        pub,
        "alpha",
        {"knowledge_sources": [raw], "concept": "Explícito"},
        {"base_commit": pub.git.head()},
    )
    assert receipt["status"] == "blocked"
    assert receipt["code"] == "proposal_too_large"


def test_review_literal_in_claim_is_data_but_review_link_is_forbidden(tmp_path):
    pub = publisher(tmp_path)
    raw = snapshot(
        pub, "alpha", {"summary": "Verificação local em reviews/abc; sem promoção."}
    )
    result = compile_knowledge(pub, "alpha", [raw], concept="Revisões", apply=True)
    assert result["state"] == "confirmed"
    meta, _ = parse_page(result["proposal"]["changes"][0]["content"])
    assert "reviews/abc" in meta["claims"][0]["text"]
    assert meta["claims"][0]["evidence"]


def test_individually_oversized_pending_sources_do_not_fill_next_selection(
    tmp_path, monkeypatch
):
    import oraculo.memory.compiler as compiler

    pub = publisher(tmp_path)
    oversized = [
        snapshot(pub, "alpha", {"summary": str(i) + " grande" * 1000})
        for i in range(21)
    ]
    small = snapshot(pub, "alpha", {"summary": "elegível"})
    monkeypatch.setattr(compiler, "MAX_PROPOSAL_BYTES", 10000)
    routines = KnowledgeRoutines(SimpleNamespace(publisher=pub, state=None))
    selected, metrics = routines.pending_sources("alpha", 20)
    assert selected == [small]
    assert {x["path"] for x in metrics["deferred_sources"]} == set(oversized)


def test_review_link_remains_forbidden(tmp_path):
    from oraculo.memory.schema import MemoryError

    pub = publisher(tmp_path)
    raw = snapshot(pub, "alpha", {"summary": "Sem referência de revisão."})
    result = compile_knowledge(pub, "alpha", [raw], concept="Revisão")
    result["proposal"]["changes"][0]["content"] += "\n[[reviews/abc/page.md]]\n"
    with pytest.raises(MemoryError, match="review_reference_in_canonical"):
        pub.publish(result["proposal"])


def test_nightly_publication_cycle_recovers_after_lost_receipt(tmp_path, monkeypatch):
    from oraculo.memory.cycles import MemoryCycles
    from oraculo.state import State

    pub = publisher(tmp_path)
    raw = snapshot(pub, "alpha", {"summary": "publicação rastreável"})
    preview = compile_knowledge(pub, "alpha", [raw], concept=DEFAULT_NIGHTLY_CONCEPT)
    state = State(tmp_path / "cycles.sqlite")
    cycles = MemoryCycles(state, pub)
    identity = {
        "attempt_id": "attempt",
        "job_id": "job",
        "objective_id": "objective",
        "thread_id": "child",
        "run_id": "run",
    }
    original = pub.publish

    def lost(proposal):
        original(proposal)
        raise RuntimeError("lost receipt")

    monkeypatch.setattr(pub, "publish", lost)
    with pytest.raises(RuntimeError, match="lost receipt"):
        cycles.publish_nightly("alpha", identity, preview["proposal"])
    [item] = cycles.list("alpha")
    assert item["write_state"] == "failed"
    assert item["write_receipt"] is None
    head = pub.git.head()
    monkeypatch.setattr(pub, "publish", original)
    recovered = cycles.publish_one(item["id"])
    assert recovered["write_state"] == "saved"
    assert recovered["write_receipt"]["state"] == "confirmed"
    assert recovered["write_receipt"]["commit"] == head
    assert recovered["identity"] == identity
    from oraculo.memory.bridge import public_cycle
    from oraculo.presentation import task_memory_confirmed

    public = public_cycle(recovered)
    assert "publication_proposal" not in public
    assert public["display"]["finish"]["state"] == "salva"
    assert task_memory_confirmed({"memory": public})
    wrong = dict(
        public, write_receipt={**public["write_receipt"], "operation_id": "wrong"}
    )
    assert not task_memory_confirmed({"memory": wrong})

    assert (
        cycles.publish_nightly("alpha", identity, preview["proposal"])["commit"] == head
    )
    assert pub.git.head() == head
    assert len(cycles.list("alpha")) == 1
    state.close()
