import json
import shutil

import pytest
from test_memory_schema import page, proposal, publisher

from oraculo.memory import ConflictError
from oraculo.memory.cycles import MemoryCycles
from oraculo.memory.schema import digest, project_key
from oraculo.state import State


def test_f1_root_scaffold_edit_and_recreated_vault_stay_usable(tmp_path):
    """F1: root scaffold edits must not freeze memory, and initialize re-scaffolds.

    A confirmed initialize journal used to return without rewriting missing
    root files. Those files were also catalogued, so an Obsidian edit of
    index.md made every read return external_change.
    """
    pub = publisher(tmp_path)
    catalog = json.loads(pub.catalog.read_bytes())
    for name in (".gitignore", "AGENTS.md", "CLAUDE.md", "index.md"):
        assert name not in catalog
        assert (pub.vault / name).is_file()
    index = pub.vault / "index.md"
    index.write_text(index.read_text() + "\nNota humana.\n")
    state = State(tmp_path / "cycles.sqlite")
    cycles = MemoryCycles(state, pub)
    try:
        item = cycles.begin(
            "alpha", "run-1", "use learned result", {"objective_id": "obj"}
        )
    finally:
        state.close()
    assert item["read_state"] != "unavailable"
    assert "external_change" not in (item["read_receipt"].get("error") or "")
    pub.publish(proposal())
    canonical = pub.vault / proposal()["changes"][0]["path"]
    canonical.write_text(canonical.read_text() + "\nEdição humana aceite.\n")
    with pytest.raises(ConflictError, match="external_change"):
        with pub.locked():
            pub._check_integrity()
    receipt = pub.rebaseline()
    assert receipt["state"] == "confirmed"
    assert receipt["operation_id"].startswith("rebaseline-")
    assert "index.md" not in receipt["paths"]
    with pub.locked():
        pub._check_integrity()
    assert "Edição humana aceite." in canonical.read_text()
    assert pub.rebaseline()["operation_id"] == receipt["operation_id"]
    shutil.rmtree(pub.vault)
    pub.initialize()
    for name in (".gitignore", "AGENTS.md", "CLAUDE.md", "index.md"):
        assert (pub.vault / name).is_file()


def test_f7_missing_provenance_is_written_on_snapshot_retry(tmp_path):
    """F7: a crash after the raw commit must not drop provenance forever."""
    pub = publisher(tmp_path)
    raw = pub.snapshot("alpha", "evidência original", {"thread_id": "t1"})
    raw_hash = raw.rsplit("/", 1)[-1].removesuffix(".md")
    path = pub.state_dir / "provenance" / f"{project_key('alpha')}-{raw_hash}.json"
    assert json.loads(path.read_bytes())["thread_id"] == "t1"
    path.unlink()
    assert pub.snapshot("alpha", "evidência original") == raw
    assert path.is_file()
    assert json.loads(path.read_bytes())["project_id"] == "alpha"


def test_ingest_index_and_append_only_log(tmp_path):
    pub = publisher(tmp_path)
    raw = pub.snapshot("alpha", "evidência", {"task_id": "task-1"})
    first = proposal(page(sources=[f"[[{raw}]]"]))
    receipt = pub.publish(first)
    log = pub.vault / "projects/alpha/wiki/log.md"
    before = log.read_text()
    assert "## [2026-" in before and "batch_id: first" in before
    assert "alpha--decision" in (pub.vault / "projects/alpha/wiki/index.md").read_text()
    assert pub.publish(first) == receipt
    assert log.read_text() == before
    path = pub.vault / first["changes"][0]["path"]
    pub.publish(
        proposal(
            page(body="aprendizado novo"),
            operation="second",
            base=digest(path.read_bytes()),
        )
    )
    assert log.read_text().startswith(before)
    assert log.read_text().count("batch_id: first") == 1
    assert pub.git.head() != receipt["commit"]


def test_recover_interrupted_batch_without_duplicate_commit(tmp_path, monkeypatch):
    pub = publisher(tmp_path)
    import oraculo.memory.publish as module

    real = module.atomic_write
    count = 0

    def crash(path, data):
        nonlocal count
        if path.is_relative_to(pub.vault):
            count += 1
            if count == 2:
                raise OSError("simulated crash")
        real(path, data)

    monkeypatch.setattr(module, "atomic_write", crash)
    with pytest.raises(OSError):
        pub.publish(proposal())
    monkeypatch.setattr(module, "atomic_write", real)
    assert pub.recover()[0]["state"] == "confirmed"
    head = pub.git.head()
    pub.publish(proposal())
    assert pub.git.head() == head
    assert json.loads((pub.journals / "first.json").read_text())["state"] == "confirmed"


def test_recovery_preserves_external_change(tmp_path, monkeypatch):
    pub = publisher(tmp_path)
    original = pub.git.commit
    monkeypatch.setattr(
        pub.git, "commit", lambda *args: (_ for _ in ()).throw(OSError("crash"))
    )
    with pytest.raises(OSError):
        pub.publish(proposal())
    path = pub.vault / proposal()["changes"][0]["path"]
    path.write_text("external editor change")
    monkeypatch.setattr(pub.git, "commit", original)
    with pytest.raises(ConflictError):
        pub.recover()
    assert path.read_text() == "external editor change"
