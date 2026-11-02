import json

import pytest
from test_memory_schema import page, proposal, publisher

from oraculo.memory.context import Context
from oraculo.memory.cycles import MemoryCycles
from oraculo.memory.retrieval import (
    TOKEN_ESTIMATOR,
    cache_stats,
    clear_index_cache,
    derive_query,
    estimate_tokens,
    retrieve,
)
from oraculo.memory.schema import MemoryError, digest
from oraculo.state import State


def _write_page(root, relative, body, *, title="Nota", aliases=None, tags=None):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    alias_lines = "\n".join(f"  - {alias}" for alias in aliases or [])
    tag_lines = "\n".join(f"  - {tag}" for tag in tags or [])
    path.write_text(
        "---\n"
        "type: concept\n"
        "page_id: sample\n"
        "project_id: atlas-harbor\n"
        "schema_version: 1\n"
        "reviewed: false\n"
        "created: 2026-01-15\n"
        "updated: 2026-01-15\n"
        "sources: []\n"
        f"tags:\n{tag_lines}\n"
        f"aliases:\n{alias_lines}\n"
        "---\n"
        f"# {title}\n\n{body}\n",
        encoding="utf-8",
    )


def test_estimator_is_declared_and_rounds_up():
    assert TOKEN_ESTIMATOR == "utf8_bytes_div_4_v1"
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("abcde") == 2


def test_normalized_retrieval_reserves_one_hop_inside_scope_and_budget(tmp_path):
    vault = tmp_path / "vault"
    root = "projects/atlas-harbor/wiki/concepts/"
    _write_page(
        vault,
        root + "parent.md",
        "Caisquarenta [[" + root + "leaf.md]]",
        title="Caisquarenta",
    )
    _write_page(
        vault, root + "leaf.md", "Evidência complementar sem os termos da consulta."
    )
    _write_page(
        vault,
        "projects/atlas-ridge/wiki/concepts/private.md",
        "Caisquarenta SECRET_OTHER_PROJECT",
    )
    result = retrieve(
        Context(vault), "atlas-harbor", "Caisquarenta", limit=2, detail_tokens=1000
    )
    assert [document["path"] for document in result["documents"]] == [
        root + "parent.md",
        root + "leaf.md",
    ]
    assert result["selection"]["tokens"]["detail"] <= 1000
    assert "SECRET_OTHER_PROJECT" not in json.dumps(result)


def test_derive_query_keeps_goal_and_title_only():
    payload = {
        "marker": "ORACULO_MANAGED_TASK_V1",
        "title": "Recuperar o código do cais",
        "request": "Qual página descreve o cais norte?",
        "rules": ["ORACULO_MANAGED_TASK_V1 never matches a page"],
        "task_id": "task-secret",
        "memory": {"path": "/tmp/private.json"},
    }
    derived = derive_query(json.dumps(payload, ensure_ascii=False))
    assert "cais norte" in derived
    assert "código do cais" in derived
    assert "ORACULO_MANAGED_TASK_V1" not in derived
    assert "task-secret" not in derived
    assert derive_query("use learned result") == "use learned result"


def test_scope_precedes_ranking_cache_snippets_and_links(tmp_path):
    clear_index_cache()
    harbor = tmp_path / "vault"
    _write_page(
        harbor,
        "projects/atlas-harbor/wiki/concepts/z-fact.md",
        "O código vigente do cais norte é HQ-17-CARMINE.\n"
        "Veja [[projects/atlas-ridge/wiki/concepts/z-secret.md]].",
        title="Código do cais norte",
        aliases=["código vigente do cais norte"],
        tags=["cais"],
    )
    _write_page(
        harbor,
        "projects/atlas-ridge/wiki/concepts/z-secret.md",
        "O código vigente do cais norte é RQ-44-INDIGO.",
        title="Código do cais norte",
    )
    (harbor / "projects/atlas-harbor/.oraculo-project.json").write_text(
        '{"project_id":"atlas-harbor"}'
    )
    (harbor / "projects/atlas-ridge/.oraculo-project.json").write_text(
        '{"project_id":"atlas-ridge"}'
    )
    result = retrieve(Context(harbor), "atlas-harbor", "código do cais norte")
    visible = json.dumps(
        {
            "documents": result["documents"],
            "snippets": result["selection"]["snippets"],
            "ranked_paths": result["selection"]["ranked_paths"],
            "brief": result["brief"],
        },
        ensure_ascii=False,
    )
    assert "HQ-17-CARMINE" in visible
    assert "RQ-44-INDIGO" not in visible
    assert "projects/atlas-ridge" not in visible
    assert result["selection"]["snippets"]
    assert all(
        snippet["path"].startswith("projects/atlas-harbor/")
        or snippet["path"].startswith("shared/")
        for snippet in result["selection"]["snippets"]
    )
    assert result["selection"]["index"]["fingerprint"]
    again = retrieve(Context(harbor), "atlas-harbor", "código do cais norte")
    assert (
        again["selection"]["index"]["fingerprint"]
        == result["selection"]["index"]["fingerprint"]
    )
    assert cache_stats()["hits"] >= 1
    target = harbor / "projects/atlas-harbor/wiki/concepts/z-fact.md"
    target.write_text(target.read_text().replace("CARMINE", "CARMINE-2"))
    changed = retrieve(Context(harbor), "atlas-harbor", "código do cais norte")
    assert (
        changed["selection"]["index"]["fingerprint"]
        != result["selection"]["index"]["fingerprint"]
    )
    assert "CARMINE-2" in json.dumps(changed)


def test_cache_is_bounded_by_content_hash(tmp_path):
    clear_index_cache()
    for index in range(6):
        root = tmp_path / f"vault-{index}"
        _write_page(
            root,
            "projects/atlas-harbor/wiki/concepts/z-fact.md",
            f"código do cais norte versão {index}",
            title="Código do cais norte",
        )
        (root / "projects/atlas-harbor/.oraculo-project.json").write_text(
            '{"project_id":"atlas-harbor"}'
        )
        retrieve(Context(root), "atlas-harbor", "código do cais norte")
    assert cache_stats()["size"] <= 4
    assert cache_stats()["evictions"] >= 2


def test_oversize_page_is_a_reference_not_a_silent_cut(tmp_path):
    root = tmp_path / "vault"
    huge = "código do cais norte " + ("palavra " * 20000)
    _write_page(
        root,
        "projects/atlas-harbor/wiki/concepts/z-huge.md",
        huge,
        title="Código do cais norte",
    )
    raw = (root / "projects/atlas-harbor/wiki/concepts/z-huge.md").read_text()
    result = retrieve(
        Context(root),
        "atlas-harbor",
        "código do cais norte",
        detail_tokens=50,
        brief_tokens=80,
    )
    assert result["documents"] == []
    assert result["selection"]["oversize"]
    reference = result["selection"]["oversize"][0]
    assert reference["coverage"] == 0
    assert reference["sha256"] == digest(raw)
    assert reference["bytes"] == len(raw.encode("utf-8"))
    assert all(raw not in document["content"] for document in result["documents"])
    assert result["selection"]["tokens"]["detail"] <= 50
    assert result["selection"]["tokens"]["brief"] <= 80
    assert result["selection"]["estimator"] == TOKEN_ESTIMATOR


def test_abstain_on_unsupported_query_and_on_empty_terms(tmp_path):
    root = tmp_path / "vault"
    _write_page(
        root,
        "projects/atlas-harbor/wiki/concepts/z-fact.md",
        "O código vigente do cais norte é HQ-17-CARMINE.",
        title="Código do cais norte",
    )
    missing = retrieve(Context(root), "atlas-harbor", "xilofone9")
    assert missing["selection"]["abstained"] is True
    assert missing["selection"]["abstain_reason"] == "unsupported"
    assert missing["documents"] == []
    assert missing["selection"]["ranked_paths"] == []
    empty = retrieve(Context(root), "atlas-harbor", "o de da")
    assert empty["selection"]["abstain_reason"] == "no_useful_terms"
    assert empty["documents"] == []


def test_index_fallback_is_scoped_and_explicit(tmp_path):
    root = tmp_path / "vault"
    _write_page(
        root,
        "projects/atlas-harbor/wiki/concepts/z-fact.md",
        "O código vigente do cais norte é HQ-17-CARMINE.",
        title="Código do cais norte",
    )
    _write_page(
        root,
        "projects/atlas-ridge/wiki/concepts/z-secret.md",
        "RQ-44-INDIGO não entra.",
        title="Outro",
    )
    result = retrieve(
        Context(root), "atlas-harbor", "código do cais norte", force_fallback=True
    )
    assert result["selection"]["ranker"] == "scoped_lexical_fallback"
    assert "index_unavailable" in result["selection"]["limitation"]
    blob = json.dumps(result, ensure_ascii=False)
    assert "RQ-44-INDIGO" not in blob
    assert "HQ-17-CARMINE" in blob


def test_legacy_bundle_stays_the_baseline_entry_point(tmp_path):
    root = tmp_path / "vault"
    _write_page(
        root,
        "projects/atlas-harbor/wiki/concepts/a-first.md",
        "código do cais norte repetido sem título útil",
        title="Nota operacional",
    )
    _write_page(
        root,
        "projects/atlas-harbor/wiki/concepts/z-fact.md",
        "código do cais norte repetido sem título útil",
        title="Código do cais norte",
        aliases=["código do cais norte"],
    )
    context = Context(root)
    legacy = context.bundle("atlas-harbor", "código do cais norte")
    explicit = context.legacy_bundle("atlas-harbor", "código do cais norte")
    assert legacy == explicit
    assert "selection" not in legacy
    budgeted = context.bundle("atlas-harbor", "código do cais norte", mode="budgeted")
    assert budgeted["selection"]["ranker"] == "normalized"
    assert budgeted["selection"]["ranked_paths"][0].endswith("a-first.md")
    weighted = context.bundle(
        "atlas-harbor",
        "código do cais norte",
        mode="budgeted",
        ranker="field_weights",
    )
    assert weighted["selection"]["ranked_paths"][0].endswith("z-fact.md")
    assert legacy["documents"][0]["path"].endswith("a-first.md")


def test_cycle_consumer_keeps_complete_bytes_for_a_small_corpus(tmp_path):
    state = State(tmp_path / "cycles.sqlite")
    cycles = MemoryCycles(state, publisher(tmp_path))
    cycles.publisher.publish(proposal(page(body="alpha private use learned result")))
    cycles.publisher.publish(
        proposal(
            page(project="shared", body="allowed shared"),
            project="shared",
            operation="shared-op",
        )
    )
    item = cycles.begin("alpha", "run-1", "use learned result", {"objective_id": "obj"})
    bundle = item["context"]["bundle"]
    assert bundle["selection"]["estimator"] == TOKEN_ESTIMATOR
    blob = json.dumps(bundle)
    assert "alpha private use learned result" in blob
    assert "allowed shared" in blob
    for document in bundle["documents"]:
        file_text = (cycles.publisher.vault / document["path"]).read_text()
        assert document["content"] == file_text or document["sha256"] == digest(
            document["content"]
        )
        assert document["sha256"] == digest(document["content"])
    state.close()


def test_cycle_does_not_drop_unmatched_pages_when_they_fit(tmp_path):
    state = State(tmp_path / "cycles.sqlite")
    pub = publisher(tmp_path)
    cycles = MemoryCycles(state, pub)
    pub.publish(proposal(page(body="alpha private")))
    pub.publish(
        proposal(
            page(project="shared", body="allowed shared"),
            project="shared",
            operation="shared-op",
        )
    )
    item = cycles.begin("alpha", "run-9", "zzzz-unmatched", {"objective_id": "obj"})
    blob = json.dumps(item["context"])
    assert "alpha private" in blob and "allowed shared" in blob
    assert item["read_receipt"]["sources"]
    state.close()


def test_invalid_budget_is_rejected(tmp_path):
    with pytest.raises(MemoryError, match="budget"):
        retrieve(Context(tmp_path), "atlas-harbor", "cais", detail_tokens=-1)
