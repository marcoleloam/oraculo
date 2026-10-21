import argparse
import fcntl
import json
from pathlib import Path

import pytest

from oraculo import skill_install as skills


@pytest.fixture
def isolated(tmp_path):
    home = tmp_path / "home with espaços"
    root = home / ".agents" / "skills"
    evidence = tmp_path / "discovery.json"
    evidence.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "provider": "codex",
                "harness_version": "test-1",
                "provider_instance_id": "test-codex",
                "mechanism": "isolated_fixture",
                "source": "synthetic test catalog",
                "observed_at": "2026-01-01T00:00:00Z",
                "roots": [str(root)],
                "skill_sources": [],
            }
        )
    )
    return dict(provider="codex", home=home, root=root, evidence=evidence)


def test_inspect_and_dry_run_never_create_directories(isolated):
    for action in ("inspect", "install", "uninstall"):
        result = skills.operate(action, **isolated, dry_run=True)
        assert result["error_code"] is None
        assert not isolated["home"].exists()
        assert result["confirmed_service"] == "not_observed"
        assert result["loaded_skill"] == "not_observed"


def test_install_hashes_noop_and_uninstall(isolated):
    first = skills.operate("install", **isolated)
    assert first["state"] == "installed"
    target = isolated["root"] / "oraculo"
    source = skills._source_files()
    assert {name: (target / name).read_bytes() for name in source} == source
    manifest = target / skills.MANIFEST
    original = manifest.read_bytes(), manifest.stat().st_mtime_ns
    second = skills.operate("install", **isolated)
    assert second["state"] == "unchanged"
    assert (manifest.read_bytes(), manifest.stat().st_mtime_ns) == original
    assert skills.diagnose(**isolated)["integrity"] == "verified"
    assert skills.operate("uninstall", **isolated)["state"] == "uninstalled"
    assert not target.exists()
    assert skills.operate("uninstall", **isolated)["state"] == "absent"


@pytest.mark.parametrize("action", ["install", "uninstall", "inspect"])
def test_external_edit_preserves_entire_install(isolated, action):
    skills.operate("install", **isolated)
    target = isolated["root"] / "oraculo"
    (target / "SKILL.md").write_text("User's edited instructions\n")
    before = {p: p.read_bytes() for p in target.rglob("*") if p.is_file()}
    result = skills.operate(action, **isolated)
    assert result["error_code"] == "skill_external_edits"
    assert {p: p.read_bytes() for p in target.rglob("*") if p.is_file()} == before


def test_uninstall_preserves_unmanaged_extras_and_symlinks(isolated, tmp_path):
    skills.operate("install", **isolated)
    target = isolated["root"] / "oraculo"
    (target / "notes.txt").write_text("external")
    (target / "empty-user-directory").mkdir()
    outside = tmp_path / "external"
    outside.mkdir()
    (outside / "keep").write_text("external data")
    (target / "external-link").symlink_to(outside, target_is_directory=True)
    result = skills.operate("uninstall", **isolated)
    assert result["state"] == "uninstalled"
    assert result["preserved"] == ["empty-user-directory", "external-link", "notes.txt"]
    assert (target / "empty-user-directory").is_dir()
    assert (target / "notes.txt").read_text() == "external"
    assert (outside / "keep").read_text() == "external data"
    assert not (target / "SKILL.md").exists()


def test_unmanaged_directory_is_never_adopted(isolated):
    target = isolated["root"] / "oraculo"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_bytes(skills._source_files()["SKILL.md"])
    for action in ("install", "uninstall"):
        assert (
            skills.operate(action, **isolated)["error_code"]
            == "skill_unmanaged_destination"
        )
    assert not (target / skills.MANIFEST).exists()


@pytest.mark.parametrize("part", ["root", "target", "file", "parent"])
def test_symlink_conflicts(isolated, tmp_path, part):
    outside = tmp_path / "outside"
    outside.mkdir()
    if part == "file":
        skills.operate("install", **isolated)
        path = isolated["root"] / "oraculo" / "SKILL.md"
        path.unlink()
        (outside / "SKILL.md").write_text("external")
        path.symlink_to(outside / "SKILL.md")
    else:
        path = {
            "root": isolated["root"],
            "target": isolated["root"] / "oraculo",
            "parent": isolated["root"].parent,
        }[part]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.symlink_to(outside, target_is_directory=True)
    result = skills.operate("install", **isolated)
    assert result["error_code"] == "skill_symlink_conflict"
    assert not (outside / skills.MANIFEST).exists()


def test_missing_or_wrong_discovery_blocks_before_write(isolated):
    isolated["evidence"] = None
    assert (
        skills.operate("install", **isolated)["error_code"]
        == "skill_discovery_evidence_required"
    )
    assert not isolated["home"].exists()


def test_ambiguous_evidence_requires_one_explicit_destination(isolated):
    path = isolated["evidence"]
    data = json.loads(path.read_text())
    data["roots"].append(str(isolated["home"] / ".codex/skills"))
    path.write_text(json.dumps(data))
    assert (
        skills.operate("install", **dict(isolated, root=None))["error_code"]
        == "skill_destination_required"
    )
    assert skills.operate("install", **isolated)["state"] == "installed"


@pytest.mark.parametrize("value", [[], {}, {"schema_version": 2}])
def test_invalid_evidence_is_input_error(isolated, value):
    isolated["evidence"].write_text(json.dumps(value))
    with pytest.raises(ValueError, match="invalid_skill_discovery_evidence"):
        skills.operate("install", **isolated)
    assert not isolated["home"].exists()


@pytest.mark.parametrize(
    "provider,duplicate",
    [
        ("codex", ".codex/skills/oraculo/SKILL.md"),
        ("claude", ".claude/commands/oraculo.md"),
        ("grok", ".claude/skills/oraculo/SKILL.md"),
        ("grok", ".agents/commands/oraculo.md"),
    ],
)
def test_compatibility_duplicates_block(isolated, provider, duplicate):
    isolated["provider"] = provider
    isolated["root"] = isolated["home"] / skills.PROVIDERS[provider][0]
    data = json.loads(isolated["evidence"].read_text())
    data.update(provider=provider, roots=[str(isolated["root"])])
    isolated["evidence"].write_text(json.dumps(data))
    path = isolated["home"] / duplicate
    path.parent.mkdir(parents=True)
    path.write_text("external skill")
    assert (
        skills.operate("install", **isolated)["error_code"]
        == "skill_duplicate_discovery"
    )
    assert not (isolated["root"] / "oraculo").exists()
    assert path.read_text() == "external skill"


def test_catalog_plugin_source_blocks_even_outside_known_roots(isolated, tmp_path):
    data = json.loads(isolated["evidence"].read_text())
    data["skill_sources"] = [str(tmp_path / "plugin/skills/oraculo/SKILL.md")]
    isolated["evidence"].write_text(json.dumps(data))
    assert (
        skills.operate("install", **isolated)["error_code"]
        == "skill_duplicate_discovery"
    )


def test_parent_project_discovery_and_external_worktree_boundary(isolated, tmp_path):
    project = tmp_path / "project"
    nested = project / "src" / "sub"
    nested.mkdir(parents=True)
    (project / ".git").write_text("gitdir: /synthetic/main/.git/worktrees/test")
    other = project / ".agents/skills/oraculo"
    other.mkdir(parents=True)
    (other / "SKILL.md").write_text("project skill")
    result = skills.operate("install", **isolated, project=nested)
    assert result["error_code"] == "skill_duplicate_discovery"
    assert str(other / "SKILL.md") in result["conflicts"]
    roots = skills._roots("codex", isolated["home"], nested)
    assert tmp_path / ".agents/skills" not in roots


def test_malicious_manifest_cannot_escape_destination(isolated, tmp_path):
    skills.operate("install", **isolated)
    path = isolated["root"] / "oraculo" / skills.MANIFEST
    data = json.loads(path.read_text())
    external = tmp_path / "keep"
    external.write_text("do not touch")
    data["files"][str(external)] = skills._digest(external.read_bytes())
    data["source_sha256"] = skills._bundle_hash(data["files"])
    path.write_text(json.dumps(data))
    assert (
        skills.operate("uninstall", **isolated)["error_code"]
        == "skill_manifest_invalid"
    )
    assert external.read_text() == "do not touch"


def test_update_removes_only_old_managed_tree(isolated, monkeypatch):
    skills.operate("install", **isolated)
    source = skills._source_files()
    source["SKILL.md"] += b"\nUpdated source.\n"
    del source["references/operations.md"]
    monkeypatch.setattr(skills, "_source_files", lambda: source)
    assert skills.operate("install", **isolated)["state"] == "updated"
    target = isolated["root"] / "oraculo"
    assert (target / "SKILL.md").read_bytes() == source["SKILL.md"]
    assert not (target / "references/operations.md").exists()
    assert not (isolated["root"] / ".oraculo-previous").exists()


def test_update_preserves_external_files_by_blocking(isolated, monkeypatch):
    skills.operate("install", **isolated)
    target = isolated["root"] / "oraculo"
    (target / "user.txt").write_text("keep")
    source = skills._source_files()
    source["SKILL.md"] += b"new"
    monkeypatch.setattr(skills, "_source_files", lambda: source)
    assert (
        skills.operate("install", **isolated)["error_code"]
        == "skill_update_has_external_files"
    )
    assert (target / "user.txt").read_text() == "keep"


def test_failed_swap_restores_previous_install(isolated, monkeypatch):
    skills.operate("install", **isolated)
    before = (isolated["root"] / "oraculo/SKILL.md").read_bytes()
    source = skills._source_files()
    source["SKILL.md"] += b"updated"
    monkeypatch.setattr(skills, "_source_files", lambda: source)
    original = Path.rename

    def fail_stage(self, destination):
        if self.name.startswith(".oraculo-stage-"):
            raise OSError("injected failure")
        return original(self, destination)

    monkeypatch.setattr(Path, "rename", fail_stage)
    assert skills.operate("install", **isolated)["error_code"] == "skill_io_error"
    assert (isolated["root"] / "oraculo/SKILL.md").read_bytes() == before
    assert skills.diagnose(**isolated)["integrity"] == "verified"


def test_interrupted_swap_is_not_overwritten(isolated):
    skills.operate("install", **isolated)
    target = isolated["root"] / "oraculo"
    backup = isolated["root"] / ".oraculo-previous"
    target.rename(backup)
    for action in ("inspect", "install", "uninstall"):
        assert (
            skills.operate(action, **isolated)["error_code"]
            == "skill_interrupted_install"
        )
    assert (backup / "SKILL.md").exists()


def test_installer_lock_blocks_concurrent_mutation(isolated):
    isolated["root"].mkdir(parents=True)
    with (isolated["root"] / ".oraculo-install.lock").open("w") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = skills.operate("install", **isolated)
    assert result["error_code"] == "skill_install_busy"
    assert result["retryable"] is True
    assert not (isolated["root"] / "oraculo").exists()


def test_cli_registration_dispatch_without_config(isolated):
    parser = argparse.ArgumentParser()
    skills.register_parsers(parser.add_subparsers(dest="command", required=True))
    args = parser.parse_args(
        [
            "skill",
            "install",
            "--provider",
            "codex",
            "--root",
            str(isolated["root"]),
            "--home",
            str(isolated["home"]),
            "--evidence",
            str(isolated["evidence"]),
            "--dry-run",
        ]
    )
    assert skills.dispatch(args)["state"] == "installed"
    assert not isolated["home"].exists()
