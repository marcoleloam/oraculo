"""Offline reading dashboard for one scoped portfolio. No network and no extra dependencies."""

from __future__ import annotations

import json
import re
from html import escape as esc
from pathlib import PurePosixPath
from urllib.parse import quote

# Scope token for the shared library. Project ids must not use this value.
SHARED_SCOPE = "shared"
# Evidence for shared documents is addressed under this one vault-relative directory.
SHARED_EXPORT_ROOT = "Compartilhado"


def dashboard(model: dict) -> str:
    """Render the portfolio model as one self-contained HTML document."""
    from oraculo.experience import KINDS, metric_lines, wave_sort
    from oraculo.memory.schema import digest

    if (
        not isinstance(model, dict)
        or model.get("schema_version") != 1
        or model.get("kind") != "portfolio"
        or not isinstance(model.get("projects"), list)
        or not isinstance(model.get("links", []), list)
        or (
            model.get("shared") is not None
            and not isinstance(model.get("shared"), dict)
        )
    ):
        raise ValueError("experience_portfolio_html_model")

    def e(value) -> str:
        return esc("" if value is None else str(value), quote=True)

    def kind_label(kind) -> str:
        return KINDS.get(
            kind, str(kind) if isinstance(kind, str) and kind else "Registro"
        )

    def strings(value) -> list[str]:
        if not isinstance(value, list):
            return []
        return [item for item in value if isinstance(item, str)]

    shared_paths: set[str] = set()

    def safe_relative(value) -> str | None:
        if not isinstance(value, str):
            return None
        if (
            not value
            or value.startswith(("/", "\\"))
            or any(char in value for char in "\x00\r\n\\")
            or "://" in value
            or value.lower().startswith("javascript:")
        ):
            return None
        parts = PurePosixPath(value).parts
        if (
            not parts
            or any(part in {"", ".", ".."} for part in parts)
            or any(":" in part for part in parts)
        ):
            return None
        return "/".join(parts)

    def evidence_href(root, document_path) -> str | None:
        relative_root = safe_relative(
            SHARED_EXPORT_ROOT if document_path in shared_paths else root
        )
        relative_path = safe_relative(document_path)
        if not relative_root or not relative_path:
            return None
        return quote(
            f"{relative_root}/Evidencias/{relative_path}.html",
            safe="/",
        )

    projects = []
    seen_ids = set()
    for raw in model["projects"]:
        if not isinstance(raw, dict) or not isinstance(raw.get("project_id"), str):
            raise ValueError("experience_portfolio_project_id")
        project_id = raw["project_id"]
        if not project_id or project_id == SHARED_SCOPE or project_id in seen_ids:
            raise ValueError("experience_portfolio_project_id")
        seen_ids.add(project_id)
        entries = []
        for entry in raw.get("entries") or []:
            if (
                isinstance(entry, dict)
                and isinstance(entry.get("id"), str)
                and entry["id"]
            ):
                entries.append(entry)
        projects.append(
            {
                "raw": raw,
                "id": project_id,
                "title": raw.get("project_title")
                if isinstance(raw.get("project_title"), str)
                and raw.get("project_title")
                else "Projeto sem título",
                "entries": entries,
                "by_id": {entry["id"]: entry for entry in entries},
                "evidence": raw.get("evidence")
                if isinstance(raw.get("evidence"), dict)
                else raw.get("documents")
                if isinstance(raw.get("documents"), dict)
                else {},
                "export_root": raw.get("export_root")
                if isinstance(raw.get("export_root"), str)
                else "",
                "observed_at": raw.get("observed_at")
                if isinstance(raw.get("observed_at"), str) and raw.get("observed_at")
                else "desconhecida",
            }
        )
    titles = [project["title"] for project in projects]
    for project in projects:
        root = project["export_root"] or project["id"]
        project["label"] = (
            f"{project['title']} · {root}"
            if titles.count(project["title"]) > 1
            else project["title"]
        )

    shared_raw = model.get("shared") or {}
    shared_entries = [
        entry
        for entry in shared_raw.get("entries") or []
        if isinstance(entry, dict) and isinstance(entry.get("id"), str) and entry["id"]
    ]
    shared = {
        "entries": shared_entries,
        "by_id": {entry["id"]: entry for entry in shared_entries},
        "evidence": shared_raw.get("documents")
        if isinstance(shared_raw.get("documents"), dict)
        else {},
        "label": "Conhecimento compartilhado",
    }
    shared_paths.update(
        entry["document_path"]
        for entry in shared_entries
        if isinstance(entry.get("document_path"), str) and not entry.get("conflict")
    )
    projects_by_id = {project["id"]: project for project in projects}
    links = []
    for raw_link in model.get("links") or []:
        if not isinstance(raw_link, dict):
            continue
        source = _endpoint(
            raw_link.get("source_project_id"),
            raw_link.get("source_entry_id"),
            projects_by_id,
            shared,
        )
        target = _endpoint(
            raw_link.get("target_project_id"),
            raw_link.get("target_entry_id"),
            projects_by_id,
            shared,
        )
        if source and target:
            links.append(
                {
                    "source": source,
                    "target": target,
                    "relation": raw_link.get("relation")
                    if isinstance(raw_link.get("relation"), str)
                    else "relação explícita",
                    "basis": raw_link.get("basis")
                    if isinstance(raw_link.get("basis"), str)
                    else "explícita",
                }
            )

    def scope_of(entry_id, project):
        return project["by_id"].get(entry_id) if isinstance(entry_id, str) else None

    def context(scope_label, entry, by_id) -> str:
        ancestors = strings(entry.get("ancestor_ids"))
        related = scope_of(entry.get("related_task_id"), {"by_id": by_id})
        if related:
            ancestors = strings(related.get("ancestor_ids")) + [related["id"]]
        names = [
            str(by_id[item].get("title") or "Sem título")
            for item in ancestors
            if item in by_id
        ]
        return " / ".join([scope_label, *names])

    def button(
        entry,
        article_id,
        scope,
        project_id,
        scope_label,
        by_id,
        nested="",
        show_context=False,
    ):
        trail = (
            f'<small class="context">{e(context(scope_label, entry, by_id))}</small>'
            if show_context
            else ""
        )
        title = entry.get("title") if isinstance(entry.get("title"), str) else ""
        title = title or "Sem título"
        state = entry.get("state") if isinstance(entry.get("state"), str) else ""
        state = state or "Estado não informado"
        return (
            f'<li><button type="button" data-scope="{e(scope)}" '
            f'data-project="{e(project_id)}" data-entry="{e(entry["id"])}" '
            f'data-kind="{e(entry.get("kind") or "")}" aria-controls="{e(article_id)}">'
            f"{trail}<span>{e(title)}</span><small>{e(kind_label(entry.get('kind')))}"
            f" · {e(state)}</small></button>{nested}</li>"
        )

    def task_tree(by_id, scope, project_id, scope_label, ids):
        def node(entry, visiting):
            children = []
            child_ids = strings(entry.get("subtask_ids"))
            for child_id in child_ids:
                child = by_id.get(child_id)
                if child and child_id not in visiting:
                    children.append(child)
            nested = ""
            if children:
                nested = (
                    '<details class="subtasks" open><summary>Subtarefas ('
                    + str(len(children))
                    + ")</summary><ul>"
                    + "".join(
                        node(child, visiting | {entry["id"]}) for child in children
                    )
                    + "</ul></details>"
                )
            return button(
                entry,
                ids[entry["id"]],
                scope,
                project_id,
                scope_label,
                by_id,
                nested,
            )

        ordered = []
        for entry in by_id.values():
            parent_id = entry.get("parent_entry_id")
            parent = by_id.get(parent_id) if isinstance(parent_id, str) else None
            if entry.get("identity") and not parent:
                ordered.append(entry)
        tree = (
            '<ul class="task-tree">'
            + "".join(node(entry, set()) for entry in ordered)
            + "</ul>"
        )
        if not ordered:
            tree += (
                '<p class="meta">Sem tarefas registradas neste snapshot. '
                "O legado de memória está na Biblioteca.</p>"
            )
        return tree

    def library(entries, by_id, scope, project_id, scope_label, ids):
        documents = [
            entry for entry in entries if isinstance(entry.get("document_path"), str)
        ]
        records = [
            entry
            for entry in entries
            if entry.get("kind") in {"decision", "lesson", "error"}
        ]
        parts = []
        if documents:
            parts.append(
                "<h3>Documentos</h3><ul>"
                + "".join(
                    button(
                        entry, ids[entry["id"]], scope, project_id, scope_label, by_id
                    )
                    for entry in documents
                )
                + "</ul>"
            )
        if records:
            parts.append(
                "<h3>Decisões, aprendizados e erros</h3><ul>"
                + "".join(
                    button(
                        entry, ids[entry["id"]], scope, project_id, scope_label, by_id
                    )
                    for entry in records
                )
                + "</ul>"
            )
        if not parts:
            parts.append('<p class="meta">Sem documentos neste recorte.</p>')
        return "".join(parts)

    def link_buttons(entry_scope, project_id, entry_id):
        related = []
        for link in links:
            ends = (link["source"], link["target"])
            if not any(
                item["scope"] == entry_scope
                and item["project_id"] == project_id
                and item["entry"]["id"] == entry_id
                for item in ends
            ):
                continue
            ends_html = []
            for item in ends:
                ends_html.append(
                    f'<button class="source" type="button" data-scope="{e(item["scope"])}" '
                    f'data-project="{e(item["project_id"])}" data-entry="{e(item["entry"]["id"])}">'
                    f"{e(item['label'])}: {e(item['entry'].get('title') or 'Sem título')}</button>"
                )
            related.append(
                '<li><span class="scope-label">Referência explícita</span> '
                + ends_html[0]
                + f' <span class="meta">{e(link["relation"])} · {e(link["basis"])}</span> '
                + ends_html[1]
                + "</li>"
            )
        if not related:
            return ""
        return (
            '<section><h3>Referências entre projetos</h3><ul class="sources">'
            + "".join(related)
            + "</ul></section>"
        )

    catalogs = []
    shared_ids = {}
    sequence = 0
    for project in projects:
        ids = {}
        for entry in project["entries"]:
            ids[entry["id"]] = f"read-{sequence}"
            sequence += 1
        catalogs.append((project, ids))
    for entry in shared_entries:
        shared_ids[entry["id"]] = f"read-{sequence}"
        sequence += 1

    def article(
        entry, article_id, scope, project_id, scope_label, by_id, evidence, export_root
    ):
        title = (
            entry.get("title")
            if isinstance(entry.get("title"), str) and entry.get("title")
            else "Sem título"
        )
        state = (
            entry.get("state")
            if isinstance(entry.get("state"), str)
            else "Estado não informado"
        )
        updated = (
            entry.get("updated_at")
            if isinstance(entry.get("updated_at"), str)
            else "desconhecida"
        )
        text = entry.get("text") if isinstance(entry.get("text"), str) else ""
        topics = strings(entry.get("topics"))
        wave = entry.get("wave") if isinstance(entry.get("wave"), str) else ""
        parts = []
        for key, label in (("decisions", "Decisões"), ("lessons", "Aprendizados")):
            values = strings(entry.get(key))
            if values:
                parts.append(
                    f"<section><h3>{label}</h3>"
                    + "".join(f'<p class="reading">{e(item)}</p>' for item in values)
                    + "</section>"
                )
        subtasks = ""
        if entry.get("identity"):
            children = [
                by_id[child_id]
                for child_id in strings(entry.get("subtask_ids"))
                if child_id in by_id
            ]
            child_links = "".join(
                f'<li><button class="source" type="button" data-scope="{e(scope)}" '
                f'data-project="{e(project_id)}" data-entry="{e(child["id"])}">'
                f"{e(child.get('title') or 'Sem título')}</button></li>"
                for child in children
            )
            subtasks = (
                "<section><h3>Subtarefas</h3>"
                + (
                    f'<ul class="sources">{child_links}</ul>'
                    if child_links
                    else "<p>Sem subtarefas registradas.</p>"
                )
                + "</section>"
            )
            parent = scope_of(entry.get("parent_entry_id"), {"by_id": by_id})
            if parent:
                subtasks += (
                    f'<p>Tarefa pai: <button class="source" type="button" data-scope="{e(scope)}" '
                    f'data-project="{e(project_id)}" data-entry="{e(parent["id"])}">'
                    f"{e(parent.get('title') or 'Sem título')}</button></p>"
                )
            if entry.get("relationship_status") in {"orphan", "cycle"}:
                subtasks += (
                    '<p class="meta">Relação de tarefa pai indisponível neste recorte '
                    "ou inconsistente. Registro preservado sem inferir parentesco.</p>"
                )
        source_links = []
        for path in strings(entry.get("sources")):
            match = next(
                (item for item in by_id.values() if item.get("document_path") == path),
                None,
            )
            if match:
                source_links.append(
                    f'<li><button class="source" type="button" data-scope="{e(scope)}" '
                    f'data-project="{e(project_id)}" data-entry="{e(match["id"])}">'
                    f"{e(match.get('title') or 'Sem título')}</button></li>"
                )
        related_records = []
        if entry.get("identity"):
            related_records = [
                item
                for item in by_id.values()
                if item.get("task_id") == entry.get("identity")
            ]
        related = ""
        if related_records:
            related = (
                '<section><h3>Registros relacionados da tarefa</h3><ul class="sources">'
                + "".join(
                    f'<li><button class="source" type="button" data-scope="{e(scope)}" '
                    f'data-project="{e(project_id)}" data-entry="{e(item["id"])}">'
                    f"{e(kind_label(item.get('kind')))}: {e(item.get('title') or 'Sem título')}"
                    "</button></li>"
                    for item in related_records
                )
                + "</ul></section>"
            )
        path = (
            entry.get("document_path")
            if isinstance(entry.get("document_path"), str)
            else ""
        )
        href = evidence_href(export_root, path) if path else None
        blob = (
            evidence.get(path) if path and isinstance(evidence.get(path), str) else None
        )
        evidence_link = (
            f'<p><a href="{e(href)}">Abrir evidência original preservada ↗</a></p>'
            if href
            else (
                '<p class="meta">Caminho de evidência indisponível neste recorte.</p>'
                if path
                else ""
            )
        )
        if entry.get("conflict"):
            version_links = []
            for version in entry.get("versions") or []:
                if not isinstance(version, dict):
                    continue
                sha = version.get("sha256")
                relative_path = safe_relative(path)
                if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha):
                    continue
                if not relative_path:
                    continue
                target = quote(
                    f"{SHARED_EXPORT_ROOT}/Conflitos/{sha}/Evidencias/{relative_path}.txt",
                    safe="/",
                )
                origins = ", ".join(
                    projects_by_id[pid]["label"]
                    for pid in strings(version.get("project_ids"))
                    if pid in projects_by_id
                )
                version_links.append(
                    f'<li><a href="{e(target)}">Versão {e(sha[:12])}'
                    f" · {e(origins)}</a></li>"
                )
            evidence_link = (
                "<p>Versões divergentes preservadas; nenhuma foi escolhida.</p><ul>"
                + "".join(version_links)
                + "</ul>"
            )
        original = (
            '<details class="technical"><summary>Conteúdo original integral</summary><pre>'
            + e(blob)
            + "</pre></details>"
            if isinstance(blob, str) and entry.get("structured") is True
            else ""
        )
        technical = [entry.get("identity") or entry["id"]]
        if isinstance(entry.get("wave_basis"), str) and entry.get("wave_basis"):
            technical.append("Origem da onda: " + entry["wave_basis"])
        if path:
            technical.append(path)
        if isinstance(blob, str):
            technical.append("SHA-256: " + digest(blob))
        metrics = entry.get("metrics")
        if isinstance(metrics, dict):
            try:
                technical.extend(metric_lines(metrics))
            except (KeyError, TypeError):
                pass
        if entry.get("kind") == "error" and isinstance(entry.get("occurrences"), int):
            technical.append(
                f"Ocorrências explícitas da mesma chave: {entry['occurrences']}"
            )
        view = (
            entry.get("presentation")
            if isinstance(entry.get("presentation"), dict)
            else {}
        )
        if isinstance(view.get("url"), str) and view.get("url"):
            technical.append(
                "Conversa de origem (sem navegação externa): " + view["url"]
            )
        unavailable = entry.get("unavailable_sources")
        unavailable = (
            unavailable
            if isinstance(unavailable, int) and not isinstance(unavailable, bool)
            else 0
        )
        return (
            f'<article id="{e(article_id)}" data-scope="{e(scope)}" data-project="{e(project_id)}" '
            f'data-entry="{e(entry["id"])}" data-kind="{e(entry.get("kind") or "")}" '
            f'data-wave="{e(wave)}" data-state="{e(state)}" '
            f'data-topics="{e(json.dumps(topics, ensure_ascii=False))}" '
            f'data-scope-label="{e(scope_label)}" tabindex="-1">'
            f'<p class="breadcrumb">{e(context(scope_label, entry, by_id))} / {e(kind_label(entry.get("kind")))}</p>'
            f'<h2>{e(title)}</h2><p class="meta">{e(state)} · Atualização: {e(updated)}</p>'
            f'<section><h3>Conteúdo</h3><div class="reading">{e(text)}</div></section>'
            f"{''.join(parts)}{subtasks}{related}"
            f'<section><h3>Evidências</h3><ul class="sources">{"".join(source_links)}</ul>'
            f'<p class="meta">{unavailable} referências indisponíveis neste recorte.</p>'
            f"{evidence_link}</section>{original}{link_buttons(scope, project_id, entry['id'])}"
            f'<details class="technical"><summary>Identidade técnica e uso da memória</summary>'
            f"<pre>{e(chr(10).join(str(item) for item in technical))}</pre></details></article>"
        )

    hierarchy = []
    library_html = []
    intros = []
    articles = []
    cards = []
    for project, ids in catalogs:
        panel = e(project["id"])
        hierarchy.append(
            f'<div id="hierarchy-{panel}" class="project-panel" data-project-panel="{panel}">'
            f'<p class="scope-label">Projeto · {e(project["label"])}</p>'
            + task_tree(
                project["by_id"], "project", project["id"], project["label"], ids
            )
            + "</div>"
        )
        library_html.append(
            f'<div id="library-{panel}" class="project-panel" data-project-panel="{panel}">'
            f'<p class="scope-label">Biblioteca · {e(project["label"])}</p>'
            + library(
                project["entries"],
                project["by_id"],
                "project",
                project["id"],
                project["label"],
                ids,
            )
            + "</div>"
        )
        tasks = [entry for entry in project["entries"] if entry.get("identity")]
        documents = [
            entry
            for entry in project["entries"]
            if isinstance(entry.get("document_path"), str)
        ]
        children = [
            entry for entry in tasks if scope_of(entry.get("parent_entry_id"), project)
        ]
        empty = ""
        if not tasks:
            empty += "<p>Sem tarefas registradas neste snapshot.</p>"
        if not documents:
            empty += "<p>Sem documentos neste projeto.</p>"
        intros.append(
            f'<section class="project-intro" data-project-panel="{panel}" hidden>'
            f"<h2>{e(project['title'])}</h2>"
            f'<p class="scope-label">Projeto · {e(project["label"])}</p>'
            f'<p class="meta">Tarefas e fixes: {len(tasks)} · Subtarefas: {len(children)} · '
            f"Documentos: {len(documents)}</p>"
            f'<p class="meta">Observado em {e(project["observed_at"])}. '
            "Hora do snapshot, sem atualização automática.</p>"
            f"{empty}</section>"
        )
        cards.append(
            '<li><button type="button" data-open-project="'
            + panel
            + '"><span class="card-title">'
            + e(project["title"])
            + "</span><small>"
            + e(project["export_root"] or "diretório não informado")
            + "</small><small>Tarefas e fixes: "
            + str(len(tasks))
            + " · Documentos: "
            + str(len(documents))
            + "</small><small>Observado em "
            + e(project["observed_at"])
            + "</small>"
            + (
                "<small>Sem tarefas e sem documentos neste snapshot.</small>"
                if not tasks and not documents
                else ""
            )
            + "</button></li>"
        )
        for entry in project["entries"]:
            articles.append(
                article(
                    entry,
                    ids[entry["id"]],
                    "project",
                    project["id"],
                    project["label"],
                    project["by_id"],
                    project["evidence"],
                    project["export_root"],
                )
            )

    shared_hierarchy = (
        '<p class="scope-label">Escopo: conhecimento compartilhado</p>'
        + task_tree(shared["by_id"], SHARED_SCOPE, "", shared["label"], shared_ids)
    )
    shared_library = (
        '<p class="scope-label">Escopo: conhecimento compartilhado</p>'
        + library(
            shared_entries,
            shared["by_id"],
            SHARED_SCOPE,
            "",
            shared["label"],
            shared_ids,
        )
    )
    if not shared_entries:
        shared_hierarchy += (
            '<p class="meta">Nenhum conhecimento compartilhado neste recorte.</p>'
        )
    for entry in shared_entries:
        articles.append(
            article(
                entry,
                shared_ids[entry["id"]],
                SHARED_SCOPE,
                "",
                shared["label"],
                shared["by_id"],
                shared["evidence"],
                SHARED_EXPORT_ROOT,
            )
        )
    intros.append(
        '<section class="project-intro" data-project-panel="shared" hidden>'
        "<h2>Conhecimento compartilhado</h2>"
        '<p class="scope-label">Escopo: conhecimento compartilhado. '
        "Não é um projeto e não herda tarefas de outro projeto.</p>"
        + (
            f'<p class="meta">{len(shared_entries)} registros sanitizados neste recorte.</p>'
            if shared_entries
            else '<p class="meta">Nenhum conhecimento compartilhado neste recorte.</p>'
        )
        + "</section>"
    )

    result_entries = []
    for project, ids in catalogs:
        for entry in project["entries"]:
            result_entries.append(
                button(
                    entry,
                    ids[entry["id"]],
                    "project",
                    project["id"],
                    project["label"],
                    project["by_id"],
                    show_context=True,
                )
            )
    for entry in shared_entries:
        result_entries.append(
            button(
                entry,
                shared_ids[entry["id"]],
                SHARED_SCOPE,
                "",
                shared["label"],
                shared["by_id"],
                show_context=True,
            )
        )

    link_items = []
    for link in links:
        source = link["source"]
        target = link["target"]
        link_items.append(
            "<li>"
            f'<button type="button" data-scope="{e(source["scope"])}" '
            f'data-project="{e(source["project_id"])}" data-entry="{e(source["entry"]["id"])}">'
            f"{e(source['label'])}: {e(source['entry'].get('title') or 'Sem título')}</button>"
            f' <span class="meta">{e(link["relation"])} · base: {e(link["basis"])}</span> '
            f'<button type="button" data-scope="{e(target["scope"])}" '
            f'data-project="{e(target["project_id"])}" data-entry="{e(target["entry"]["id"])}">'
            f"{e(target['label'])}: {e(target['entry'].get('title') or 'Sem título')}</button>"
            "</li>"
        )
    if link_items:
        cross = (
            '<section><h2>Referências explícitas</h2><ul class="sources">'
            + "".join(link_items)
            + "</ul></section>"
        )
    else:
        cross = (
            "<section><h2>Referências explícitas</h2>"
            '<p class="meta">Nenhuma referência cruzada explícita neste recorte.</p></section>'
        )
    if not projects:
        cards_html = '<p class="meta">Nenhum projeto neste recorte.</p>'
    else:
        cards_html = '<ul class="cards">' + "".join(cards) + "</ul>"

    counts = model.get("counts") if isinstance(model.get("counts"), dict) else {}

    def count(key, fallback):
        value = counts.get(key, fallback)
        return (
            value
            if isinstance(value, int) and not isinstance(value, bool)
            else fallback
        )

    summary = (
        f"{count('projects', len(projects))} projetos · "
        f"{count('tasks', sum(1 for project in projects for entry in project['entries'] if entry.get('identity')))} tarefas · "
        f"{count('documents', sum(1 for project in projects for entry in project['entries'] if isinstance(entry.get('document_path'), str)) + sum(1 for entry in shared_entries if isinstance(entry.get('document_path'), str)))} documentos"
    )
    observed = (
        model.get("observed_at")
        if isinstance(model.get("observed_at"), str) and model.get("observed_at")
        else "desconhecida"
    )
    waves = sorted(
        {
            entry.get("wave")
            for project in projects
            for entry in project["entries"]
            if isinstance(entry.get("wave"), str) and entry.get("wave")
        }
        | {
            entry.get("wave")
            for entry in shared_entries
            if isinstance(entry.get("wave"), str) and entry.get("wave")
        },
        key=wave_sort,
    )
    states = sorted(
        {
            entry.get("state")
            for project in projects
            for entry in project["entries"]
            if isinstance(entry.get("state"), str) and entry.get("state")
        }
        | {
            entry.get("state")
            for entry in shared_entries
            if isinstance(entry.get("state"), str) and entry.get("state")
        }
    )
    topics = sorted(
        {
            topic
            for project in projects
            for entry in project["entries"]
            for topic in strings(entry.get("topics"))
        }
        | {topic for entry in shared_entries for topic in strings(entry.get("topics"))}
    )
    kinds = list(KINDS.items())
    known = {key for key, _label in kinds}
    for project in projects:
        for entry in project["entries"]:
            kind = entry.get("kind")
            if isinstance(kind, str) and kind and kind not in known:
                kinds.append((kind, kind))
                known.add(kind)
    for entry in shared_entries:
        kind = entry.get("kind")
        if isinstance(kind, str) and kind and kind not in known:
            kinds.append((kind, kind))
            known.add(kind)

    def options(items):
        return "".join(
            f'<option value="{e(key)}">{e(label)}</option>' for key, label in items
        )

    project_options = "".join(
        f'<option value="{e(project["id"])}">{e(project["label"])}</option>'
        for project in projects
    )
    demo = (
        '<p class="demo">Demonstração sintética · dados fictícios para explorar a navegação</p>'
        if model.get("demonstration") is True
        or any(project["raw"].get("demonstration") is True for project in projects)
        else ""
    )
    overview = (
        "<h2>Todos os projetos</h2>"
        f'<p class="meta" id="portfolio-counts">{e(summary)}</p>'
        f'<p class="meta">Observado em {e(observed)}. Snapshot offline; '
        "esta hora é a observação registrada, não uma consulta ao vivo.</p>"
        + cards_html
        + cross
        + (
            "<section><h2>Conhecimento compartilhado</h2>"
            '<p class="scope-label">Escopo: conhecimento compartilhado</p>'
            + (
                f"<p>{len(shared_entries)} registros sanitizados. Abra o seletor para ler cada um com a origem rotulada.</p>"
                if shared_entries
                else "<p>Nenhum conhecimento compartilhado neste recorte.</p>"
            )
            + "</section>"
        )
    )
    template = """<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>Todos os projetos · Oráculo</title><style>
:root{color-scheme:dark;--bg:#141619;--panel:#1c1f23;--text:#eeeae2;--muted:#b6b4ad;--line:#393c40;--gold:#dfbf7e;--active:#34302a}html{scroll-padding-top:7rem}input,select,button,article{scroll-margin-top:7rem}
*{box-sizing:border-box}body{overflow-wrap:anywhere;margin:0;background:var(--bg);color:var(--text);font:16px/1.65 system-ui,sans-serif}button,input,select{font:inherit;color:inherit}button,summary{cursor:pointer}a{color:var(--gold);text-underline-offset:4px}button,input,select{border:1px solid var(--line);border-radius:7px;background:var(--panel)}button{padding:10px 14px;min-height:44px}button:hover{border-color:var(--gold)}:focus-visible{outline:3px solid var(--gold);outline-offset:3px}[hidden]{display:none!important}.skip{position:absolute;left:12px;top:-100px;background:var(--panel);padding:12px;z-index:5}.skip:focus{top:8px}header{padding:28px 32px 22px;border-bottom:1px solid var(--line)}.brand{color:var(--gold);font-weight:700;font-size:12px;letter-spacing:.15em}h1,h2{line-height:1.2}h1{font-size:clamp(27px,4vw,40px);margin:10px 0}h2{font-size:clamp(22px,3vw,30px);margin:0 0 12px}header p{margin:6px 0;color:var(--muted);font-size:14px}.demo{color:var(--gold);border-left:3px solid var(--gold);padding-left:12px}.project-bar{position:sticky;top:0;z-index:4;display:flex;align-items:center;gap:12px;padding:10px 16px;background:var(--bg);border-bottom:1px solid var(--line);font-size:14px}.project-bar label{display:grid;gap:2px;min-width:0;flex:1 1 220px;color:var(--gold);font-size:12px}.project-bar select{width:100%;min-width:0;min-height:44px;padding:8px 10px;font-size:16px}.project-bar strong{min-width:0}.project-bar a{color:var(--gold)}.scope-label{color:var(--gold);font-size:13px;font-weight:650}.subtasks{margin-left:14px;border-left:1px solid var(--line);padding-left:10px}.subtasks summary{font-size:13px;color:var(--muted)}.controls{padding:20px 32px;display:grid;grid-template-columns:2fr repeat(4,minmax(0,1fr));gap:14px;border-bottom:1px solid var(--line)}label{display:grid;gap:5px;font-size:13px;color:var(--muted)}input,select{width:100%;min-width:0;min-height:44px;padding:9px 12px;font-size:16px}.workspace{display:grid;grid-template-columns:minmax(250px,340px) minmax(0,1fr);align-items:start}aside{padding:22px 20px;border-right:1px solid var(--line);min-width:0}.modes{display:flex;gap:8px;flex-wrap:wrap}.modes button{flex:1;padding:8px;font-size:14px}.modes [aria-pressed=true]{color:var(--gold);background:var(--active);border-color:var(--gold)}#count{font-size:13px;color:var(--muted);margin:15px 0}nav{max-height:70vh;overflow:auto;scrollbar-gutter:stable}nav ul,.cards{list-style:none;margin:0;padding:0}nav li,.cards li{margin:6px 0}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px}nav li button,.cards button{display:block;text-align:left;width:100%}.cards button{min-height:44px}.card-title{display:block;font-weight:650;font-size:18px}nav li button{border-color:transparent;background:transparent;padding:10px 12px;border-left:2px solid transparent}nav li button[aria-current=true]{background:var(--active);border-left-color:var(--gold)}nav li button span,.card-title{overflow-wrap:anywhere}nav small,.cards small{display:block;color:var(--muted);font-size:12px;margin-top:4px}main{min-width:0;padding:28px 40px 48px}article,.project-intro{scroll-margin-top:96px;max-width:850px;overflow-wrap:anywhere}article:focus{outline:none}.js article{display:none}.js article.selected{display:block}.breadcrumb{color:var(--gold);font-size:13px;margin:0 0 15px}h3{font-size:15px;color:var(--gold);margin:0 0 14px}.meta{color:var(--muted);font-size:13px}article section,main section{border-top:1px solid var(--line);padding-top:22px;margin-top:22px}.reading{white-space:pre-wrap;font:16px/1.85 system-ui;tab-size:2}.sources{padding-left:0;list-style:none}.source{border:0;background:transparent;color:var(--gold);text-align:left;text-decoration:underline;text-underline-offset:3px;padding:8px 0}.technical{margin-top:30px;padding-top:14px;border-top:1px solid var(--line);font-size:13px;color:var(--muted)}summary{min-height:36px}pre{white-space:pre-wrap;overflow-wrap:anywhere}footer{padding:20px 32px;border-top:1px solid var(--line);font-size:12px;color:var(--muted)}#mobile-nav{display:none}#empty{color:var(--muted)}
@media(max-width:850px){.controls{grid-template-columns:1fr 1fr}.search{grid-column:1/-1}.workspace{grid-template-columns:270px minmax(0,1fr)}main{padding:24px}}
@media(max-width:620px){header,.controls{padding:16px}.project-bar{flex-wrap:wrap;gap:6px;padding:8px 12px}.project-bar label{flex:1 1 100%}.project-bar strong{flex:1 1 auto;font-size:14px}.project-bar a{flex:0 0 auto}html{scroll-padding-top:8.5rem}.workspace{display:block}aside{border-right:0;border-bottom:1px solid var(--line);padding:16px}main{padding:20px 16px 36px}.js #mobile-nav{display:inline-block;margin-top:10px}nav{max-height:420px}h1{font-size:26px}h2{font-size:22px}footer{padding:16px}.controls{gap:10px}}
</style></head><body><a class="skip" href="#reader">Ir para o conteúdo</a>
<div class="project-bar"><label>Projeto<select id="project" aria-controls="reader"><option value="">Todos os projetos</option>@@PROJECT_OPTIONS@@<option value="shared">Conhecimento compartilhado</option></select></label><strong id="active-label">Todos os projetos</strong><a href="#reader">Conteúdo</a></div>
<header><div class="brand">ORÁCULO / COFRE</div><h1>Todos os projetos</h1>@@DEMO@@
<p>Projeto → Tarefas → Subtarefas · Biblioteca e conhecimento compartilhado · Ondas opcionais</p>
<p id="summary">@@SUMMARY@@</p><p>Observado em @@OBSERVED@@. Snapshot offline, sem atualização automática. Abra Inicio.md no mesmo vault, sem plugins.</p></header>
<div class="controls" role="search"><label class="search">Buscar em todo o recorte, inclusive decisões, aprendizados e erros<input id="query" type="search" placeholder="Ex.: decisão, aprendizado, erro" enterkeyhint="search"></label><label>Tipo<select id="kind"><option value="">Todos os tipos</option>@@KINDS@@</select></label><label>Assunto<select id="topic"><option value="">Todos os assuntos</option>@@TOPICS@@</select></label><label>Estado<select id="state"><option value="">Todos os estados</option>@@STATES@@</select></label><label>Onda (opcional)<select id="wave"><option value="">Todas as ondas / sem filtro</option>@@WAVES@@</select></label></div>
<div class="workspace"><aside><div class="modes" aria-label="Modo de navegação"><button id="overview-mode" type="button" aria-pressed="true">Projetos</button><button id="hierarchy-mode" type="button" aria-pressed="false">Tarefas</button><button id="library-mode" type="button" aria-pressed="false">Biblioteca</button></div><button type="button" id="mobile-nav" aria-controls="navigation" aria-expanded="true">Mostrar / ocultar navegação</button><p id="count" aria-live="polite">@@SUMMARY@@</p><nav id="navigation" aria-label="Projetos, tarefas e biblioteca"><div id="overview-nav"><p class="scope-label">Projetos do recorte</p>@@CARDS@@</div><div id="hierarchy" hidden>@@HIERARCHY@@</div><div id="library" hidden>@@LIBRARY@@</div><div id="shared-nav" hidden><div id="shared-hierarchy">@@SHARED_HIERARCHY@@</div><div id="shared-library" hidden>@@SHARED_LIBRARY@@</div></div><div id="results" hidden><ul>@@RESULTS@@</ul></div></nav></aside><main id="reader" tabindex="-1"><section id="overview-panel">@@OVERVIEW@@</section>@@INTROS@@<p id="empty" hidden>Nenhum registro corresponde aos filtros. Ajuste a busca, o tipo, o estado ou a onda.</p>@@ARTICLES@@</main></div>
<footer>Somente os projetos deste recorte e o conhecimento compartilhado sanitizado. Uma referência só aparece quando os dois lados estão no recorte. Declarações e resultados verificados são evidências distintas. Mantenha este pacote privado.</footer>
<script>
document.documentElement.classList.add('js');
const articles=Array.from(document.querySelectorAll('article'));
const keyOf=el=>[el.dataset.scope,el.dataset.project||'',el.dataset.entry].join('\\n');
const byKey=new Map(articles.map(a=>[keyOf(a),a]));
const nav=document.getElementById('navigation');
const controls=['query','kind','topic','state','wave'].map(id=>document.getElementById(id));
const projectSelect=document.getElementById('project');
const normalize=s=>s.normalize('NFD').replace(/[\\u0300-\\u036f]/g,'').toLocaleLowerCase('pt-BR');
const searchText=new Map(articles.map(a=>[keyOf(a),normalize(a.textContent)]));
let mode='overview',selected=null;
function matchesNow(){
 const [query,kind,topic,state,wave]=controls.map(c=>c.value);
 const found=new Set();
 for(const article of articles){
  let topics=[];
  try{topics=JSON.parse(article.getAttribute('data-topics')||'[]');}catch(error){topics=[];}
  if(searchText.get(keyOf(article)).includes(normalize(query))&&(!kind||article.dataset.kind===kind)&&(!topic||topics.includes(topic))&&(!state||article.dataset.state===state)&&(!wave||article.dataset.wave===wave))found.add(keyOf(article));
 }
 return found;
}
function searching(){return controls.some(c=>c.value);}
function buttonsIn(root){return root?Array.from(root.querySelectorAll('[data-entry]')):[];}
function activeRoot(){
 const scope=projectSelect.value;
 if(searching())return document.getElementById('results');
 if(scope==='shared')return document.getElementById(mode==='library'?'shared-library':'shared-hierarchy');
 if(mode==='library')return document.getElementById('library');
 if(mode==='hierarchy')return document.getElementById('hierarchy');
 return null;
}
function visibleButtons(found){
 const scope=projectSelect.value;
 const buttons=buttonsIn(activeRoot());
 if(!scope&&mode==='library')buttons.push(...buttonsIn(document.getElementById('shared-library')));
 return buttons.filter(button=>found.has(keyOf(button))&&(!scope||(button.dataset.scope===(scope==='shared'?'shared':'project')&&(scope==='shared'||button.dataset.project===scope))));
}
function render(focus){
 const found=matchesNow();
 const search=searching();
 const scope=projectSelect.value;
 for(const button of nav.querySelectorAll('[data-entry]'))button.parentElement.hidden=!found.has(keyOf(button));
 document.getElementById('results').hidden=!search;
 document.getElementById('overview-nav').hidden=search||scope!==''||mode!=='overview';
 document.getElementById('overview-panel').hidden=search||scope!==''||mode!=='overview';
 document.getElementById('hierarchy').hidden=search||scope==='shared'||mode!=='hierarchy';
 document.getElementById('library').hidden=search||scope==='shared'||mode!=='library';
 document.getElementById('shared-nav').hidden=search||!(scope==='shared'||(!scope&&mode==='library'));
 document.getElementById('shared-hierarchy').hidden=mode==='library';
 document.getElementById('shared-library').hidden=mode!=='library';
 for(const panel of document.querySelectorAll('[data-project-panel]')){
  if(!panel.classList.contains('project-intro'))panel.hidden=!!scope&&panel.dataset.projectPanel!==scope;
 }
 const visible=search?buttonsIn(document.getElementById('results')).filter(button=>found.has(keyOf(button))):visibleButtons(found);
 if(selected&&!found.has(selected))selected=null;
 if((search||scope||mode!=='overview')&&!visible.some(button=>keyOf(button)===selected)){
  const preferred=visible.find(button=>['task','fix'].includes(button.dataset.kind))||visible[0];
  selected=preferred?keyOf(preferred):null;
 }
 if(!search&&scope===''&&mode==='overview')selected=null;
 for(const intro of document.querySelectorAll('.project-intro'))intro.hidden=intro.dataset.projectPanel!==scope||selected!==null;
 for(const article of articles)article.classList.toggle('selected',keyOf(article)===selected);
 for(const button of nav.querySelectorAll('[data-entry]'))button.setAttribute('aria-current',String(keyOf(button)===selected));
 const current=selected?byKey.get(selected):null;
 const option=projectSelect.selectedOptions[0];
 document.getElementById('active-label').textContent=search?(current?'Resultado · '+current.dataset.scopeLabel:'Busca em todo o recorte'):!scope&&current?current.dataset.scopeLabel:(option?option.textContent:'Todos os projetos');
 document.getElementById('overview-mode').setAttribute('aria-pressed',String(!search&&scope===''&&mode==='overview'));
 document.getElementById('hierarchy-mode').setAttribute('aria-pressed',String(!search&&mode==='hierarchy'));
 document.getElementById('library-mode').setAttribute('aria-pressed',String(!search&&mode==='library'));
 document.getElementById('count').textContent=search?visible.length+' resultados no recorte':mode==='overview'?option.textContent:visible.length+(mode==='library'?' entradas na biblioteca':' tarefas e subtarefas');
 document.getElementById('empty').hidden=!(search&&visible.length===0);
 if(focus&&current)current.focus();
}
function chooseMode(){
 mode=projectSelect.value&&projectSelect.value!=='shared'&&buttonsIn(document.getElementById('hierarchy')).some(button=>button.dataset.project===projectSelect.value)?'hierarchy':projectSelect.value?'library':'overview';
}
function openEntry(scope,project,entry,focus){
 const button=articles.length&&byKey.get([scope,project,entry].join('\\n'));
 const inTree=buttonsIn(document.getElementById(scope==='shared'?'shared-hierarchy':'hierarchy')).some(item=>item.dataset.scope===scope&&item.dataset.project===project&&item.dataset.entry===entry);
 projectSelect.value=scope==='shared'?'shared':project;
 mode=inTree?'hierarchy':'library';
 selected=[scope,project,entry].join('\\n');
 if(button)render(focus);
}
nav.addEventListener('click',event=>{
 const opener=event.target.closest('[data-open-project]');
 if(opener){controls.forEach(control=>control.value='');projectSelect.value=opener.dataset.openProject;selected=null;chooseMode();render(false);return;}
 const button=event.target.closest('[data-entry]');
 if(!button)return;
 selected=keyOf(button);
 render(true);
});
document.getElementById('reader').addEventListener('click',event=>{
 const opener=event.target.closest('[data-open-project]');
 if(opener){controls.forEach(control=>control.value='');projectSelect.value=opener.dataset.openProject;selected=null;chooseMode();render(false);return;}
 const button=event.target.closest('[data-entry]');
 if(!button)return;
 controls.forEach(control=>control.value='');
 openEntry(button.dataset.scope,button.dataset.project||'',button.dataset.entry,true);
});
controls.forEach(control=>control.addEventListener('input',()=>render(false)));
projectSelect.addEventListener('change',()=>{selected=null;chooseMode();render(false);});
document.getElementById('overview-mode').addEventListener('click',()=>{controls.forEach(control=>control.value='');projectSelect.value='';mode='overview';selected=null;render(false);});
document.getElementById('hierarchy-mode').addEventListener('click',()=>{mode='hierarchy';selected=null;render(false);});
document.getElementById('library-mode').addEventListener('click',()=>{mode='library';selected=null;render(false);});
document.getElementById('mobile-nav').addEventListener('click',event=>{nav.hidden=!nav.hidden;event.currentTarget.setAttribute('aria-expanded',String(!nav.hidden));});
const mobile=matchMedia('(max-width:620px)'),toggle=document.getElementById('mobile-nav');
function adaptNavigation(){nav.hidden=mobile.matches;toggle.setAttribute('aria-expanded',String(!nav.hidden));}
mobile.addEventListener('change',adaptNavigation);adaptNavigation();
render(false);
</script></body></html>"""
    replacements = {
        "DEMO": demo,
        "SUMMARY": e(summary),
        "OBSERVED": e(observed),
        "KINDS": options(kinds),
        "STATES": options((item, item) for item in states),
        "TOPICS": options((item, item) for item in topics),
        "WAVES": options((item, item) for item in waves),
        "PROJECT_OPTIONS": project_options,
        "CARDS": cards_html,
        "HIERARCHY": "".join(hierarchy),
        "LIBRARY": "".join(library_html),
        "SHARED_HIERARCHY": shared_hierarchy,
        "SHARED_LIBRARY": shared_library,
        "RESULTS": "".join(result_entries),
        "OVERVIEW": overview,
        "INTROS": "".join(intros),
        "ARTICLES": "".join(articles),
    }
    # One replacement pass: source text containing template markers stays inert.
    # Reader content is escaped HTML, never interpolated into this script.
    return re.sub(
        r"@@([A-Z_]+)@@",
        lambda match: replacements[match[1]],
        template,
    )


def _endpoint(project_id, entry_id, projects_by_id, shared):
    if not isinstance(entry_id, str) or not entry_id:
        return None
    if project_id == SHARED_SCOPE:
        entry = shared["by_id"].get(entry_id)
        if not entry:
            return None
        return {
            "scope": SHARED_SCOPE,
            "project_id": "",
            "entry": entry,
            "label": shared["label"],
        }
    if not isinstance(project_id, str):
        return None
    project = projects_by_id.get(project_id)
    if not project:
        return None
    entry = project["by_id"].get(entry_id)
    if not entry:
        return None
    return {
        "scope": "project",
        "project_id": project_id,
        "entry": entry,
        "label": project["label"],
    }
