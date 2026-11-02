"""Refresh a derived vault without overwriting edits or losing the last export."""

import os

import pytest

from oraculo.experience_portfolio_refresh import refresh_export


def test_refresh_accepts_added_and_removed_generated_notes(tmp_path):
    target = tmp_path / "Cofre"
    refresh_export(target, {"Inicio.md": "Anterior", "Antiga.md": "Anterior"})
    receipt = refresh_export(target, {"Inicio.md": "Atual", "Nova.md": "Conteúdo"})
    assert receipt["state"] == "refreshed"
    assert receipt["path"] == str(target)
    assert (target / "Nova.md").read_text() == "Conteúdo"
    assert not (target / "Antiga.md").exists()
    assert not list(tmp_path.glob(".Cofre.*"))


@pytest.mark.parametrize("edit", ["modified", "unknown", "missing", "symlink"])
def test_refresh_preserves_local_changes_and_unknown_files(tmp_path, edit):
    target = tmp_path / "Cofre"
    refresh_export(target, {"Inicio.md": "Anterior"})
    if edit == "modified":
        (target / "Inicio.md").write_text("Edição do usuário")
    elif edit == "unknown":
        (target / "Pessoal.md").write_text("Nota pessoal")
    elif edit == "missing":
        (target / "Inicio.md").unlink()
    else:
        (target / "Alias.md").symlink_to(target / "Inicio.md")
    before = (target / ".oraculo-export.json").read_bytes()
    with pytest.raises(ValueError):
        refresh_export(target, {"Inicio.md": "Atual", "Nova.md": "Novo"})
    assert (target / ".oraculo-export.json").read_bytes() == before
    assert not (target / "Nova.md").exists()
    if edit == "modified":
        assert (target / "Inicio.md").read_text() == "Edição do usuário"


def test_failed_replacement_restores_previous_export(tmp_path, monkeypatch):
    target = tmp_path / "Cofre"
    refresh_export(target, {"Inicio.md": "Anterior"})
    replace = os.replace

    def fail_stage(source, destination):
        if ".staging-" in str(source):
            raise OSError("synthetic replacement failure")
        return replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_stage)
    with pytest.raises(OSError, match="synthetic replacement failure"):
        refresh_export(target, {"Inicio.md": "Atual"})
    assert (target / "Inicio.md").read_text() == "Anterior"
    assert not list(tmp_path.glob(".Cofre.*"))


def test_invalid_staged_paths_leave_previous_export_untouched(tmp_path):
    target = tmp_path / "Cofre"
    refresh_export(target, {"Inicio.md": "Anterior"})
    with pytest.raises(ValueError):
        refresh_export(target, {"../Escape.md": "Inválido"})
    assert (target / "Inicio.md").read_text() == "Anterior"
    assert not (tmp_path / "Escape.md").exists()


def test_refresh_retains_obsidian_workspace_and_new_local_configuration(tmp_path):
    target = tmp_path / "Cofre"
    refresh_export(
        target,
        {"Inicio.md": "Anterior", ".obsidian/workspace.json": '{"active":"Inicio"}'},
    )
    workspace = '{"active":"Projeto selecionado pelo usuário"}'
    (target / ".obsidian/workspace.json").write_text(workspace)
    (target / ".obsidian/app.json").write_text('{"readableLineLength":false}')
    refresh_export(
        target,
        {"Inicio.md": "Atual", ".obsidian/workspace.json": '{"active":"Inicio"}'},
    )
    assert (target / ".obsidian/workspace.json").read_text() == workspace
    assert (target / ".obsidian/app.json").read_text() == '{"readableLineLength":false}'
    assert (target / "Inicio.md").read_text() == "Atual"
