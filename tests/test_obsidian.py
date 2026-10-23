import copy
import io
import json

import pytest

from oraculo.cli import main, make_envelope, request
from oraculo.memory.schema import corpus
from oraculo.obsidian import project_labels_css


def test_titles_are_selective_deterministic_and_preserve_canonical_paths():
    snapshot = {
        "projects": [
            {"id": "beta", "title": "Mesmo nome", "workspaceRoot": "/private/b"},
            {"id": "alpha", "title": "Mesmo nome", "workspaceRoot": "/private/a"},
            {"id": "other", "title": "Cliente privado"},
        ],
        "nextCursor": None,
    }
    before = copy.deepcopy(snapshot)
    css = project_labels_css(snapshot, ["beta", "alpha", "alpha"])
    assert css == project_labels_css(snapshot, ["alpha", "beta"])
    assert css.index('data-path="projects/alpha"') < css.index(
        'data-path="projects/beta"'
    )
    assert css.count('content: "Mesmo nome — ";') == 2
    assert "private" not in css and "Cliente" not in css and "other" not in css
    assert "reviews/" not in css and "shared/" not in css
    assert 'data-type="file-explorer"' in css
    assert "data-path^=" not in css and "data-path*=" not in css
    assert 'contenteditable="true"' in css
    assert (
        "::before" in css and "display: none" not in css and "font-size: 0" not in css
    )
    assert snapshot == before


def test_service_snapshot_uses_only_basename_without_reading_path():
    snapshot = {
        "ok": True,
        "data": {
            "projects": [
                {
                    "id": "alpha",
                    "path": "/not/a/real/path/Projeto Á",
                    "status": "registered",
                }
            ]
        },
    }
    css = project_labels_css(snapshot, ["alpha"])
    assert 'content: "Projeto Á — ";' in css
    assert "/not/a/real" not in css


def test_title_takes_precedence_and_non_filename_ids_use_existing_corpus_mapping():
    project_id = "mcp:project"
    css = project_labels_css(
        {
            "projects": [
                {"id": project_id, "title": "Título real", "path": "/work/old-name"}
            ]
        },
        [project_id],
    )
    assert f'data-path="{corpus(project_id)}"' in css
    assert "Título real" in css and "old-name" not in css


def test_css_escape_does_not_allow_title_to_create_new_rules():
    title = 'Á " \\ \n\r\f\t\x7f </style> */ ; } body { color: red; }'
    css = project_labels_css({"projects": [{"id": "alpha", "title": title}]}, ["alpha"])
    content = next(line for line in css.splitlines() if line.startswith("  content:"))
    assert content == (
        '  content: "Á \\22  \\5c  \\a \\d \\c \\9 \\7f  '
        '\\3c /style\\3e  */ ; } body { color: red; } — ";'
    )
    assert "</style>" not in css


@pytest.mark.parametrize(
    "snapshot,ids,error",
    [
        ({"projects": []}, ["absent"], "unknown_project"),
        ({"ok": False, "data": {"projects": []}}, ["alpha"], "failed_status"),
        ({"projects": []}, [], "requires_snapshot"),
        ({"projects": []}, ["shared"], "shared_is_not"),
        ({"projects": []}, ["../alpha"], "invalid_project_id"),
        ({"projects": []}, ["alpha/beta"], "invalid_project_id"),
        ({"projects": {}}, ["alpha"], "invalid_projects_snapshot"),
        ({"ok": True}, ["alpha"], "invalid_projects_snapshot"),
        ([], ["alpha"], "requires_snapshot"),
        ({"projects": [None]}, ["alpha"], "invalid_project"),
        ({"projects": [{"id": []}]}, ["alpha"], "invalid_project"),
        ({"projects": [], "nextCursor": 0}, ["alpha"], "complete_snapshot"),
        (
            {"projects": [{"id": "alpha", "title": "A"}] * 2},
            ["alpha"],
            "duplicate_project",
        ),
        (
            {"projects": [{"id": "alpha", "title": "A", "deletedAt": "2026-01-01"}]},
            ["alpha"],
            "inactive_project",
        ),
        (
            {"projects": [{"id": "alpha", "title": "A", "status": "removed"}]},
            ["alpha"],
            "inactive_project",
        ),
        (
            {"projects": [{"id": "alpha", "path": "relative"}]},
            ["alpha"],
            "missing_name",
        ),
        ({"projects": [{"id": "alpha", "title": " "}]}, ["alpha"], "invalid_name"),
        ({"projects": [{"id": "alpha", "title": "\x00"}]}, ["alpha"], "invalid_name"),
        ({"projects": [{"id": "alpha", "title": []}]}, ["alpha"], "invalid_name"),
    ],
)
def test_invalid_or_ambiguous_metadata_fails_closed(snapshot, ids, error):
    with pytest.raises(ValueError, match=error):
        project_labels_css(snapshot, ids)


def test_distinct_ids_with_colliding_corpus_are_rejected():
    a = "mcp:project"
    b = corpus(a).removeprefix("projects/")
    with pytest.raises(ValueError, match="path_collision"):
        project_labels_css({"projects": []}, [a, b])
    with pytest.raises(ValueError, match="path_collision"):
        project_labels_css({"projects": [{"id": b, "title": "Another project"}]}, [a])


def test_regeneration_replaces_renamed_and_removed_labels():
    first = {"projects": [{"id": "a", "title": "Antes"}, {"id": "b", "title": "B"}]}
    second = {"projects": [{"id": "a", "title": "Depois"}]}
    assert "Antes" in project_labels_css(first, ["a", "b"])
    css = project_labels_css(second, ["a"])
    assert "Depois" in css and "Antes" not in css and "projects/b" not in css


@pytest.mark.parametrize("use_stdin", [False, True])
def test_cli_is_offline_and_emits_only_css(tmp_path, monkeypatch, capsys, use_stdin):
    snapshot = {"projects": [{"id": "alpha", "title": "Projeto Á"}]}
    source = tmp_path / "projects.json"
    source.write_text(json.dumps(snapshot), encoding="utf-8")
    original = source.read_bytes()

    def forbidden(*args, **kwargs):
        pytest.fail("Offline renderer must not open configuration, database or service")

    monkeypatch.setattr("oraculo.cli.Config.load", forbidden)
    monkeypatch.setattr("oraculo.cli.request", forbidden)
    monkeypatch.setattr("oraculo.state.State.__init__", forbidden)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(snapshot)))
    argv = ["obsidian", "project-labels", "--project", "alpha"]
    if not use_stdin:
        argv += ["--file", str(source)]
    assert main(argv) == 0
    captured = capsys.readouterr()
    assert captured.out == project_labels_css(snapshot, ["alpha"])
    assert captured.err == ""
    assert source.read_bytes() == original
    assert list(tmp_path.iterdir()) == [source]


@pytest.mark.parametrize(
    "raw", ["{", '{"ok":false}', '{"projects":[{"id":"alpha","title":"A"}]}']
)
def test_cli_failure_never_outputs_partial_css(monkeypatch, capsys, raw):
    monkeypatch.setattr("sys.stdin", io.StringIO(raw))
    assert (
        main(
            ["obsidian", "project-labels", "--project", "alpha", "--project", "missing"]
        )
        == 1
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err)["ok"] is False


async def test_real_service_snapshot_generates_without_touching_vault(
    running_service, service_config, tmp_path, capsys
):
    config, _ = service_config
    response = await request(config, make_envelope("status.get", "alpha", {}))
    assert response["ok"] is True
    source = tmp_path / "status.json"
    source.write_text(json.dumps(response), encoding="utf-8")
    before = {
        p.relative_to(config.vault_root): p.read_bytes()
        for p in config.vault_root.rglob("*")
        if p.is_file()
    }
    assert (
        main(
            ["obsidian", "project-labels", "--file", str(source), "--project", "alpha"]
        )
        == 0
    )
    assert 'content: "alpha — ";' in capsys.readouterr().out
    after = {
        p.relative_to(config.vault_root): p.read_bytes()
        for p in config.vault_root.rglob("*")
        if p.is_file()
    }
    assert before == after
