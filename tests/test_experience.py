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
    assert any(name.startswith("Assuntos/Importação--") for name in files)
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
        if not href.startswith("t3-thread:"):
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
    navigation = "\n".join(c for p, c in files.items() if p.startswith("Navegar/"))
    assert "<script>" not in navigation and "[[outside|go]]" not in navigation
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
    entry = project_model(source, "demo")["entries"][0]
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
        {"title": report["title"]},
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
        "Importação sintética" in name for name in files if name.startswith("Navegar/")
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
