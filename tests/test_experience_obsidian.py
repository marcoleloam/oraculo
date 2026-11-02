"""Colors, graph groups and the project map. Synthetic snapshots only."""

import copy
import json
import re
import sys
import types

import pytest

from oraculo.experience_obsidian import (
    CANVAS_PATH,
    GRAPH_PATH,
    GUIDE_PATH,
    STOPWORDS,
    VOCABULARY,
    assign_project_colors,
    connection_suggestions,
    palette,
    path_query,
    shared_color,
)
from oraculo.experience_portfolio import _place, portfolio_export_files, portfolio_model
from oraculo.experience_portfolio_refresh import refresh_export
from oraculo.memory.schema import digest, links, resolve_link

_CANVAS_NODE = {
    "text": {"id", "type", "x", "y", "width", "height", "color", "text"},
    "file": {"id", "type", "x", "y", "width", "height", "color", "file"},
    "group": {"id", "type", "x", "y", "width", "height", "color", "label"},
}
_CANVAS_EDGE = {
    "id",
    "fromNode",
    "fromSide",
    "toNode",
    "toSide",
    "toEnd",
    "color",
    "label",
}
_SIDES = {"top", "right", "bottom", "left"}
_HEX = re.compile(r"^#[0-9A-F]{6}$")


def _integer(value):
    return isinstance(value, int) and not isinstance(value, bool)


def page(project_id, page_id, title, body="Corpo visível.\n", tags=None):
    tag_text = "[" + ", ".join(tags) + "]" if tags else "[]"
    content = (
        "---\n"
        "schema_version: 1\n"
        f"page_id: {page_id}\n"
        f"project_id: {project_id}\n"
        "type: concept\n"
        "sources: []\n"
        f"tags: {tag_text}\n"
        "aliases: []\n"
        "reviewed: false\n"
        "created: '2026-11-02'\n"
        "updated: '2026-11-02'\n"
        "---\n"
        f"# {title}\n\n{body}"
    )
    return {
        "path": f"projects/{project_id}/wiki/concepts/{page_id}.md",
        "content": content,
        "sha256": digest(content),
    }


def snapshot(project_id, title, documents=None):
    return {
        "schema_version": 1,
        "observed_at": "2026-11-02T00:00:00Z",
        "projects": [{"id": project_id, "title": title}],
        "documents": documents or [],
        "tasks": [],
        "cycles": [],
    }


def install_html(monkeypatch):
    module = types.ModuleType("oraculo.experience_portfolio_html")
    module.dashboard = lambda model: "<!doctype html><title>Carteira</title>\n"
    monkeypatch.setitem(sys.modules, "oraculo.experience_portfolio_html", module)


def exported(monkeypatch, snapshots):
    install_html(monkeypatch)
    model = portfolio_model(snapshots)
    before = copy.deepcopy(model["links"])
    files = portfolio_export_files(model)
    assert model["links"] == before
    return model, files


def canvas_of(files):
    return json.loads(files[CANVAS_PATH])


def _contains(outer, inner, margin=0):
    return (
        inner["x"] >= outer["x"] + margin
        and inner["y"] >= outer["y"] + margin
        and inner["x"] + inner["width"] <= outer["x"] + outer["width"] - margin
        and inner["y"] + inner["height"] <= outer["y"] + outer["height"] - margin
    )


def _separated(left, right, gap=12):
    return (
        left["x"] + left["width"] + gap <= right["x"]
        or right["x"] + right["width"] + gap <= left["x"]
        or left["y"] + left["height"] + gap <= right["y"]
        or right["y"] + right["height"] + gap <= left["y"]
    )


def assert_canvas_spec(document, files):
    """JSON Canvas 1.0: groups, files, text, hex colors, labeled edges."""
    assert set(document) == {"nodes", "edges"}
    nodes = document["nodes"]
    edges = document["edges"]
    assert isinstance(nodes, list) and isinstance(edges, list)
    ids = [node["id"] for node in nodes]
    assert len(ids) == len(set(ids))
    assert "dashed" not in json.dumps(document)
    by_id = {}
    for node in nodes:
        assert node["type"] in _CANVAS_NODE
        assert set(node) == _CANVAS_NODE[node["type"]]
        assert isinstance(node["id"], str) and node["id"]
        for key in ("x", "y", "width", "height"):
            assert _integer(node[key])
        assert node["width"] > 0 and node["height"] > 0
        assert _HEX.fullmatch(node["color"])
        if node["type"] == "text":
            assert isinstance(node["text"], str) and node["text"]
        if node["type"] == "file":
            assert node["file"] in files
        if node["type"] == "group":
            assert isinstance(node["label"], str) and node["label"]
        by_id[node["id"]] = node
    edge_ids = []
    for edge in edges:
        assert set(edge) <= _CANVAS_EDGE
        assert {"id", "fromNode", "toNode", "label"} <= set(edge)
        assert isinstance(edge["label"], str) and edge["label"]
        assert edge["label"].startswith(("confirmada · ", "Possível — "))
        assert edge["fromNode"] in by_id and edge["toNode"] in by_id
        assert edge["fromSide"] in _SIDES and edge["toSide"] in _SIDES
        assert edge["toEnd"] == "arrow"
        assert _HEX.fullmatch(edge["color"])
        edge_ids.append(edge["id"])
    assert len(edge_ids) == len(set(edge_ids))
    assert set(edge_ids).isdisjoint(ids)
    for index, left in enumerate(nodes):
        for right in nodes[index + 1 :]:
            if _contains(left, right) or _contains(right, left):
                outer, inner = (
                    (left, right) if _contains(left, right) else (right, left)
                )
                assert outer["type"] == "group" and inner["type"] != "group"
                assert _contains(outer, inner, margin=12)
                assert nodes.index(outer) < nodes.index(inner)
            else:
                assert _separated(left, right)
    for target in links(files[GUIDE_PATH]):
        assert resolve_link(target, set(files), GUIDE_PATH) in files
    for target in links(files["Inicio.md"]):
        assert resolve_link(target, set(files), "Inicio.md") in files


def test_colors_follow_identity_not_title_or_order():
    forward = assign_project_colors(["beta", "alpha", "empty"])
    backward = assign_project_colors(["empty", "alpha", "beta"])
    assert forward == backward
    assert forward["alpha"]["hex"] != forward["beta"]["hex"]
    assert forward["alpha"]["name"] != forward["beta"]["name"]
    renamed = portfolio_model([snapshot("alpha", "Um"), snapshot("beta", "Outro nome")])
    same_ids = portfolio_model(
        [snapshot("beta", "Terceiro"), snapshot("alpha", "Quarto")]
    )
    assert (
        assign_project_colors(["alpha", "beta"])["alpha"]
        == assign_project_colors(
            [project["project_id"] for project in renamed["projects"]]
        )["alpha"]
    )
    assert [project["project_id"] for project in renamed["projects"]] == [
        project["project_id"] for project in same_ids["projects"]
    ]
    many = [f"project-{index:02d}" for index in range(len(palette()) + 1)]
    colors = assign_project_colors(list(reversed(many)))
    assert len({item["hex"] for item in colors.values()}) == len(many)
    assert shared_color()["hex"] not in {item["hex"] for item in colors.values()}
    assert all(item["name"] for item in colors.values())


def test_graph_queries_are_scoped_and_escape_the_folder(monkeypatch):
    model, files = exported(
        monkeypatch,
        [
            snapshot("alpha", "Aurora", [page("alpha", "a", "Diário")]),
            snapshot("beta", "Aurora", [page("beta", "b", "Diário")]),
            snapshot("plus", "A+B (teste).", [page("plus", "c", "Diário")]),
        ],
    )
    graph = json.loads(files[GRAPH_PATH])
    assert set(graph) >= {"search", "colorGroups", "showAttachments", "hideUnresolved"}
    assert "Fontes/" in graph["search"] and "Evidencias/" in graph["search"]
    assert "-path:/^Inicio\\.md$/" in graph["search"]
    assert graph["showAttachments"] is False
    roots = {
        project["project_id"]: project["export_root"] for project in model["projects"]
    }
    queries = [group["query"] for group in graph["colorGroups"]]
    assert queries[-1] == path_query("Compartilhado")
    for project_id, root in roots.items():
        query = path_query(root)
        assert query in queries
        own = root + "/Inicio.md"
        assert (root + "/") in own
        for other in roots.values():
            if other == root:
                continue
            assert (
                not (other + "/Inicio.md")
                .casefold()
                .startswith((root + "/").casefold())
            )
    assert path_query("A+B (teste).") == 'path:"A+B (teste)./"'
    assert path_query('weird"name') == 'path:"weird\\"name/"'
    assert any("/Fontes/" in path for path in files)
    assert any("/Evidencias/" in path for path in files)
    assert files[
        roots["alpha"] + "/Evidencias/projects/alpha/wiki/concepts/a.md.txt"
    ].startswith("---\n")
    assert sorted(path for path in files if ".obsidian/" in path) == [
        ".obsidian/graph.json",
        ".obsidian/workspace.json",
    ]
    assert digest(model["projects"][0]["project_id"])[:12] not in json.dumps(
        [
            node["label"]
            for node in canvas_of(files)["nodes"]
            if node["type"] == "group" and node["label"] != "Possível — a validar"
        ]
    )
    labels = [
        node["label"]
        for node in canvas_of(files)["nodes"]
        if node["type"] == "group" and node["label"].startswith("Aurora")
    ]
    assert len(labels) == 2 and labels[0] != labels[1]
    assert all("·" in label for label in labels)


def test_place_rejects_nested_obsidian_and_keeps_root_graph():
    _place({}, ".obsidian/workspace.json", "{}\n")
    _place({}, ".obsidian/graph.json", "{}\n")
    for path in (
        ".obsidian/plugins/graph.json",
        "Projetos/Aurora/.obsidian/graph.json",
        ".obsidian/graph.json/extra",
        ".obsidian/app.json",
    ):
        with pytest.raises(ValueError, match="experience_nested_obsidian"):
            _place({}, path, "{}\n")
    with pytest.raises(ValueError, match="experience_unsafe_document_path"):
        _place({}, ".obsidian/../graph.json", "{}\n")


def test_canvas_resolves_files_and_does_not_invent_confirmed_links(monkeypatch):
    sentinel = "CORPO_PRIVADO_QUE_NAO_PODE_VAZAR"
    model, files = exported(
        monkeypatch,
        [
            snapshot(
                "alpha",
                "Aurora",
                [
                    page("alpha", "sql", "Guia de SQL", body=sentinel + "\n"),
                    page("alpha", "other", "Diário"),
                ],
            ),
            snapshot(
                "beta",
                "Aurora",
                [page("beta", "sql", "Consulta SQL", body="outro corpo\n")],
            ),
        ],
    )
    assert model["links"] == []
    document = canvas_of(files)
    assert_canvas_spec(document, files)
    labels = [edge["label"] for edge in document["edges"]]
    assert labels == ["Possível — SQL"]
    assert not any(label.startswith("confirmada") for label in labels)
    guide = files[GUIDE_PATH]
    assert "Ligações confirmadas: 0." in guide
    assert "Possível — a validar" in guide
    assert "Possível — SQL" in guide
    assert sentinel not in guide
    assert sentinel not in files[CANVAS_PATH]
    assert "[[Mapa dos projetos.canvas|Mapa dos projetos]]" in files["Inicio.md"]
    assert CANVAS_PATH in files and GUIDE_PATH in files
    suggestion_groups = [
        node
        for node in document["nodes"]
        if node.get("label") == "Possível — a validar"
    ]
    assert len(suggestion_groups) == 1
    inside = [
        node
        for node in document["nodes"]
        if node is not suggestion_groups[0] and _contains(suggestion_groups[0], node)
    ]
    assert any(
        node["type"] == "file" and node["file"].endswith("Guia de SQL.md")
        for node in inside
    )
    assert any(
        node["type"] == "file" and "Consulta SQL.md" in node["file"] for node in inside
    )


def test_suggestions_use_real_terms_and_stop_at_the_limit(monkeypatch):
    nosql, _files = exported(
        monkeypatch,
        [
            snapshot("alpha", "Um", [page("alpha", "a", "Índice NoSQL")]),
            snapshot("beta", "Dois", [page("beta", "b", "Curso de SQL")]),
        ],
    )
    assert connection_suggestions(nosql) == {"kept": [], "omitted": 0}

    postgres, _files = exported(
        monkeypatch,
        [
            snapshot("alpha", "Um", [page("alpha", "a", "Migrar PostgreSQL")]),
            snapshot("beta", "Dois", [page("beta", "b", "Guia SQL")]),
        ],
    )
    assert connection_suggestions(postgres)["kept"] == []

    plural, _files = exported(
        monkeypatch,
        [
            snapshot("alpha", "Um", [page("alpha", "a", "APIs internas")]),
            snapshot("beta", "Dois", [page("beta", "b", "API pública")]),
        ],
    )
    assert connection_suggestions(plural)["kept"] == []

    stopped, _files = exported(
        monkeypatch,
        [
            snapshot(
                "alpha",
                "Um",
                [page("alpha", "a", "fix da tarefa", tags=["tarefa", "testes"])],
            ),
            snapshot(
                "beta",
                "Dois",
                [page("beta", "b", "onda do projeto", tags=["dev", "projeto"])],
            ),
        ],
    )
    assert connection_suggestions(stopped)["kept"] == []
    assert STOPWORDS >= {"fix", "tarefa", "dev", "onda", "projeto", "testes"}

    body_only, _files = exported(
        monkeypatch,
        [
            snapshot(
                "alpha",
                "SQL",
                [page("alpha", "a", "Nota comum", body="usa SQL no corpo\n")],
            ),
            snapshot(
                "beta", "SQL", [page("beta", "b", "Outra nota", body="também SQL\n")]
            ),
        ],
    )
    assert connection_suggestions(body_only)["kept"] == []

    same_title, _files = exported(
        monkeypatch,
        [
            snapshot("alpha", "Aurora", [page("alpha", "a", "Diário")]),
            snapshot("beta", "Aurora", [page("beta", "b", "Diário")]),
        ],
    )
    assert connection_suggestions(same_title)["kept"] == []

    topics = exported(
        monkeypatch,
        [
            snapshot("alpha", "Um", [page("alpha", "a", "Nota", tags=["RAG"])]),
            snapshot("beta", "Dois", [page("beta", "b", "Outra", tags=["RAG"])]),
        ],
    )[0]
    topic_rows = connection_suggestions(topics)["kept"]
    assert [row["term"] for row in topic_rows] == ["RAG"]
    assert topic_rows[0]["bases"] == ("topico",)
    assert "Não é uma ligação confirmada." in topic_rows[0]["reason"]

    mixed, _files = exported(
        monkeypatch,
        [
            snapshot("alpha", "Um", [page("alpha", "a", "Nota", tags=["SQL"])]),
            snapshot("beta", "Dois", [page("beta", "b", "Guia SQL")]),
        ],
    )
    assert connection_suggestions(mixed)["kept"] == []

    limited_model, limited_files = exported(
        monkeypatch,
        [
            snapshot(
                "alpha",
                "Um",
                [
                    page("alpha", "api", "Contrato da API"),
                    page("alpha", "pdf", "Ler o PDF"),
                    page("alpha", "sql-a", "SQL alfa"),
                    page("alpha", "sql-b", "SQL beta"),
                    page("alpha", "sql-c", "SQL gama"),
                ],
            ),
            snapshot(
                "beta",
                "Dois",
                [
                    page("beta", "api", "Cliente da API"),
                    page("beta", "pdf", "Exportar PDF"),
                    page("beta", "sql", "Relatório SQL"),
                ],
            ),
        ],
    )
    report = connection_suggestions(limited_model)
    assert [row["term"] for row in report["kept"]] == ["API", "PDF"]
    assert report["omitted"] == 1
    assert len(report["kept"][0]["left_notes"]) <= 2
    assert "Ficaram de fora 1" in limited_files[GUIDE_PATH]
    labels = [edge["label"] for edge in canvas_of(limited_files)["edges"]]
    assert labels == ["Possível — API", "Possível — PDF"]
    assert "SQL" in VOCABULARY.values()
    three = [
        page("alpha", "sql-a", "SQL alfa"),
        page("alpha", "sql-b", "SQL beta"),
        page("alpha", "sql-c", "SQL gama"),
    ]
    evidence_model, evidence_files = exported(
        monkeypatch,
        [
            snapshot("alpha", "Um", three),
            snapshot("beta", "Dois", [page("beta", "sql", "Relatório SQL")]),
        ],
    )
    notes = connection_suggestions(evidence_model)["kept"][0]["left_notes"]
    assert [note["title"] for note in notes] == ["SQL alfa", "SQL beta"]
    assert "SQL gama" not in evidence_files[CANVAS_PATH]


def test_refresh_updates_generated_map_and_preserves_edited_config(
    tmp_path, monkeypatch
):
    alpha = snapshot("alpha", "Aurora", [page("alpha", "a", "Guia de SQL")])
    beta = snapshot("beta", "Boreal", [page("beta", "b", "Consulta SQL")])
    _model_ab, files_ab = exported(monkeypatch, [alpha, beta])
    _model_a, files_a = exported(monkeypatch, [alpha])
    target = tmp_path / "Cofre"
    refresh_export(target, files_ab)
    manifest = json.loads((target / ".oraculo-export.json").read_text())
    assert CANVAS_PATH in manifest and GUIDE_PATH in manifest and GRAPH_PATH in manifest
    original_graph = (target / GRAPH_PATH).read_bytes()
    edited_graph = '{"colorGroups":[],"search":"editado pelo usuario"}\n'
    edited_workspace = '{"active":"humano"}\n'
    (target / GRAPH_PATH).write_text(edited_graph)
    (target / ".obsidian/workspace.json").write_text(edited_workspace)
    (target / ".obsidian/app.json").write_text('{"readableLineLength":false}')
    canvas = target / CANVAS_PATH
    generated_canvas = canvas.read_text()
    edited_canvas = json.loads(generated_canvas)
    edited_canvas["nodes"][0]["x"] += 1
    canvas.write_text(json.dumps(edited_canvas))
    with pytest.raises(ValueError, match="experience_export_locally_modified"):
        refresh_export(target, files_a)
    assert json.loads(canvas.read_text()) == edited_canvas
    assert (target / GRAPH_PATH).read_text() == edited_graph
    # Native Obsidian reformats Canvas JSON on open, without changing its data.
    canvas.write_text(json.dumps(json.loads(generated_canvas), indent=2))
    guide = target / GUIDE_PATH
    generated_guide = guide.read_text()
    guide.write_text("nota editada\n")
    with pytest.raises(ValueError, match="experience_export_locally_modified"):
        refresh_export(target, files_a)
    guide.write_text(generated_guide)
    nested = target / ".obsidian" / "plugins"
    nested.mkdir()
    (nested / "graph.json").write_text("{}\n")
    with pytest.raises(ValueError, match="experience_export_locally_modified"):
        refresh_export(target, files_a)
    (nested / "graph.json").unlink()
    nested.rmdir()
    refresh_export(target, files_a)
    assert (target / GRAPH_PATH).read_text() == edited_graph
    assert (target / ".obsidian/workspace.json").read_text() == edited_workspace
    assert (target / ".obsidian/app.json").read_text() == '{"readableLineLength":false}'
    assert "Boreal" not in (target / CANVAS_PATH).read_text()
    assert not (target / "Projetos" / "Boreal").exists()
    assert (target / "Inicio.md").read_text() == files_a["Inicio.md"]
    assert original_graph != (target / GRAPH_PATH).read_bytes()

    fresh = tmp_path / "Novo"
    refresh_export(fresh, files_ab)
    refresh_export(fresh, files_a)
    updated = json.loads((fresh / GRAPH_PATH).read_text())
    queries = [group["query"] for group in updated["colorGroups"]]
    assert path_query("Projetos/Aurora") in queries
    assert path_query("Projetos/Boreal") not in queries
    assert "Boreal" not in (fresh / GUIDE_PATH).read_text()
    assert (fresh / CANVAS_PATH).read_text() == files_a[CANVAS_PATH]


def test_shared_page_does_not_become_a_cross_project_bridge(monkeypatch):
    content = (
        "---\n"
        "schema_version: 1\n"
        "page_id: rag-shared\n"
        "project_id: shared\n"
        "type: concept\n"
        "sources: []\n"
        "tags: [RAG]\n"
        "aliases: []\n"
        "reviewed: true\n"
        "created: '2026-11-02'\n"
        "updated: '2026-11-02'\n"
        "---\n"
        "# Nota compartilhada sobre RAG\n\n"
        "Texto único.\n"
    )
    shared = {
        "path": "shared/wiki/concepts/shared--rag.md",
        "content": content,
        "sha256": digest(content),
    }
    model, files = exported(
        monkeypatch,
        [
            snapshot("alpha", "Um", [shared, page("alpha", "a", "Diário")]),
            snapshot(
                "beta",
                "Dois",
                [copy.deepcopy(shared), page("beta", "b", "Outro diário")],
            ),
        ],
    )
    assert connection_suggestions(model)["kept"] == []
    assert "Possível — RAG" not in files[CANVAS_PATH]
    assert "Nota compartilhada sobre RAG" in "".join(
        text for path, text in files.items() if path.startswith("Compartilhado/")
    )
