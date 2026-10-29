"""Immutable source selection with synthetic repositories and caller-bound host."""

from pathlib import Path

import pytest
import test_managed_tasks as managed
from test_managed_tasks import git, start

from oraculo.managed_tasks import workspace

system = managed.system


def dirty(root):
    (root / "original name.txt").write_text("committed version\n")
    git(root, "add", ".")
    git(
        root,
        "-c",
        "user.name=Demo",
        "-c",
        "user.email=demo@example.org",
        "commit",
        "-m",
        "Add tracked demo file",
    )
    sha = git(root, "rev-parse", "HEAD")
    git(root, "mv", "original name.txt", "renamed file.txt")
    (root / "renamed file.txt").write_text("LOCAL_ONLY_CONTENT\n")
    (root / " leading space.txt").write_text("UNTRACKED_ONLY_CONTENT\n")
    return sha


@pytest.mark.parametrize("base_kind", ["HEAD", "main", "short"])
async def test_dirty_symbolic_and_short_base_stay_blocked_on_resume(system, base_kind):
    root = system.service.config.projects_root / "alpha"
    sha = dirty(root)
    result = await start(system, base=sha[:12] if base_kind == "short" else base_kind)
    assert result["blocked_reason"] == "dirty_source_commit_selection_required"
    assert result["memory"] is None and not system.host.created
    resumed = await system.engine.command("resume", {"task_id": result["id"]})
    assert resumed["blocked_reason"] == result["blocked_reason"]
    assert resumed["id"] == result["id"] and not system.host.created
    assert not system.service.memory_cycles.list()
    assert "SHA completo de 40" in resumed["presentation"]["blocker"]


@pytest.mark.parametrize("uppercase", [False, True])
async def test_dirty_full_sha_launch_records_paths_and_never_copies_local_edits(
    system, uppercase
):
    root = system.service.config.projects_root / "alpha"
    sha = dirty(root)
    result = await start(system, base=sha.upper() if uppercase else sha)
    assert result["phase"] == "executing", result
    assert result["transport_mode"] == "native_child"
    assert result["source_commit"] == result["base_sha"] == sha
    assert result["source_dirty"] is True
    assert result["unselected_paths"] == [
        " leading space.txt",
        "original name.txt",
        "renamed file.txt",
    ]
    assert result["parent_workspace"]["dirty"] is True
    child = Path(result["child_workspace"]["path"])
    assert (child / "original name.txt").read_text() == "committed version\n"
    assert not (child / "renamed file.txt").exists()
    assert not (child / " leading space.txt").exists()
    assert (root / "renamed file.txt").read_text() == "LOCAL_ONLY_CONTENT\n"
    assert git(child, "status", "--porcelain") == ""
    source = next(e["proof"] for e in result["events"] if e["phase"] == "memory_read")
    assert source["source_dirty"] is True and source["source_commit"] == sha
    assert source["unselected_paths"] == result["unselected_paths"]
    assert "LOCAL_ONLY_CONTENT" not in str(result)
    assert "UNTRACKED_ONLY_CONTENT" not in str(result)


async def test_resume_persisted_dirty_full_sha_rechecks_without_new_start(system):
    root = system.service.config.projects_root / "alpha"
    sha = dirty(root)
    # Simulate the old persisted rejection without mutating the immutable request.
    task = system.store.create(
        dict(thread_id="origin", run_id="origin-run", project_id="alpha"),
        "persisted-full-sha",
        dict(kind="work", cwd=str(root), text="Validate demo.", title="Demo", base=sha),
    )
    system.store.update(
        task["id"],
        "blocked",
        {"reason": "dirty_source_commit_selection_required"},
        blocked_reason="dirty_source_commit_selection_required",
    )
    resumed = await system.engine.command("resume", {"task_id": task["id"]})
    assert resumed["phase"] == "executing" and resumed["id"] == task["id"]
    assert resumed["source_dirty"] is True and resumed["source_commit"] == sha
    assert len(system.host.created) == 1
    assert (
        system.store.db.execute("SELECT count(*) FROM managed_tasks").fetchone()[0] == 1
    )


@pytest.mark.parametrize("is_dirty", [True, False])
async def test_missing_commit_is_invalid_base_not_git_metadata(system, is_dirty):
    root = system.service.config.projects_root / "alpha"
    if is_dirty:
        dirty(root)
    result = await start(system, base="a" * 40)
    assert result["blocked_reason"] == "invalid_base_ref", result
    assert result["memory"] is None and not system.host.created
    resumed = await system.engine.command("resume", {"task_id": result["id"]})
    assert resumed["blocked_reason"] == "invalid_base_ref"


async def test_dirty_full_sha_does_not_relax_native_base_not_head(system):
    root = system.service.config.projects_root / "alpha"
    previous = workspace(root)["head"]
    dirty(root)
    result = await start(system, base=previous)
    assert result["blocked_reason"] == "native_child_base_not_head"
    assert result["rejection"]["persisted"] is False and not result["created"]
    assert not system.host.created and not system.service.memory_cycles.list()
