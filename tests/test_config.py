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
