import json
import re
from pathlib import Path

import pytest

from oraculo.memory.context import Context
from oraculo.memory.evaluation import (
    DEFINITION_BASE_SHA,
    SCALES,
    SEED,
    build_manifest,
    choose_ranker,
    fixed_page_count,
    materialize,
    measure_question,
    question_bank,
    recall_at_10,
    run_benchmark,
)
from oraculo.memory.retrieval import FROZEN_RANKER
from oraculo.memory.schema import MemoryError

FIXTURE = Path(__file__).parent / "fixtures" / "second_brain" / "manifest.json"
FORBIDDEN = (
    "/Users/",
    "AKIA",
    "Bearer ",
    "sk-",
    "@",
)


def test_manifest_is_frozen_and_covers_the_required_questions():
    manifest = build_manifest()
    on_disk = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert on_disk == manifest
    assert manifest["base_sha"] == DEFINITION_BASE_SHA
    assert manifest["seed"] == SEED
    assert manifest["scales"] == list(SCALES)
    questions = manifest["questions"]
    assert len(questions) >= 60
    assert {question["language"] for question in questions} == {"pt", "en"}
    kinds = {
        "direct",
        "paraphrase",
        "multihop",
        "temporal",
        "decision",
        "contradiction",
        "no-answer",
    }
    assert {question["type"] for question in questions} == kinds
    tuning = [question for question in questions if question["split"] == "tuning"]
    acceptance = [
        question for question in questions if question["split"] == "acceptance"
    ]
    assert tuning and acceptance
    assert {question["id"] for question in tuning}.isdisjoint(
        {question["id"] for question in acceptance}
    )
    for split_rows in (tuning, acceptance):
        assert {question["language"] for question in split_rows} == {"pt", "en"}
        assert {question["type"] for question in split_rows} == kinds
    blob = json.dumps(manifest)
    assert not re.search(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}", blob)
    for secret in FORBIDDEN:
        assert secret not in blob


def test_scale_writes_the_requested_page_count_and_keeps_labels(tmp_path):
    assert fixed_page_count() < 50
    vault = tmp_path / "vault"
    created = materialize(vault, 50)
    pages = list(vault.rglob("*.md"))
    assert created["pages"] == 50 == len(pages)
    texts = {path.relative_to(vault).as_posix(): path.read_text() for path in pages}
    joined = "\n".join(texts.values())
    assert not re.search(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}", joined)
    for secret in FORBIDDEN:
        assert secret not in joined
    for question in question_bank():
        if question["type"] == "no-answer":
            scoped = "\n".join(
                text
                for path, text in texts.items()
                if path.startswith(f"projects/{question['project_id']}/")
                or path.startswith("shared/")
            )
            assert question["query"] not in scoped
            continue
        for evidence in question["evidence"]:
            assert evidence["span"] in texts[evidence["path"]]
        other = (
            "atlas-ridge"
            if question["project_id"] == "atlas-harbor"
            else "atlas-harbor"
        )
        other_text = "\n".join(
            text
            for path, text in texts.items()
            if path.startswith(f"projects/{other}/")
        )
        for evidence in question["evidence"]:
            if (
                "HQ-17-CARMINE" in evidence["span"]
                or "RQ-44-INDIGO" in evidence["span"]
            ):
                assert evidence["span"] not in other_text


def test_baseline_calls_real_context_bundle_and_not_an_approximation(
    tmp_path, monkeypatch
):
    vault = tmp_path / "vault"
    materialize(vault, 50)
    question = next(
        item
        for item in question_bank()
        if item["split"] == "tuning" and item["type"] == "direct"
    )
    calls = []
    original = Context.bundle

    def spy(self, project_id, query="", limit=20, **kwargs):
        calls.append(kwargs.get("mode", "legacy"))
        return original(self, project_id, query, limit, **kwargs)

    monkeypatch.setattr(Context, "bundle", spy)
    measured = measure_question(vault, question, ranker=None)
    assert calls == ["legacy"]
    assert "recall_at_10" in measured
    with pytest.raises(MemoryError, match="tuning_split"):
        choose_ranker("acceptance", {"recall_at_10": 0.0}, {})


def test_n_memory_freshness_eval_1_legacy_rank_drops_index_preamble(tmp_path):
    """N-memory-freshness-eval-1: index pages are not ranked baseline hits."""

    vault = tmp_path / "vault"
    materialize(vault, 50)
    question = next(
        item
        for item in question_bank()
        if item["split"] == "tuning" and item["relevant_paths"]
    )
    measured = measure_question(vault, question, ranker=None)
    assert measured["ranking_convention"] == "candidates_without_index_preamble"
    assert any(path.endswith("/wiki/index.md") for path in measured["document_paths"])
    assert measured["ranked_paths"] == [
        path
        for path in measured["document_paths"]
        if not path.endswith("/wiki/index.md")
    ]
    assert measured["recall_at_10"] == recall_at_10(
        measured["ranked_paths"], question["relevant_paths"]
    )


def test_recall_ignores_no_answer_shape():
    with pytest.raises(MemoryError, match="relevant"):
        recall_at_10(["a"], [])


def test_tuning_scale_50_is_measured_with_the_real_baseline(tmp_path):
    report = run_benchmark(tmp_path / "work", scales=(50,))
    choice = report["choice"]
    assert choice["split"] == "tuning"
    # N-memory-freshness-eval-1: the index preamble made legacy recall look
    # like zero, so the +10pp branch selected the frozen ranker. Without those
    # pages the baseline is already above 0.90. Quality holds and MRR matches,
    # but context does not shrink by 30%, so the gate selects nothing.
    assert choice["selected"] is None
    assert choice["constant_matches"] is False
    assert choice["frozen_constant"] == FROZEN_RANKER
    normalized = next(
        item for item in choice["comparisons"] if item["ranker"] == FROZEN_RANKER
    )
    assert normalized["quality_preserved"] is True
    assert normalized["median_context_reduced_30pct"] is False
    assert normalized["passed"] is False
    tuning = report["scales"][0]["tuning"]["legacy_bundle"]
    acceptance_legacy = report["scales"][0]["acceptance"]["legacy_bundle"]
    assert tuning["ranking_convention"] == "candidates_without_index_preamble"
    assert acceptance_legacy["ranking_convention"] == (
        "candidates_without_index_preamble"
    )
    assert tuning["recall_at_10"] > 0.90
    acceptance = report["scales"][0]["acceptance_gate"]
    # The gate is whatever the frozen acceptance split produced. Do not rewrite
    # labels from this assertion; a miss stays a miss.
    assert acceptance["passed"] is False
    assert acceptance["reason"] == (
        "baseline_above_0.90_preserve_quality_and_shrink_context"
    )
    assert acceptance["baseline_recall_at_10"] > 0.90
    budgeted = report["scales"][0]["acceptance"]["budgeted"]
    assert budgeted["mrr"] + 1e-12 >= acceptance_legacy["mrr"]
    assert report["measured_ranker"] == FROZEN_RANKER
    assert report["scales"][0]["acceptance"]["legacy_bundle"]["no_answer"]["count"] > 0
    legacy_false = report["scales"][0]["acceptance"]["legacy_bundle"]["no_answer"][
        "false_hits"
    ]
    budget_false = report["scales"][0]["acceptance"]["budgeted"]["no_answer"][
        "false_hits"
    ]
    assert legacy_false >= budget_false
