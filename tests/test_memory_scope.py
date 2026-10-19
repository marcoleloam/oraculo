from test_memory_schema import page, proposal, publisher

from oraculo.memory import Context


def test_homonyms_do_not_cross_projects(tmp_path):
    pub = publisher(tmp_path)
    pub.publish(proposal(page(body="Cliente ALPHA confidencial.")))
    pub.publish(
        proposal(
            page("beta", body="Cliente BETA segredo."), project="beta", operation="beta"
        )
    )
    pub.publish(
        proposal(
            page("shared", body="Conhecimento geral."),
            project="shared",
            operation="shared",
        )
    )
    bundle = Context(pub.vault).bundle("alpha", "Decisão")
    joined = str(bundle)
    assert "ALPHA" in joined and "Conhecimento geral" in joined
    assert "BETA" not in joined and "projects/beta" not in joined
    assert bundle["documents"][0]["path"].endswith("/index.md")


def test_t3_opaque_project_id_publish_review_and_scope(tmp_path):
    import json

    import pytest

    from oraculo.memory import MemoryError, ReviewManager
    from oraculo.memory.schema import corpus, parse_page, project_key, render_page

    original_id = "mcp:4123ff35-19ff-423f-a3d4-af5e4219a35e"
    other_id = "mcp:4123ff35-19ff-423f-a3d4-af5e4219a35f"
    pub = publisher(tmp_path)
    key = project_key(original_id)
    assert key == project_key(original_id)
    assert key != project_key(other_id)
    assert project_key("alpha") == "alpha"
    raw = pub.snapshot(original_id, "evidência T3", {"thread_id": "thread-1"})
    content = page(key, sources=[f"[[{raw}]]"])
    meta, body = parse_page(content)
    meta["project_id"] = original_id
    item = proposal(render_page(meta, body), project=key, operation="opaque")
    item["project_id"] = original_id
    pub.publish(item)
    bundle = Context(pub.vault).bundle(original_id)
    assert bundle["corpus_path"] == corpus(original_id)
    assert bundle["page_prefix"] == key + "--"
    assert bundle["project_id"] == original_id
    manager = ReviewManager(pub)
    session = manager.open(original_id)
    reviewed = next((pub.vault / session["wiki_folder"]).glob("concepts/*.md"))
    reviewed.write_text(reviewed.read_text() + "\nRevisão T3.")
    assert manager.close(session["session_id"])["state"] == "imported"
    canonical = pub.vault / item["changes"][0]["path"]
    assert parse_page(canonical.read_text())[0]["project_id"] == original_id
    assert "Revisão T3." in canonical.read_text()
    provenance = next((pub.state_dir / "provenance").glob(key + "-*.json"))
    assert json.loads(provenance.read_text())["project_id"] == original_id
    assert not Context(pub.vault).bundle(other_id)["documents"]
    with pytest.raises(MemoryError, match="project_key_collision"):
        Context(pub.vault).bundle(key)
    with pytest.raises(MemoryError, match="project_key_collision"):
        pub.initialize_project(key)


def test_project_keys_reject_traversal_controls_and_oversized_ids():
    import pytest

    from oraculo.memory import MemoryError
    from oraculo.memory.schema import project_key

    for invalid in (
        "",
        "../x",
        "/absolute",
        "mcp:../x",
        "x\\y",
        "x\n",
        "x\x00y",
        "x" * 513,
    ):
        with pytest.raises(MemoryError):
            project_key(invalid)
