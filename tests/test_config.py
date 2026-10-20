import pytest
from pydantic import ValidationError

from oraculo.config import Config
from oraculo.state import State


def test_projects_scope_and_registration(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    inside = root / "repo"
    inside.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "escape").symlink_to(outside, target_is_directory=True)
    config = Config(
        projects_root=root, vault_root=tmp_path / "vault", state_dir=tmp_path / "state"
    )
    assert config.project_in_scope(inside)
    assert not config.project_in_scope(root / "escape")
    assert not config.project_in_scope(tmp_path / "work-other")
    state = State(config.db_path)
    state.register_project("p", inside)
    state.register_project("missing", root / "unregistered", "unregistered")
    assert state.get_project("missing")["status"] == "unregistered"
    assert all(
        not p.billing_verified and not p.models for p in config.providers.values()
    )


def test_no_spend_or_embedded_credentials():
    with pytest.raises(ValidationError):
        Config(additional_spend_allowed=True)
    with pytest.raises(ValidationError):
        Config(t3_mcp_url="https://token:secret@example.com/mcp")


def test_explicit_scope_excludes_siblings_and_nested_projects(tmp_path):
    root = tmp_path / "work"
    project = root / "projeto-a"
    project.mkdir(parents=True)
    alias = root / "projeto-alias"
    alias.symlink_to(project, target_is_directory=True)
    config = Config(
        projects_root=root,
        allowed_projects=[str(alias)],
        vault_root=tmp_path / "vault",
        state_dir=tmp_path / "state",
    )
    assert config.project_in_scope(project)
    assert config.project_in_scope(alias)
    assert not config.project_in_scope(root / "other-client")
    assert not config.project_in_scope(project / "nested-repo")
    assert not config.project_in_scope(root)
    # A replaced project symlink must not authorize a sibling or escape the root.
    project.rmdir()
    project.symlink_to(root / "other-client", target_is_directory=True)
    assert not config.project_in_scope(project)
    project.unlink()
    project.symlink_to(tmp_path / "outside", target_is_directory=True)
    assert not config.project_in_scope(project)


def test_empty_explicit_scope_authorizes_nothing(tmp_path):
    config = Config(
        projects_root=tmp_path / "work",
        allowed_projects=[],
        vault_root=tmp_path / "vault",
        state_dir=tmp_path / "state",
    )
    assert not config.project_in_scope(tmp_path / "work" / "repo")


@pytest.mark.parametrize("location", ["outside", "work", "work-other/repo"])
def test_explicit_scope_cannot_escape_root(tmp_path, location):
    with pytest.raises(ValidationError, match="strictly inside projects_root"):
        Config(
            projects_root=tmp_path / "work",
            allowed_projects=[tmp_path / location],
            vault_root=tmp_path / "vault",
            state_dir=tmp_path / "state",
        )
