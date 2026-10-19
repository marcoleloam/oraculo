from pathlib import Path

import pytest

from oraculo.memory import ConflictError, MemoryError, Publisher
from oraculo.memory.schema import digest, render_page


def page(
    project="alpha",
    name="decision",
    body="Decisão registrada.",
    reviewed=False,
    sources=None,
):
    return render_page(
        {
            "type": "concept",
            "created": "2026-10-18",
            "updated": "2026-10-18",
            "sources": sources or [],
            "tags": [],
            "aliases": ["Decisão"],
            "reviewed": reviewed,
            "page_id": f"{project}--{name}",
            "project_id": project,
            "schema_version": 1,
        },
        body,
    )


def proposal(
    content=None, project="alpha", name="decision", operation="first", base=None
):
    return {
        "schema_version": 1,
        "batch_id": operation,
        "operation_id": operation,
        "project_id": project,
        "base_commit": None,
        "source_hashes": {},
        "changes": [
            {
                "page_id": f"{project}--{name}",
                "path": f"projects/{project}/wiki/concepts/{project}--{name}.md"
                if project != "shared"
                else f"shared/wiki/concepts/shared--{name}.md",
                "base_sha256": base,
                "content": content or page(project, name),
            }
        ],
        "citations": [],
        "findings": [],
    }


def publisher(tmp_path: Path):
    pub = Publisher(tmp_path / "vault", tmp_path / "state")
    pub.initialize()
    pub.initialize_project("alpha")
    return pub


def test_raw_and_reviewed_protection(tmp_path):
    pub = publisher(tmp_path)
    raw = pub.snapshot("alpha", "evidência original", {"thread_id": "t1"})
    assert pub.snapshot("alpha", "evidência original") == raw
    second = pub.snapshot("alpha", "evidência modificada")
    assert raw != second
    assert (pub.vault / raw).read_text() == "evidência original"
    pub.publish(proposal(page(reviewed=True)))
    target = pub.vault / proposal()["changes"][0]["path"]
    with pytest.raises(ConflictError, match="reviewed_page_protected"):
        pub.publish(
            proposal(
                page(body="sobrescrita"),
                operation="overwrite",
                base=digest(target.read_bytes()),
            )
        )
    (pub.vault / raw).write_text("corrupção externa")
    with pytest.raises(ConflictError, match="external_change"):
        pub.snapshot("alpha", "mais evidência")


def test_reject_paths_links_and_symlinks(tmp_path):
    pub = publisher(tmp_path)
    bad = proposal()
    bad["changes"][0]["path"] = "../outside.md"
    with pytest.raises(MemoryError):
        pub.publish(bad)
    with pytest.raises(MemoryError, match="unresolved"):
        pub.publish(
            proposal(page(body="[[projects/beta/wiki/private]]"), operation="cross")
        )
    outside = tmp_path / "outside"
    outside.mkdir()
    (pub.vault / "projects").mkdir(exist_ok=True)
    (pub.vault / "projects" / "gamma").symlink_to(outside, target_is_directory=True)
    with pytest.raises(MemoryError, match="symlink"):
        pub.publish(proposal(project="gamma", operation="link"))
