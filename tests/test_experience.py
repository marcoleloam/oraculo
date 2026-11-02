"""Synthetic end-to-end presentation; no operational state or customer data."""

import copy
import json
import re
from pathlib import Path
from urllib.parse import unquote

import pytest

from oraculo.cli import main, make_envelope, request
from oraculo.experience import export_files, project_model, write_export
from oraculo.memory.schema import links, resolve_link

FIXTURE = Path(__file__).parent / "fixtures/experience.json"


def fixture():
    return json.loads(FIXTURE.read_text())


def test_shared_model_links_identity_and_idempotent_rebuild(tmp_path):
    source = fixture()
    original = copy.deepcopy(source)
    model = project_model(source, "demo")
    assert source == original
    files = export_files(model)
    assert "Jardim de ideias" in files["Inicio.md"]
    assert "Reconciliar importação" in files["index.html"]
    assert "Assuntos/Importação.md" in files
    assert not any(re.fullmatch(r"Assuntos/[0-9a-f]{64}\.md", name) for name in files)
    paths = set(files)
    for name, content in files.items():
        if name.endswith(".md"):
            # Inert, indented source text isn't an active wikilink.
            active = "\n".join(
                line for line in content.splitlines() if not line.startswith("    ")
            )
            for target in links(active):
                assert resolve_link(target, paths, name) in paths
    for href in re.findall(r'href="([^"]+)"', files["index.html"]):
        if href.startswith("#"):
            assert f'id="{href[1:]}"' in files["index.html"]
        elif not href.startswith("t3-thread:"):
            assert unquote(href) in paths
    first = write_export(tmp_path / "export", files)
    assert (
        write_export(tmp_path / "export", export_files(project_model(source, "demo")))
        == first
    )
    assert not (tmp_path / "export" / "projects").exists()
    source["tasks"][0]["title"] = "Título atualizado"
    renamed = project_model(source, "demo")
    assert [e["id"] for e in model["entries"]] == [e["id"] for e in renamed["entries"]]


def test_memory_metrics_do_not_claim_read_or_causal_benefit():
    model = project_model(fixture(), "demo")
    tasks = [e for e in model["entries"] if e["kind"] == "task"]
    metrics = tasks[0]["metrics"]
    assert metrics["memory_found"] == "found"
    assert len(metrics["consulted_sources"]) == 1
    assert metrics["agent_read"] == metrics["benefit_demonstrated"] == "unknown"
    assert metrics["application_declared"]
    assert metrics["checks_passed"] == 1 and metrics["checks_failed"] == 0
    assert tasks[1]["metrics"]["checks_failed"] == 1
    errors = [e for e in model["entries"] if e["kind"] == "error"]
    assert [e["occurrences"] for e in errors] == [2, 2]
    source = fixture()
    source["cycles"] = []
    source["tasks"][0]["completion_evidence"] = False
    metric = next(
        e["metrics"]
        for e in project_model(source, "demo")["entries"]
        if e["kind"] == "task"
    )
    assert metric["memory_found"] == metric["result_verified"] == "unknown"
    assert metric["application_declared"] == []


def test_consulted_sources_need_matching_hash():
    source = fixture()
    source["cycles"][0]["read_receipt"]["sources"][0]["sha256"] = "0" * 64
    metric = next(
        e["metrics"]
        for e in project_model(source, "demo")["entries"]
        if e["kind"] == "task"
    )
    assert metric["consulted_sources"] == []


def test_unselected_project_content_never_exported():
    source = fixture()
    source["projects"].append({"id": "other", "title": "PRIVATE_SENTINEL"})
    source["documents"].append(
        {
            "path": "projects/other/wiki/concepts/secret.md",
            "content": "PRIVATE_SENTINEL",
        }
    )
    source["tasks"].append(
        {"id": "other", "project": {"project_id": "other"}, "title": "PRIVATE_SENTINEL"}
    )
    source["cycles"].append(
        {"id": "other", "project_id": "other", "query": "PRIVATE_SENTINEL"}
    )
    files = export_files(project_model(source, "demo"))
    assert "PRIVATE_SENTINEL" not in "".join(files.values())
    assert not any("other" in p for p in files)


def test_untrusted_text_does_not_become_html_or_active_links():
    source = fixture()
    title = '</pre><script>window.evil=1</script>[[outside|go]] " <img src=x>'
    source["tasks"][0]["title"] = title
    concept = source["documents"][0]
    concept["content"] += (
        "\n<script>evil()</script>\n![](https://example.invalid/track)\n[[projects/other/secret]]"
    )
    files = export_files(project_model(source, "demo"))
    assert "<script>window.evil" not in files["index.html"]
    assert "<img src=x>" not in files["index.html"]
    navigation = "\n".join(
        files[e["reading_path"]] for e in project_model(source, "demo")["entries"]
    )
    active = "\n".join(
        line for line in navigation.splitlines() if not line.startswith("    ")
    )
    assert "<script>" not in active and "[[outside|go]]" not in active
    assert all(".." not in Path(p).parts for p in files)
    assert (
        "\n    ![](https://example.invalid/track)" in files["Fontes/" + concept["path"]]
    )
    assert "javascript:" not in files["index.html"]


@pytest.mark.parametrize(
    "path",
    [
        "../escape.md",
        "/absolute.md",
        "projects/demo/../../escape.md",
        "projects/demo/wiki/a|x.md",
        "projects/demo/wiki/a\\b.md",
    ],
)
def test_reject_unsafe_document_paths(path):
    source = fixture()
    source["documents"].append({"path": path, "content": "untrusted"})
    with pytest.raises(ValueError):
        project_model(source, "demo")


def test_missing_and_ambiguous_links_are_explicit():
    source = fixture()
    source["documents"][0]["content"] += "\n[[missing]]\n[[same]]"
    source["documents"] += [
        dict(path=f"projects/demo/wiki/{kind}/same.md", content="# Same")
        for kind in ["sources", "entities"]
    ]
    entry = next(
        e
        for e in project_model(source, "demo")["entries"]
        if e.get("document_path") == source["documents"][0]["path"]
    )
    assert entry["unavailable_sources"] == 2
    assert entry["sources"] == [source["documents"][0]["path"]]


@pytest.mark.parametrize("field", ["documents", "tasks", "cycles"])
def test_reject_duplicate_identity(field):
    source = fixture()
    source[field].append(source[field][0])
    with pytest.raises(ValueError, match="duplicate"):
        project_model(source, "demo")


def test_tampered_raw_and_hashes_rejected():
    source = fixture()
    source["documents"][1]["content"] += "tamper"
    with pytest.raises(ValueError, match="integrity"):
        project_model(source, "demo")


def test_export_preserves_user_edits_and_refuses_symlinks(tmp_path):
    files = export_files(project_model(fixture(), "demo"))
    target = tmp_path / "export"
    write_export(target, files)
    (target / "Inicio.md").write_text("my note")
    with pytest.raises(ValueError, match="locally_modified"):
        write_export(target, files)
    assert (target / "Inicio.md").read_text() == "my note"
    other = tmp_path / "other"
    other.mkdir()
    (other / "My note.md").write_text("keep")
    with pytest.raises(ValueError, match="empty_or_owned"):
        write_export(other, files)
    link = tmp_path / "link"
    link.symlink_to(other, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        write_export(link, files)
    (target / "Inicio.md").unlink()
    (target / "Inicio.md").symlink_to(other / "My note.md")
    with pytest.raises(ValueError, match="symlink"):
        write_export(target, files)
    assert (other / "My note.md").read_text() == "keep"


def test_cli_offline_export_and_json_status_without_config(tmp_path, capsys):
    missing = tmp_path / "no-config.toml"
    assert (
        main(
            [
                "--config",
                str(missing),
                "experience",
                "export",
                "--file",
                str(FIXTURE),
                "--project",
                "demo",
                "--output",
                str(tmp_path / "export"),
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["state"] == "exported"
    status = fixture()["tasks"][0]
    path = tmp_path / "status.json"
    path.write_text(json.dumps(status))
    assert main(["experience", "status", "--file", str(path)]) == 0
    assert "Reconciliar importação" in capsys.readouterr().out
    assert main(["experience", "status", "--file", str(path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == status
    assert not missing.exists()


async def test_service_snapshot_socket_scope_and_no_vault_changes(running_service):
    service = running_service
    service.publisher.snapshot("alpha", "# Safe synthetic evidence")
    service.publisher.initialize_project("other")
    service.publisher.snapshot("other", "# PRIVATE_SENTINEL")
    before = service.publisher.git.head()
    result = await request(
        service.config, make_envelope("experience.snapshot", "alpha", {})
    )
    assert result["ok"], result
    value = result["data"]
    assert "PRIVATE_SENTINEL" not in json.dumps(value)
    assert all(
        d["path"].startswith(("projects/alpha/", "shared/")) for d in value["documents"]
    )
    assert project_model(value, "alpha")["project_title"] == "alpha"
    assert service.publisher.git.head() == before
    assert service.managed_service is None  # no lazy managed mutation on read
    denied = await request(
        service.config, make_envelope("experience.snapshot", "other", {})
    )
    assert not denied["ok"]


async def test_service_memory_report_to_both_views_with_evidence(
    running_service, tmp_path
):
    from oraculo.task_store import TaskStore

    service = running_service
    service.publisher.snapshot("alpha", "# Regra sintética de preservação")
    cycle = service.memory_cycles.begin(
        "alpha", "synthetic-task", "preservação", {"thread_id": "demo", "run_id": "run"}
    )
    report = {
        "title": "Importação sintética",
        "summary": "Notas preservadas na fixture.",
        "topics": ["Importação"],
        "decisions": ["Preservar identidades."],
        "lessons": ["Conferir origem."],
        "errors": ["source_missing"],
        "memory_application": ["Aplicação declarada na fixture."],
        "checks": [{"command": "synthetic check", "status": "passed"}],
        "secret_unselected_field": "PRIVATE_CONTEXT",
    }
    service.memory_cycles.finish(cycle["id"], "succeeded", report)
    saved = service.memory_cycles.publish_one(cycle["id"])
    assert saved["write_state"] == "saved"
    store = TaskStore(service.config.state_dir / "managed-tasks.sqlite3")
    task = store.create(
        {"thread_id": "origin", "run_id": "run"},
        "synthetic",
        {
            "title": report["title"],
            "wave": "Onda 3",
            "parent_task_id": "synthetic-parent",
            "tags": ["Leitura"],
            "private": "PRIVATE_CONTEXT",
        },
    )
    store.update(
        task["id"],
        "pr_pending",
        {"fixture": "terminal"},
        project={"project_id": "alpha"},
        memory=saved,
        completion_evidence={"fixture": True},
        work_status="succeeded",
        context={"private": "PRIVATE_CONTEXT"},
    )
    store.close()
    response = await request(
        service.config, make_envelope("experience.snapshot", "alpha", {})
    )
    assert response["ok"], response
    assert "PRIVATE_CONTEXT" not in json.dumps(response["data"])
    model = project_model(response["data"], "alpha")
    task_entry = next(e for e in model["entries"] if e["kind"] == "task")
    assert task_entry["title"] == report["title"]
    assert task_entry["wave"] == "Onda 3"
    assert response["data"]["tasks"][0]["parent_task_id"] == "synthetic-parent"
    assert task_entry["relationship_status"] == "orphan"
    assert task_entry["metrics"]["application_declared"] == report["memory_application"]
    assert task_entry["metrics"]["checks_passed"] == 1
    assert task_entry["metrics"]["benefit_demonstrated"] == "unknown"
    assert all(
        any(e["kind"] == kind for e in model["entries"])
        for kind in ["decision", "lesson", "error"]
    )
    files = export_files(model)
    raw_source = task_entry["sources"][0]
    assert (
        json.loads(files["Evidencias/" + raw_source + ".txt"])["evidence"][
            "memory_application"
        ]
        == report["memory_application"]
    )
    assert any(
        "Importação sintética" in name for name in files if name.startswith("Projetos/")
    )
    write_export(tmp_path / "export", files)


async def test_large_snapshot_download_uses_scoped_immutable_chunks(
    running_service, monkeypatch
):
    import oraculo.experience_service as adapter
    from oraculo.cli import experience_snapshot

    service = running_service
    service.publisher.snapshot(
        "alpha", "# Evidência sintética\n" + "Aprendizado ágil.\n" * 1200
    )
    monkeypatch.setattr(adapter, "MAX_ENVELOPE_BYTES", 8192)
    result = await request(
        service.config, make_envelope("experience.snapshot", "alpha", {})
    )
    assert result["ok"] and result["data"]["artifact"]["bytes"] > 8192
    reference = result["data"]["artifact"]["artifact"]
    first = await request(
        service.config,
        make_envelope(
            "experience.snapshot", "alpha", {"artifact": reference, "offset": 0}
        ),
    )
    assert first["ok"] and not first["data"]["eof"]
    snapshot = await experience_snapshot(service.config, "alpha")
    assert any("Aprendizado ágil." in d["content"] for d in snapshot["documents"])
    assert project_model(snapshot, "alpha")["project_title"] == "alpha"
    # The server rejects cross-project requests before reading the private cache.
    denied = await request(
        service.config,
        make_envelope("experience.snapshot", "other", {"artifact": reference}),
    )
    assert not denied["ok"]


async def test_chunk_integrity_failure_is_not_a_successful_export(
    running_service, monkeypatch
):
    import oraculo.cli as cli
    import oraculo.experience_service as adapter

    service = running_service
    service.publisher.snapshot("alpha", "# Synthetic\n" + "source\n" * 1500)
    monkeypatch.setattr(adapter, "MAX_ENVELOPE_BYTES", 8192)
    original = cli.request

    async def corrupt(config, envelope):
        result = await original(config, envelope)
        if envelope["payload"].get("artifact") and result.get("ok"):
            content = result["data"]["content"]
            result["data"]["content"] = "X" + content[1:]
        return result

    monkeypatch.setattr(cli, "request", corrupt)
    with pytest.raises(ValueError, match="integrity"):
        await cli.experience_snapshot(service.config, "alpha")


async def test_snapshot_includes_early_blocked_progress_and_plan_state(running_service):
    from oraculo.task_store import TaskStore

    service = running_service
    store = TaskStore(service.config.state_dir / "managed-tasks.sqlite3")
    early = store.create(
        dict(thread_id="demo-origin", run_id="run", project_id="alpha"),
        "early",
        {"title": "Demo antes da resolução"},
    )
    store.update(
        early["id"],
        "blocked",
        {"reason": "workspace_git_metadata_unavailable"},
        blocked_reason="workspace_git_metadata_unavailable",
    )
    plan = store.create(
        dict(thread_id="demo-origin", run_id="run", project_id="alpha"),
        "plan",
        {"title": "Plano demo"},
    )
    store.update(
        plan["id"],
        "awaiting_approval",
        {"progress": "Plano aguardando origem"},
        project={"project_id": "alpha"},
        deliverable_kind="plan",
        approval_state="pending",
        work_status="succeeded",
        completion_evidence={"demo": True},
        observed={"status": "completed"},
    )
    value = (
        await request(service.config, make_envelope("experience.snapshot", "alpha", {}))
    )["data"]
    rows = {t["id"]: t for t in value["tasks"]}
    assert early["id"] in rows and rows[early["id"]]["project"]["project_id"] == "alpha"
    assert rows[plan["id"]]["deliverable_kind"] == "plan"
    model = project_model(value, "alpha")
    rendered = export_files(model)["index.html"]
    assert "Plano aguardando origem" in rendered
    assert "Planejamento entregue; aprovação ainda pendente." in rendered
    assert "Conversa de origem" in rendered
    assert "Bloqueada / parada" in rendered
    store.close()


def test_cli_panel_uses_fixed_project_and_loopback_entrypoint(
    monkeypatch, service_config
):
    import oraculo.task_panel as panel

    config, path = service_config
    calls = []
    monkeypatch.setattr(
        panel,
        "serve",
        lambda cfg, project, port: calls.append((cfg.socket_path, project, port)),
    )
    assert (
        main(
            [
                "--config",
                str(path),
                "experience",
                "serve",
                "--project",
                "alpha",
                "--port",
                "8767",
            ]
        )
        == 0
    )
    assert calls == [(config.socket_path, "alpha", 8767)]


HIERARCHY = Path(__file__).parent / "fixtures/experience_hierarchy.json"


def hierarchy_fixture():
    return json.loads(HIERARCHY.read_text())


def test_explicit_hierarchy_duplicate_names_and_complete_library():
    from oraculo.memory.schema import digest

    source = hierarchy_fixture()
    model = project_model(source, "demo-hierarchy")
    tasks = {e["identity"]: e for e in model["entries"] if e.get("identity")}
    assert tasks["task-synthetic-0"]["wave"] == "Onda 1"
    assert tasks["task-synthetic-4"]["wave"] == "Onda 2"
    assert tasks["task-synthetic-5"]["wave"] == "Onda 4"  # marker in middle
    assert tasks["task-synthetic-6"]["wave"] == "Sem onda informada"
    assert tasks["task-synthetic-6"]["kind"] == "task"  # errors are not fixes
    assert tasks["task-synthetic-3"]["kind"] == "fix"
    assert tasks["task-synthetic-7"]["kind"] == "fix"
    assert (
        tasks["task-synthetic-1"]["reading_path"]
        != tasks["task-synthetic-2"]["reading_path"]
    )
    assert "--" in tasks["task-synthetic-1"]["reading_path"]
    assert "--" in tasks["task-synthetic-4"]["reading_path"]
    files = export_files(model)
    docs = {e["document_path"]: e for e in model["entries"] if e.get("document_path")}
    assert set(docs) == {d["path"] for d in source["documents"]}
    for d in source["documents"]:
        entry = docs[d["path"]]
        assert files["Evidencias/" + d["path"] + ".txt"] == d["content"]
        assert entry["reading_path"] in files
        assert entry["reading_path"] in files["Biblioteca/Biblioteca.md"]
        assert digest(d["content"]) in files[entry["reading_path"]]
    isolated = next(
        e for e in docs.values() if e["title"] == "Pesquisa avulsa sobre acentuação"
    )
    assert isolated["kind"] == "raw"
    assert "Este material não está ligado" in files[isolated["reading_path"]]
    assert "FIM DO CONTEÚDO INTEGRAL" in files["index.html"]
    assert "Demonstração sintética" in files["Inicio.md"]
    assert "Demonstração sintética" in files["index.html"]
    assert (
        json.loads(files[".obsidian/workspace.json"])["main"]["children"][0]["state"][
            "state"
        ]["file"]
        == "Inicio.md"
    )


@pytest.mark.parametrize(
    "title,metadata,expected",
    [
        ("Projeto Exemplo > Onda 5 > Tarefa", {}, "Onda 5"),
        ("Exemplo — Onda 6 — Validar", {}, "Onda 6"),
        ("Onda 2a não é marcador", {}, "Sem onda informada"),
        ("ondamente 3", {}, "Sem onda informada"),
        ("Onda 8", {"wave": "Entrega Azul"}, "Entrega Azul"),
        ("Legado sem campo", {"topics": ["onda:4"]}, "Onda 4"),
    ],
)
def test_wave_metadata_and_unambiguous_title_markers(title, metadata, expected):
    source = fixture()
    source["tasks"][0].update(title=title, **metadata)
    entry = next(
        e
        for e in project_model(source, "demo")["entries"]
        if e.get("identity") == "task-demo-1"
    )
    assert entry["wave"] == expected


def test_legacy_memory_only_is_navigable_and_content_is_not_truncated():
    source = fixture()
    source["tasks"] = []
    source["cycles"] = []
    source["documents"][0]["content"] += "\n" + "ação\n" * 1500 + "FINAL INTEGRAL"
    model = project_model(source, "demo")
    assert model["waves"] == ["Sem onda informada"]
    assert len(model["entries"]) == len(source["documents"])
    files = export_files(model)
    assert "FINAL INTEGRAL" in files["index.html"]
    assert all(
        e["reading_path"] in files["Biblioteca/Biblioteca.md"] for e in model["entries"]
    )


def test_full_task_summary_decision_and_lesson_are_preserved():
    from oraculo.memory.schema import digest

    source = fixture()
    raw = source["documents"][1]
    report = json.loads(raw["content"])
    for key in ["summary", "decisions", "lessons"]:
        report["evidence"][key] = ["Conteúdo integral.\n" * 600 + "FINAL " + key]
    raw["content"] = json.dumps(report)
    raw["sha256"] = digest(raw["content"])
    raw["path"] = "projects/demo/raw/" + raw["sha256"] + ".md"
    model = project_model(source, "demo")
    entry = next(e for e in model["entries"] if e.get("identity") == "task-demo-1")
    files = export_files(model)
    for key in ["summary", "decisions", "lessons"]:
        assert "FINAL " + key in files[entry["reading_path"]]
        assert "FINAL " + key in files["index.html"]


def test_reading_filenames_are_macos_safe_and_collision_deterministic():
    import unicodedata

    source = fixture()
    originals = []
    for i, title in enumerate(
        ["A/B:C", "A B C", "Écho", "E\u0301CHO", ".", "../x", "💡" * 180]
    ):
        task = copy.deepcopy(source["tasks"][0])
        task.update(id="collision-" + str(i), title=title, wave="../Onda: X")
        originals.append(task)
    source["tasks"] = originals
    model = project_model(source, "demo")
    paths = [e["reading_path"] for e in model["entries"]]
    keys = [unicodedata.normalize("NFD", p).casefold() for p in paths]
    assert len(keys) == len(set(keys))
    for path in paths:
        assert not any(c in path for c in '\\:*?"<>|#[]^')
        assert ".." not in Path(path).parts
        assert all(len(p.encode()) < 255 for p in Path(path).parts)
    reverse = copy.deepcopy(source)
    reverse["tasks"].reverse()
    alternate = project_model(reverse, "demo")
    assert {e["id"]: e["reading_path"] for e in model["entries"]} == {
        e["id"]: e["reading_path"] for e in alternate["entries"]
    }


def test_reading_wikilinks_resolve_and_original_downloads_resolve():
    import posixpath

    model = project_model(hierarchy_fixture(), "demo-hierarchy")
    files = export_files(model)
    for name, content in files.items():
        if not name.endswith(".md"):
            continue
        active = "\n".join(
            line for line in content.splitlines() if not line.startswith("    ")
        )
        for target in links(active):
            assert resolve_link(target, set(files), name) in files
        for href in re.findall(r"\]\(([^)]+)\)", active):
            if not href.startswith("t3-thread:"):
                assert (
                    posixpath.normpath(
                        posixpath.join(posixpath.dirname(name), unquote(href))
                    )
                    in files
                )


def test_html_injection_in_all_labels_and_full_content_is_inert():
    source = hierarchy_fixture()
    attack = '</script><img src=x onerror="window.pwned=1">@@TITLE@@'
    source["projects"][0]["title"] = attack
    source["tasks"][0].update(title=attack, wave=attack, topics=[attack])
    source["documents"][-1]["content"] = (
        "# " + attack + "\n<script>window.pwned=2</script>\n[click](javascript:evil())"
    )
    files = export_files(project_model(source, "demo-hierarchy"))
    page = files["index.html"]
    assert "<img src=x" not in page
    assert "<script>window.pwned" not in page
    assert "@@TITLE@@" in page  # source text isn't re-interpolated
    assert 'href="javascript:' not in page
    assert 'src="http' not in page
    assert "fetch(" not in page


def test_hierarchy_export_cli_determinism_and_local_edit_protection(tmp_path, capsys):
    args = [
        "experience",
        "export",
        "--file",
        str(HIERARCHY),
        "--project",
        "demo-hierarchy",
        "--output",
        str(tmp_path / "export"),
    ]
    assert main(args) == 0
    first = json.loads(capsys.readouterr().out)
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out) == first
    workspace = tmp_path / "export/.obsidian/workspace.json"
    workspace.write_text("{}")
    with pytest.raises(ValueError, match="locally_modified"):
        write_export(
            tmp_path / "export",
            export_files(project_model(hierarchy_fixture(), "demo-hierarchy")),
        )
    assert workspace.read_text() == "{}"


@pytest.mark.parametrize(
    "title,kind",
    [
        ("Projeto Exemplo Onda 4 fix — F1", "fix"),
        ("Projeto Exemplo — fix Onda 4", "fix"),
        ("Projeto Exemplo / Fix: preservar notas", "fix"),
        ("Correção: preservar notas", "fix"),
        ("Investigar erro no importador", "task"),
        ("Relato de erro, sem fix implementado", "task"),
        ("Fixação do índice", "task"),
        ("Erro com prefixo hotfix não classificado", "task"),
    ],
)
def test_explicit_fix_marker_inside_title_and_document_keeps_library(title, kind):
    source = fixture()
    source["tasks"][0]["title"] = title
    source["documents"][0]["content"] = (
        "# " + title + "\n\nConteúdo de origem preservado."
    )
    model = project_model(source, "demo")
    task = next(e for e in model["entries"] if e.get("identity") == "task-demo-1")
    assert task["kind"] == kind
    doc = next(
        e
        for e in model["entries"]
        if e.get("document_path") == source["documents"][0]["path"]
    )
    files = export_files(model)
    assert doc["reading_path"] in files["Biblioteca/Biblioteca.md"]
    assert doc["document_path"] in model["evidence"]


def test_orphan_plain_raw_schema_and_json_fix_remain_in_complete_library():
    from oraculo.memory.schema import digest

    source = fixture()
    raw_text = "# Caderno avulso\n\nTexto integral sem vínculo com tarefa."
    structured = json.dumps(
        {
            "title": "Projeto Sintético — fix Onda 4",
            "summary": "Resumo humano preservado.",
            "unknown_field": {"detail": "CAMPO INTEGRAL DESCONHECIDO"},
        }
    )
    for content in [raw_text, structured]:
        source["documents"].append(
            {"path": "projects/demo/raw/" + digest(content) + ".md", "content": content}
        )
    source["documents"].append(
        {
            "path": "projects/demo/wiki/schema/structure.md",
            "content": "# Esquema legado\n\nContrato integral preservado.",
        }
    )
    model = project_model(source, "demo")
    files = export_files(model)
    entries = {
        e["document_path"]: e for e in model["entries"] if e.get("document_path")
    }
    for doc in source["documents"][-3:]:
        entry = entries[doc["path"]]
        assert entry["reading_path"] in files["Biblioteca/Biblioteca.md"]
        assert doc["content"] == files["Evidencias/" + doc["path"] + ".txt"]
    fix = entries[source["documents"][-2]["path"]]
    assert fix["kind"] == "fix"
    assert fix["reading_path"].startswith("Biblioteca/Fontes/")
    assert fix["text"].startswith("Resumo\n\nResumo humano")
    assert "CAMPO INTEGRAL DESCONHECIDO" in files[fix["reading_path"]]
    assert "CAMPO INTEGRAL DESCONHECIDO" in files["index.html"]


def task_entries(source):
    return {
        e["identity"]: e
        for e in project_model(source, "demo-hierarchy")["entries"]
        if e.get("identity")
    }


def test_explicit_subtasks_and_related_records_are_distinct():
    source = hierarchy_fixture()
    model = project_model(source, "demo-hierarchy")
    tasks = {e["identity"]: e for e in model["entries"] if e.get("identity")}
    parent = tasks["task-synthetic-0"]
    assert parent["subtask_ids"] == [
        tasks["task-synthetic-1"]["id"],
        tasks["task-synthetic-2"]["id"],
    ]
    assert tasks["task-synthetic-1"]["parent_entry_id"] == parent["id"]
    assert tasks["task-synthetic-1"]["ancestor_ids"] == [parent["id"]]
    assert not tasks["task-synthetic-3"]["subtask_ids"]
    records = [e for e in model["entries"] if e.get("task_id") == parent["identity"]]
    assert records and all(e["id"] not in parent["subtask_ids"] for e in records)
    assert all(e["related_task_id"] == parent["id"] for e in records)
    files = export_files(model)
    assert tasks["task-synthetic-1"]["reading_path"] in files[parent["reading_path"]]
    assert parent["reading_path"] in files[tasks["task-synthetic-1"]["reading_path"]]
    assert parent["reading_path"] in files[model["task_index_path"]]
    assert (
        "Sem subtarefas registradas."
        in files[tasks["task-synthetic-3"]["reading_path"]]
    )
    assert "/Ondas/" not in parent["reading_path"]
    assert "Sem onda informada" not in files[model["task_index_path"]]


@pytest.mark.parametrize(
    "relationship", ["orphan", "self_cycle", "cycle", "cross_project"]
)
def test_invalid_parent_relations_preserve_tasks_without_inventing_edges(relationship):
    source = hierarchy_fixture()
    a, b = source["tasks"][:2]
    a.pop("parent_task_id", None)
    b.pop("parent_task_id", None)
    if relationship == "orphan":
        a["parent_task_id"] = "missing-parent"
    elif relationship == "self_cycle":
        a["parent_task_id"] = a["id"]
    elif relationship == "cycle":
        a["parent_task_id"] = b["id"]
        b["parent_task_id"] = a["id"]
    else:
        source["tasks"].append(
            {
                "id": "foreign-parent",
                "title": "PRIVATE_PARENT_SENTINEL",
                "project": {"project_id": "other"},
                "child": {"childThreadId": "foreign-thread"},
            }
        )
        a["parent_task_id"] = "foreign-parent"
        b["origin"] = {"thread_id": "foreign-thread"}
    model = project_model(source, "demo-hierarchy")
    tasks = {e["identity"]: e for e in model["entries"] if e.get("identity")}
    assert tasks[a["id"]]["parent_entry_id"] is None
    assert tasks[a["id"]]["relationship_status"] == (
        "cycle" if "cycle" in relationship else "orphan"
    )
    assert tasks[b["id"]]["parent_entry_id"] is None
    files = export_files(model)
    assert "PRIVATE_PARENT_SENTINEL" not in "".join(files.values())
    assert tasks[a["id"]]["reading_path"] in files[model["task_index_path"]]


def test_unique_native_relation_only_and_explicit_id_takes_precedence():
    source = hierarchy_fixture()
    a, b, c = source["tasks"][:3]
    b.pop("parent_task_id")
    c.pop("parent_task_id")
    a["child"] = {"childThreadId": "synthetic-parent-thread"}
    b["origin"] = {"thread_id": "synthetic-parent-thread"}
    tasks = task_entries(source)
    assert tasks[b["id"]]["parent_entry_id"] == tasks[a["id"]]["id"]
    assert tasks[b["id"]]["relationship_basis"] == "relação nativa de threads"
    c["child"] = {"childThreadId": "synthetic-parent-thread"}
    assert task_entries(source)[b["id"]]["parent_entry_id"] is None
    b["parent_task_id"] = a["id"]
    assert task_entries(source)[b["id"]]["parent_entry_id"] == tasks[a["id"]]["id"]


def test_same_title_wave_and_timestamp_never_create_subtasks():
    source = hierarchy_fixture()
    for task in source["tasks"]:
        task.pop("parent_task_id", None)
        task["wave"] = "Onda 1"
        task["title"] = "Mesmo título"
    assert all(
        e["parent_entry_id"] is None and not e["subtask_ids"]
        for e in task_entries(source).values()
    )
