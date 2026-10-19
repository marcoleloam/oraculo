import json

import pytest
from test_memory_schema import page, proposal, publisher

from oraculo.memory import ConflictError
from oraculo.memory.schema import digest


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
