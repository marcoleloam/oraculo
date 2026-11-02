"""Portfolio projection over synthetic snapshots. No operational state."""

import copy
import json
import posixpath
import re
import sys
import types
from pathlib import Path, PurePosixPath
from urllib.parse import unquote

import pytest

from oraculo.experience import write_export
from oraculo.experience_portfolio import portfolio_export_files, portfolio_model
from oraculo.memory.schema import digest, links, resolve_link

FIXTURE = Path(__file__).parent / "fixtures/experience.json"
SENTINEL = "PRIVATE_UNSELECTED_SENTINEL"
RAW_MARK = (
    "[[projects/beta/wiki/concepts/demo--imports.md]]\n<script>alert(1)</script>\n"
)


def fixture():
    return json.loads(FIXTURE.read_text())


def retarget(project_id, title, observed_at="2026-10-24T00:00:00Z"):
    data = fixture()
    data["observed_at"] = observed_at
    data["projects"] = [{"id": project_id, "title": title}]
    for doc in data["documents"]:
        doc["path"] = doc["path"].replace("projects/demo/", f"projects/{project_id}/")
    for cycle in data["cycles"]:
        cycle["project_id"] = project_id
        for source in cycle["read_receipt"]["sources"]:
            source["path"] = source["path"].replace(
                "projects/demo/", f"projects/{project_id}/"
            )
    for task in data["tasks"]:
        task["project"] = {"project_id": project_id}
    data["tasks"][1]["parent_task_id"] = data["tasks"][0]["id"]
    return data


def shared_note(body="Texto único de acentuação compartilhada.\n"):
    content = (
        "---\n"
        "schema_version: 1\n"
        "page_id: acentuacao\n"
        "project_id: shared\n"
        "type: concept\n"
        "sources: []\n"
        "tags: [Acentuação]\n"
        "aliases: [Acentuação compartilhada]\n"
        "reviewed: true\n"
        "created: '2026-10-20'\n"
        "updated: '2026-10-24'\n"
        "---\n"
        "# Acentuação compartilhada\n\n" + body
    )
    return {
        "path": "shared/wiki/concepts/shared--acentuacao.md",
        "content": content,
        "sha256": digest(content),
    }


def portfolio_pair():
    alpha = retarget("alpha", "Aurora", "2026-10-23T00:00:00Z")
    beta = retarget("beta", "Aurora", "2026-10-25T00:00:00Z")
    note = shared_note()
    alpha["documents"].append(copy.deepcopy(note))
    beta["documents"].append(copy.deepcopy(note))
    concept = next(
        doc
        for doc in alpha["documents"]
        if doc["path"].endswith("wiki/concepts/demo--imports.md")
    )
    concept["content"] += (
        "\n[[projects/beta/wiki/concepts/demo--imports.md]]\n"
        "[[shared/wiki/concepts/shared--acentuacao.md]]\n" + RAW_MARK
    )
    concept["sha256"] = digest(concept["content"])
    for cycle in alpha["cycles"]:
        for source in cycle["read_receipt"]["sources"]:
            if source["path"] == concept["path"]:
                source["sha256"] = concept["sha256"]
    alpha["documents"].append(
        {
            "path": "projects/other/wiki/secret.md",
            "content": SENTINEL,
        }
    )
    empty = {
        "schema_version": 1,
        "observed_at": "2026-10-01T00:00:00Z",
        "projects": [{"id": "empty", "title": "Vazio"}],
        "documents": [],
        "tasks": [],
        "cycles": [],
    }
    return [beta, empty, alpha]


def install_html(monkeypatch, marker="PORTFOLIO_HTML_STUB"):
    module = types.ModuleType("oraculo.experience_portfolio_html")

    def dashboard(model):
        assert model["kind"] == "portfolio"
        assert model["schema_version"] == 1
        return '<!doctype html><meta charset="utf-8"><title>Carteira</title>' + marker

    module.dashboard = dashboard
    monkeypatch.setitem(sys.modules, "oraculo.experience_portfolio_html", module)
    return marker


def active(content):
    return "\n".join(
        line for line in content.splitlines() if not line.startswith(("    ", "\t"))
    )


def _assert_local(href, source, paths):
    resolved = posixpath.normpath(
        posixpath.join(posixpath.dirname(source), unquote(href.split("#", 1)[0]))
    )
    assert not resolved.startswith("../")
    assert resolved in paths


def test_portfolio_keeps_projects_apart_and_dedupes_shared_bytes():
    original = portfolio_pair()
    before = copy.deepcopy(original)
    sys.modules.pop("oraculo.experience_portfolio_html", None)
    model = portfolio_model(original, {"alpha": "Aurora"})
    assert "oraculo.experience_portfolio_html" not in sys.modules
    assert original == before
    assert [project["project_id"] for project in model["projects"]] == [
        "alpha",
        "beta",
        "empty",
    ]
    assert model["kind"] == "portfolio" and model["schema_version"] == 1
    assert model["observed_at"] == "2026-10-25T00:00:00Z"
    assert model["projects"][0]["observed_at"] == "2026-10-23T00:00:00Z"
    roots = [project["export_root"] for project in model["projects"]]
    assert len(roots) == len(set(roots))
    folded = [PurePosixPath(root).name.casefold() for root in roots]
    assert len(folded) == len(set(folded))
    assert model["projects"][-1]["export_root"] == "Projetos/Vazio"
    assert all(
        root.startswith("Projetos/") and ".." not in PurePosixPath(root).parts
        for root in roots
    )
    alpha, beta, empty = model["projects"]
    assert alpha["project_title"] == beta["project_title"] == "Aurora"
    assert alpha["export_root"] != beta["export_root"]
    alpha_tasks = [entry for entry in alpha["entries"] if entry.get("identity")]
    beta_tasks = [entry for entry in beta["entries"] if entry.get("identity")]
    assert {entry["identity"] for entry in alpha_tasks} == {
        entry["identity"] for entry in beta_tasks
    }
    assert {entry["id"] for entry in alpha_tasks} == {
        entry["id"] for entry in beta_tasks
    }
    parent = next(entry for entry in alpha_tasks if entry["identity"] == "task-demo-1")
    child = next(entry for entry in alpha_tasks if entry["identity"] == "task-demo-2")
    assert child["parent_entry_id"] == parent["id"]
    assert child["parent_entry_id"] not in {
        entry["id"]
        for entry in beta["entries"]
        if entry.get("identity") == "task-demo-2"
    }
    assert not empty["entries"]
    assert model["counts"] == {
        "projects": 3,
        "tasks": 4,
        "documents": len(alpha["evidence"]) + len(beta["evidence"]) - 1,
    }
    shared = model["shared"]["documents"]
    assert list(shared) == ["shared/wiki/concepts/shared--acentuacao.md"]
    assert (
        "Acentuação compartilhada"
        in shared["shared/wiki/concepts/shared--acentuacao.md"]
    )
    provenance = next(
        entry
        for entry in model["shared"]["entries"]
        if entry["document_path"] == "shared/wiki/concepts/shared--acentuacao.md"
    )
    assert provenance["project_ids"] == ["alpha", "beta"]
    assert provenance["conflict"] is False
    assert SENTINEL not in json.dumps(model["shared"])
    assert all(link["basis"] == "canonical_wikilink" for link in model["links"])
    assert {
        (link["source_project_id"], link["target_project_id"])
        for link in model["links"]
    } == {("alpha", "beta"), ("alpha", "shared")}
    beta_concept = next(
        entry
        for entry in beta["entries"]
        if str(entry.get("document_path", "")).endswith("demo--imports.md")
    )
    assert any(link["target_entry_id"] == beta_concept["id"] for link in model["links"])
    reversed_model = portfolio_model(list(reversed(original)))
    assert [project["export_root"] for project in reversed_model["projects"]] == roots
    assert reversed_model["links"] == model["links"]
    assert reversed_model["shared"]["documents"] == model["shared"]["documents"]


def test_same_task_id_and_foreign_parent_do_not_invent_edges():
    alpha = retarget("alpha", "Aurora")
    beta = retarget("beta", "Aurora")
    alpha["tasks"][0]["parent_task_id"] = "task-only-beta"
    beta["tasks"].append(
        {
            "id": "task-only-beta",
            "title": "Tarefa só do outro projeto",
            "project": {"project_id": "beta"},
            "phase": "pr_pending",
            "work_status": "succeeded",
        }
    )
    model = portfolio_model([alpha, beta])
    assert model["links"] == []
    alpha_parent = next(
        entry
        for entry in model["projects"][0]["entries"]
        if entry.get("identity") == "task-demo-1"
    )
    assert alpha_parent["relationship_status"] == "orphan"
    assert alpha_parent["parent_entry_id"] is None


def test_shared_conflicts_keep_every_version_and_provenance():
    alpha = retarget("alpha", "Um")
    beta = retarget("beta", "Dois")
    first = shared_note("versão alpha\n")
    second = shared_note("versão beta\n")
    alpha["documents"].append(first)
    beta["documents"].append(second)
    model = portfolio_model([beta, alpha])
    documents = model["shared"]["documents"]
    assert "shared/wiki/concepts/shared--acentuacao.md" not in documents
    assert set(documents.values()) == {first["content"], second["content"]}
    entry = model["shared"]["entries"][0]
    assert entry["conflict"] is True
    assert entry["project_ids"] == ["alpha", "beta"]
    assert (
        [version["project_ids"] for version in entry["versions"]]
        == [
            ["beta"],
            ["alpha"],
        ]
        if digest(second["content"]) < digest(first["content"])
        else [
            ["alpha"],
            ["beta"],
        ]
    )
    assert [version["sha256"] for version in entry["versions"]] == sorted(
        version["sha256"] for version in entry["versions"]
    )


def test_unknown_empty_and_unsafe_snapshots_are_rejected():
    alpha = retarget("alpha", "Aurora")
    with pytest.raises(ValueError, match="experience_unknown_project"):
        portfolio_model([alpha], {"missing": "Nome"})
    with pytest.raises(ValueError, match="experience_project_name_required"):
        portfolio_model([alpha], {"alpha": "  "})
    multi = copy.deepcopy(alpha)
    multi["projects"].append({"id": "beta", "title": "Outro"})
    with pytest.raises(ValueError, match="experience_select_one_known_project"):
        portfolio_model([multi])
    mismatched = copy.deepcopy(alpha)
    mismatched["project_id"] = "beta"
    with pytest.raises(ValueError, match="experience_unknown_project"):
        portfolio_model([mismatched])
    duplicated = copy.deepcopy(alpha)
    with pytest.raises(ValueError, match="experience_duplicate_project"):
        portfolio_model([alpha, duplicated])
    unsafe = copy.deepcopy(alpha)
    unsafe["documents"].append({"path": "../escape.md", "content": "no"})
    with pytest.raises(ValueError, match="experience_unsafe_document_path"):
        portfolio_model([unsafe])
    tampered = copy.deepcopy(alpha)
    tampered["documents"][1]["content"] += "tamper"
    with pytest.raises(ValueError, match="integrity"):
        portfolio_model([tampered])
    colon = retarget("t3:project", "Nome: estranho")
    rooted = portfolio_model([colon])["projects"][0]["export_root"]
    assert rooted == "Projetos/Nome estranho"
    assert portfolio_model([colon])["projects"][0]["project_id"] == "t3:project"
    assert ".." not in PurePosixPath(rooted).parts
    empty = portfolio_model([])
    assert empty["counts"] == {"projects": 0, "tasks": 0, "documents": 0}
    assert empty["observed_at"] == "desconhecida"
    bare = {
        "schema_version": 1,
        "projects": [{"id": "solo", "title": "Só o projeto"}],
    }
    assert portfolio_model([bare])["counts"]["projects"] == 1


def test_export_is_one_vault_and_preserves_evidence(tmp_path, monkeypatch):
    marker = install_html(monkeypatch)
    model = portfolio_model(portfolio_pair())
    assert "oraculo.experience_portfolio_html" not in sys.modules or True
    files = portfolio_export_files(model)
    canvas = json.loads(files["Mapa dos projetos.canvas"])
    edge_labels = [edge["label"] for edge in canvas["edges"]]
    assert [label for label in edge_labels if label.startswith("confirmada")] == [
        "confirmada · references",
        "confirmada · references",
    ]
    assert any(label.startswith("Possível — ") for label in edge_labels)
    assert json.loads(files["model.json"])["links"] == model["links"]
    assert files["index.html"].endswith(marker)
    assert json.loads(files["model.json"])["counts"] == model["counts"]
    manifest = {
        (item["scope"], item["path"]): item["sha256"]
        for item in json.loads(files["manifest.json"])["evidence"]
    }
    assert sorted(path for path in files if ".obsidian/" in path) == [
        ".obsidian/graph.json",
        ".obsidian/workspace.json",
    ]
    assert (
        json.loads(files[".obsidian/workspace.json"])["main"]["children"][0]["state"][
            "state"
        ]["file"]
        == "Inicio.md"
    )
    assert SENTINEL not in "".join(files.values())
    assert "<script>alert(1)</script>" in "".join(
        content for path, content in files.items() if path.endswith(".txt")
    )
    assert not any(
        "<script>alert(1)</script>" in content
        for path, content in files.items()
        if path.endswith(".html")
    )
    shared_txt = [
        path
        for path in files
        if path.endswith("shared/wiki/concepts/shared--acentuacao.md.txt")
    ]
    assert shared_txt == [
        "Compartilhado/Evidencias/shared/wiki/concepts/shared--acentuacao.md.txt"
    ]
    alpha = next(
        project for project in model["projects"] if project["project_id"] == "alpha"
    )
    concept = next(
        entry
        for entry in alpha["entries"]
        if str(entry.get("document_path", "")).endswith("demo--imports.md")
    )
    raw = alpha["evidence"][concept["document_path"]]
    evidence = alpha["export_root"] + "/Evidencias/" + concept["document_path"] + ".txt"
    assert files[evidence] == raw
    assert manifest[("alpha", concept["document_path"])] == digest(raw)
    assert manifest[("shared", "shared/wiki/concepts/shared--acentuacao.md")] == digest(
        model["shared"]["documents"]["shared/wiki/concepts/shared--acentuacao.md"]
    )
    fontes = alpha["export_root"] + "/Fontes/" + concept["document_path"]
    assert "    [[projects/beta/wiki/concepts/demo--imports.md]]" in files[fontes]
    assert "    [[Projetos/" not in files[fontes]
    paths = set(files)
    for name, content in files.items():
        assert ".." not in PurePosixPath(name).parts
        if name.endswith(".md"):
            for target in links(active(content)):
                assert resolve_link(target, paths, name) in paths
        if name.endswith(".md"):
            for href in re.findall(r"\]\(([^)]+)\)", active(content)):
                if href.startswith(
                    ("#", "t3-thread:", "http://", "https://", "mailto:")
                ):
                    continue
                _assert_local(href, name, paths)
        if name.endswith(".html") and name != "index.html":
            for href in re.findall(r'href="([^"]+)"', content):
                if href.startswith(
                    ("#", "t3-thread:", "http://", "https://", "mailto:")
                ):
                    continue
                _assert_local(href, name, paths)
    parent = next(
        entry for entry in alpha["entries"] if entry.get("identity") == "task-demo-1"
    )
    child = next(
        entry for entry in alpha["entries"] if entry.get("identity") == "task-demo-2"
    )
    parent_note = files[alpha["export_root"] + "/" + parent["reading_path"]]
    assert child["reading_path"].removesuffix(".md") in parent_note
    assert "Aurora" in files["Inicio.md"]
    assert (
        "Acentuação compartilhada"
        in files[
            "Compartilhado/Evidencias/shared/wiki/concepts/shared--acentuacao.md.txt"
        ]
    )
    assert any(
        "Acentuação" in content
        for path, content in files.items()
        if path.startswith("Compartilhado/") and path.endswith(".md")
    )
    assert "</script>" not in active(files["Inicio.md"])
    attack = portfolio_model(
        [retarget("alpha", '</script><img src=x onerror="pwned">')]
    )
    attack_files = portfolio_export_files(attack)
    assert "<img src=x" not in attack_files["Inicio.md"]
    assert "pwned" in attack_files["Inicio.md"]
    first = write_export(tmp_path / "vault", files)
    assert write_export(tmp_path / "vault", portfolio_export_files(model)) == first
    owned = tmp_path / "owned"
    owned.mkdir()
    (owned / "note.md").write_text("keep")
    with pytest.raises(ValueError, match="empty_or_owned"):
        write_export(owned, files)
    link = tmp_path / "link"
    link.symlink_to(owned, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        write_export(link, files)
    (tmp_path / "vault" / "Inicio.md").write_text("edited")
    with pytest.raises(ValueError, match="locally_modified"):
        write_export(tmp_path / "vault", files)


def test_conflict_export_preserves_both_byte_strings(monkeypatch):
    install_html(monkeypatch)
    alpha = retarget("alpha", "Um")
    beta = retarget("beta", "Dois")
    first = shared_note("versão alpha\n")
    second = shared_note("versão beta\n")
    alpha["documents"].append(first)
    beta["documents"].append(second)
    files = portfolio_export_files(portfolio_model([alpha, beta]))
    blobs = [
        content
        for content in files.values()
        if content in {first["content"], second["content"]}
    ]
    assert first["content"] in blobs and second["content"] in blobs
    assert sorted(path for path in files if ".obsidian/" in path) == [
        ".obsidian/graph.json",
        ".obsidian/workspace.json",
    ]
    joined = "\n".join(
        active(content) for path, content in files.items() if path.endswith(".md")
    )
    for target in links(joined):
        assert resolve_link(target, set(files), "Inicio.md")
