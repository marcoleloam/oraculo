import subprocess
from pathlib import Path

import pytest

from oraculo.config import Config
from oraculo.projects import ProjectResolutionError, resolve_project


def git(path, *args):
    return subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def repository(path):
    path.mkdir(parents=True)
    git(path, "init", "-b", "main")
    git(
        path,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "--allow-empty",
        "-m",
        "Fixture",
    )
    return path.resolve()


@pytest.fixture
def projects(tmp_path):
    root = tmp_path / "work"
    alpha = repository(root / "alpha")
    beta = repository(root / "beta")
    config = Config(
        projects_root=root, vault_root=tmp_path / "vault", state_dir=tmp_path / "state"
    )
    catalog = [
        {"id": "project-a", "workspaceRoot": str(alpha), "title": "Wrong label"},
        {"id": "project-b", "workspaceRoot": str(beta), "title": "alpha"},
    ]
    return config, catalog, alpha, beta


def test_resolves_cwd_subdirectory_and_ignores_titles(projects):
    config, catalog, alpha, _ = projects
    nested = alpha / "src"
    nested.mkdir()
    result = resolve_project(config, cwd=nested, projects=catalog)
    assert result["project_id"] == "project-a"
    assert result["project_path"] == str(alpha)
    assert result["repository_common_dir"] == str(alpha / ".git")
    assert result["source_cwd"] == str(nested)
    assert result["evidence"]["scope_checked"] is True


def test_external_linked_worktree_identifies_primary_project(projects, tmp_path):
    config, catalog, alpha, _ = projects
    checkout = tmp_path / "isolated checkout"
    git(alpha, "worktree", "add", "-b", "task", str(checkout))
    result = resolve_project(config, cwd=checkout, projects=catalog)
    assert result["project_path"] == str(alpha)
    assert result["project_id"] == "project-a"
    assert result["repository_common_dir"] == str(alpha / ".git")


def test_t3_registered_worktree_maps_to_primary_project(projects, tmp_path):
    config, _, alpha, _ = projects
    checkout = tmp_path / "isolated"
    git(alpha, "worktree", "add", "-b", "task", str(checkout))
    catalog = [{"id": "project-a", "workspaceRoot": str(checkout)}]
    assert resolve_project(config, cwd=alpha, projects=catalog)["project_path"] == str(
        alpha
    )


def test_explicit_other_project_overrides_cwd(projects):
    config, catalog, alpha, beta = projects
    result = resolve_project(config, cwd=alpha, projects=catalog, explicit="beta")
    assert result["project_id"] == "project-b"
    assert result["project_path"] == str(beta)
    assert result["evidence"]["method"] == "explicit_intent"


def test_symlinks_canonicalized_and_registered_alias_accepted(projects):
    config, catalog, alpha, beta = projects
    alias = config.projects_root / "other-name"
    alias.symlink_to(beta, target_is_directory=True)
    catalog[1]["workspaceRoot"] = str(alias)
    assert resolve_project(config, cwd=alias, projects=catalog)["project_path"] == str(
        beta
    )
    assert (
        resolve_project(config, cwd=alpha, projects=catalog, explicit="other-name")[
            "project_id"
        ]
        == "project-b"
    )
    assert (
        resolve_project(config, cwd=alpha, projects=catalog, explicit="../other-name")[
            "project_id"
        ]
        == "project-b"
    )


def test_project_id_is_valid_explicit_selector(projects):
    config, catalog, alpha, _ = projects
    assert (
        resolve_project(config, cwd=alpha, projects=catalog, explicit="project-b")[
            "project_id"
        ]
        == "project-b"
    )


def test_allowlist_is_enforced_for_explicit_and_cwd(projects):
    config, catalog, alpha, beta = projects
    config.allowed_projects = [alpha]
    with pytest.raises(ProjectResolutionError) as error:
        resolve_project(config, cwd=alpha, projects=catalog, explicit="beta")
    assert error.value.reason == "explicit_project_unavailable_or_out_of_scope"
    assert error.value.candidates == [
        {"project_id": "project-a", "project_path": str(alpha)}
    ]
    with pytest.raises(ProjectResolutionError):
        resolve_project(config, cwd=beta, projects=catalog)
    config.allowed_projects = []
    with pytest.raises(ProjectResolutionError):
        resolve_project(config, cwd=alpha, projects=catalog)


def test_cwd_outside_projects_is_blocked(projects, tmp_path):
    config, catalog, _, _ = projects
    with pytest.raises(ProjectResolutionError) as error:
        resolve_project(config, cwd=tmp_path, projects=catalog)
    assert error.value.reason == "cwd_unregistered_or_out_of_scope"


def test_symlink_cannot_authorize_outside_repository(projects, tmp_path):
    config, catalog, alpha, _ = projects
    external = repository(tmp_path / "outside")
    alias = config.projects_root / "alias"
    alias.symlink_to(external, target_is_directory=True)
    catalog.append({"id": "outside", "workspaceRoot": str(alias)})
    with pytest.raises(ProjectResolutionError):
        resolve_project(config, cwd=alpha, projects=catalog, explicit="alias")


def test_duplicate_names_are_ambiguous(projects):
    config, catalog, alpha, _ = projects
    second = repository(config.projects_root / "group" / "alpha")
    catalog.append({"id": "second", "workspaceRoot": str(second)})
    with pytest.raises(ProjectResolutionError) as error:
        resolve_project(config, cwd=alpha, projects=catalog, explicit="alpha")
    assert error.value.reason == "ambiguous_project"
    assert {item["project_id"] for item in error.value.candidates} == {
        "project-a",
        "second",
    }
    assert (
        resolve_project(config, cwd=alpha, projects=catalog, explicit=str(second))[
            "project_id"
        ]
        == "second"
    )


def test_duplicate_t3_identities_for_same_repo_are_ambiguous(projects):
    config, catalog, alpha, _ = projects
    catalog.append({"id": "second", "workspaceRoot": str(alpha)})
    with pytest.raises(ProjectResolutionError, match="ambiguous_project"):
        resolve_project(config, cwd=alpha, projects=catalog)


def test_deduplicates_same_registration(projects):
    config, catalog, alpha, _ = projects
    catalog.append(catalog[0].copy())
    assert (
        resolve_project(config, cwd=alpha, projects=catalog)["project_id"]
        == "project-a"
    )


def test_deleted_registration_and_label_do_not_grant_ownership(projects):
    config, catalog, alpha, _ = projects
    catalog[0]["deletedAt"] = "2025-01-01T00:00:00Z"
    with pytest.raises(ProjectResolutionError):
        resolve_project(config, cwd=alpha, projects=catalog, explicit="alpha")


def test_plain_directory_can_resolve_with_no_git_identity(projects):
    config, catalog, alpha, _ = projects
    plain = config.projects_root / "plain"
    plain.mkdir()
    catalog.append({"id": "plain", "repoPath": str(plain)})
    result = resolve_project(config, cwd=alpha, projects=catalog, explicit="plain")
    assert result["repository_common_dir"] is None


def test_ambient_git_environment_cannot_change_identity(projects, monkeypatch):
    config, catalog, alpha, beta = projects
    monkeypatch.setenv("GIT_DIR", str(beta / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(beta))
    assert (
        resolve_project(config, cwd=alpha, projects=catalog)["project_id"]
        == "project-a"
    )


def test_no_neighbor_content_or_directory_listing(projects, monkeypatch):
    config, catalog, alpha, _ = projects

    def forbidden(*args, **kwargs):
        raise AssertionError("project content must not be read")

    monkeypatch.setattr(Path, "read_text", forbidden)
    monkeypatch.setattr(Path, "read_bytes", forbidden)
    monkeypatch.setattr(Path, "iterdir", forbidden)
    assert (
        resolve_project(config, cwd=alpha, projects=catalog)["project_id"]
        == "project-a"
    )


def test_empty_intent_and_missing_cwd_are_explicit_errors(projects):
    config, catalog, alpha, _ = projects
    with pytest.raises(ProjectResolutionError, match="empty_project_intent"):
        resolve_project(config, cwd=alpha, projects=catalog, explicit=" ")
    with pytest.raises(ProjectResolutionError, match="path_unavailable"):
        resolve_project(config, cwd=alpha / "missing", projects=catalog)


def test_unregistered_symlink_alias_under_projects_root(projects):
    config, catalog, alpha, beta = projects
    alias = config.projects_root / "shortcut"
    alias.symlink_to(beta, target_is_directory=True)
    assert (
        resolve_project(config, cwd=alpha, projects=catalog, explicit="shortcut")[
            "project_id"
        ]
        == "project-b"
    )


def test_repository_metadata_timeout_is_explicit(projects, monkeypatch):
    config, catalog, alpha, _ = projects

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("git", 5)

    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(ProjectResolutionError, match="git_metadata_unavailable"):
        resolve_project(config, cwd=alpha, projects=catalog)


def test_permission_failure_does_not_become_plain_directory(projects, monkeypatch):
    config, catalog, alpha, _ = projects

    def denied(*args, **kwargs):
        return subprocess.CompletedProcess("git", 128, "", "Permission denied")

    monkeypatch.setattr(subprocess, "run", denied)
    with pytest.raises(ProjectResolutionError, match="git_metadata_unavailable"):
        resolve_project(config, cwd=alpha, projects=catalog, explicit="alpha")


def test_empty_allowlist_blocks_external_worktree(projects, tmp_path):
    config, catalog, alpha, _ = projects
    checkout = tmp_path / "checkout"
    git(alpha, "worktree", "add", "-b", "task", str(checkout))
    config.allowed_projects = []
    with pytest.raises(
        ProjectResolutionError, match="cwd_unregistered_or_out_of_scope"
    ):
        resolve_project(config, cwd=checkout, projects=catalog)
