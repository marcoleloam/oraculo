"""One scoped presentation model for Obsidian, HTML and CLI. No live I/O here."""

from __future__ import annotations

import html
import json
import re
from pathlib import Path, PurePosixPath
from urllib.parse import quote

from oraculo.memory.schema import (
    MemoryError,
    corpus,
    digest,
    links,
    parse_page,
    render_page,
    resolve_link,
    safe_path,
)
from oraculo.presentation import markdown, plain, task_view, timestamp
from oraculo.protocol import canonical_json

KINDS = {
    "concept": "Assunto",
    "entity": "Entidade",
    "source": "Memória",
    "raw": "Evidência",
    "task": "Tarefa",
    "decision": "Decisão",
    "lesson": "Aprendizado",
    "error": "Erro",
}


def texts(value) -> list[str]:
    if value is None:
        return []
    values = value if isinstance(value, list) else [value]
    return [plain(v, 2000) for v in values if isinstance(v, str) and v.strip()]


def note_title(content: str, fallback="Memória sem título") -> str:
    try:
        meta, body = parse_page(content)
    except MemoryError:
        meta, body = {}, content
    heading = re.search(r"^#\s+(.+)$", body, re.M)
    if heading and not heading[1].startswith("Task memory cycle-"):
        return plain(html.unescape(heading[1]))
    aliases = texts(meta.get("aliases"))
    if aliases:
        return aliases[0]
    summary = re.search(r"^## Resumo\s*\n+([^\n]+)", body, re.M)
    return plain(html.unescape(summary[1]), 140) if summary else fallback


def note_excerpt(body):
    summary = re.search(r"^## Resumo\s*\n+(.*?)(?=^## |\Z)", body, re.M | re.S)
    text = summary[1] if summary else re.sub(r"^# .+\n", "", body, count=1)
    text = re.sub(
        r"\[\[([^\]]+)\]\]",
        lambda m: m[1].split("|", 1)[1] if "|" in m[1] else "fonte nos detalhes",
        text,
    )
    return html.unescape(text.strip())


def _scope_path(path, project):
    if not isinstance(path, str):
        raise ValueError("experience_invalid_path")
    value = PurePosixPath(path)
    if (
        value.is_absolute()
        or str(value) != path
        or ".." in value.parts
        or any(c in path for c in "\\[]|#\x00")
        or any(ord(c) < 32 for c in path)
    ):
        raise ValueError("experience_unsafe_document_path")
    return any(path.startswith(root + "/") for root in (corpus(project), "shared"))


def _clean_document(content, path, paths):
    targets = []
    for target in links(content):
        try:
            resolved = resolve_link(target, paths, path)
        except MemoryError:
            continue
        if resolved not in targets:
            targets.append(resolved)
    # Text is inert: no remote images, scripts or embedded source instructions.
    body = "\n".join("    " + line for line in content.splitlines())
    return (
        f"# {markdown(note_title(content, 'Evidência estruturada'))}\n\n"
        + body
        + "\n\n## Referências neste recorte\n\n"
        + "\n".join(f"- [[{target}|Abrir referência]]" for target in targets)
    )


def project_model(snapshot: dict, project: str) -> dict:
    if snapshot.get("schema_version") != 1:
        raise ValueError("experience_snapshot_version")
    projects = [p for p in snapshot.get("projects", []) if p.get("id") == project]
    if len(projects) != 1:
        raise ValueError("experience_select_one_known_project")
    name = plain(projects[0].get("title"))
    if not name:
        name = PurePosixPath(projects[0].get("path") or "").name
    if not name:
        raise ValueError("experience_project_name_required")
    docs = {}
    for doc in snapshot.get("documents", []):
        path = doc["path"]
        if not _scope_path(path, project):
            continue
        if path in docs:
            raise ValueError("experience_duplicate_document")
        content = doc["content"]
        if not isinstance(content, str) or (
            doc.get("sha256") and doc["sha256"] != digest(content)
        ):
            raise ValueError("experience_document_integrity")
        if "/raw/" in path and PurePosixPath(path).stem != digest(content):
            raise ValueError("experience_raw_integrity")
        docs[path] = content
    paths = set(docs)
    entries, reports = [], {}
    for path, content in sorted(docs.items()):
        if "/raw/" in path:
            try:
                report = json.loads(content)
            except (ValueError, TypeError):
                report = {}
            if (
                isinstance(report, dict)
                and report.get("project_id") == project
                and isinstance(report.get("cycle_id"), str)
            ):
                reports[report["cycle_id"]] = (report, path)
            continue
        if path.endswith(("/index.md", "/log.md")) or "/schema/" in path:
            continue
        try:
            meta, body = parse_page(content)
        except MemoryError:
            meta, body = {}, content
        kind = meta.get("type", "source")
        if kind not in KINDS:
            kind = "source"
        sources = []
        missing = 0
        for target in links(content):
            try:
                resolved = resolve_link(target, paths, path)
            except MemoryError:
                missing += 1
            else:
                if resolved != path and resolved not in sources:
                    sources.append(resolved)
        entries.append(
            dict(
                id=digest(path),
                kind=kind,
                title=note_title(content),
                topics=texts(meta.get("tags")),
                updated_at=timestamp(str(meta.get("updated"))),
                state="Revisada"
                if meta.get("reviewed") is True
                else "Revisão não confirmada",
                text=note_excerpt(body),
                sources=[path, *sources],
                unavailable_sources=missing,
            )
        )
    selected_cycles = [
        c for c in snapshot.get("cycles", []) if c.get("project_id") == project
    ]
    cycles = {c["id"]: c for c in selected_cycles}
    if len(cycles) != len(selected_cycles):
        raise ValueError("experience_duplicate_cycle")
    errors = {}
    seen_tasks = set()
    for task in snapshot.get("tasks", []):
        if (task.get("project") or {}).get("project_id") != project:
            continue
        task = dict(task, project_title=name)
        if task["id"] in seen_tasks:
            raise ValueError("experience_duplicate_task")
        seen_tasks.add(task["id"])
        view = task_view(task)
        cycle = cycles.get((task.get("memory") or {}).get("id"), {})
        report, raw_path = reports.get(cycle.get("id"), ({}, None))
        evidence = report.get("evidence") or {}
        if not isinstance(evidence, dict):
            evidence = {}
        receipt = cycle.get("read_receipt") or {}
        consulted = list(
            dict.fromkeys(
                s["path"]
                for s in receipt.get("sources", [])
                if isinstance(s, dict)
                and s.get("path") in paths
                and s.get("sha256") == digest(docs[s["path"]])
            )
        )
        checks = (
            evidence.get("checks") if isinstance(evidence.get("checks"), list) else []
        )
        metrics = dict(
            memory_found=cycle.get("read_state", "unknown"),
            service_consulted_at=receipt.get("at"),
            consulted_sources=consulted,
            agent_read="unknown",
            application_declared=texts(evidence.get("memory_application")),
            benefit_demonstrated="unknown",
            checks_passed=sum(
                isinstance(c, dict) and c.get("status") == "passed" for c in checks
            ),
            checks_failed=sum(
                isinstance(c, dict) and c.get("status") == "failed" for c in checks
            ),
            result_verified="unknown"
            if not task.get("completion_evidence")
            else task.get("work_status", "unknown"),
        )
        sources = list(dict.fromkeys(([raw_path] if raw_path else []) + consulted))
        ident = task["id"]
        topics = texts(evidence.get("topics"))
        entries.append(
            dict(
                id=digest("task:" + ident),
                identity=ident,
                kind="task",
                title=view["title"],
                topics=topics,
                updated_at=view["updated_at"],
                state=view["phase"],
                text="\n".join(texts(evidence.get("summary"))) or view["activity"],
                sources=sources,
                metrics=metrics,
                presentation=view,
            )
        )
        for field, kind in (
            ("decisions", "decision"),
            ("lessons", "lesson"),
            ("errors", "error"),
        ):
            for index, value in enumerate(texts(evidence.get(field))):
                entry = dict(
                    id=digest(f"{ident}:{kind}:{index}"),
                    kind=kind,
                    title=value[:140],
                    text=value,
                    topics=topics,
                    updated_at=view["updated_at"],
                    state="Declarado no relatório",
                    sources=sources,
                    task_id=ident,
                )
                if kind == "error":
                    # Exact explicit error strings only, no fuzzy causal inference.
                    errors.setdefault(value, set()).add(ident)
                entries.append(entry)
    for entry in entries:
        if entry["kind"] == "error":
            entry["occurrences"] = len(errors[entry["text"]])
    return dict(
        schema_version=1,
        project_id=project,
        project_title=name,
        observed_at=plain(snapshot.get("observed_at")) or "desconhecida",
        entries=entries,
        documents={p: _clean_document(c, p, paths) for p, c in sorted(docs.items())},
        evidence={p: c for p, c in sorted(docs.items())},
    )


def metric_lines(metrics):
    found = {
        "found": "encontrada",
        "empty": "vazia",
        "unavailable": "indisponível",
    }.get(metrics["memory_found"], "desconhecida")
    result = {
        "succeeded": "positivo verificado",
        "failed": "negativo verificado",
        "stopped": "interrupção verificada",
    }.get(metrics["result_verified"], "desconhecido")
    return [
        f"Memória no serviço: {found}",
        f"Fontes consultadas pelo serviço com hash conferido: {len(metrics['consulted_sources'])}; consulta: {timestamp(metrics['service_consulted_at'])}",
        "Leitura pelo agente: desconhecida",
        "Aplicação declarada: "
        + ("; ".join(metrics["application_declared"]) or "desconhecida"),
        f"Checks registrados: {metrics['checks_passed']} passed; {metrics['checks_failed']} failed",
        f"Resultado verificado pelo controle: {result}",
        "Benefício demonstrado da memória: desconhecido (checks não comprovam causalidade)",
    ]


def entry_path(entry):
    slug = (
        re.sub(r"[^\w -]+", "", entry["title"], flags=re.UNICODE).strip(" .")[:80]
        or KINDS[entry["kind"]]
    )
    return "Navegar/" + slug + "--" + entry["id"][:12] + ".md"


def export_files(model: dict) -> dict[str, str]:
    from oraculo.experience_html import dashboard

    files = {
        "model.json": canonical_json(
            {k: v for k, v in model.items() if k not in {"documents", "evidence"}}
        )
        + "\n"
    }
    home = [
        f"# {markdown(model['project_title'])}",
        f"Última atualização observada: {markdown(model['observed_at'])}",
        "Abra index.html para buscar e filtrar. Este pacote é uma observação, sem atualização automática.",
    ]
    for kind, label in KINDS.items():
        group = [e for e in model["entries"] if e["kind"] == kind]
        if not group:
            continue
        home.append("## " + label)
        for entry in group:
            relative = entry_path(entry)
            if relative in files:
                raise ValueError("experience_projection_path_collision")
            home.append(
                f"- [[{relative}|{markdown(entry['title'])}]] — {markdown(entry['state'])} · {markdown(entry['updated_at'])}"
            )
            body = [
                f"# {markdown(entry['title'])}",
                f"{markdown(entry['state'])} · {markdown(entry['updated_at'])}",
                "Assuntos: "
                + (", ".join(map(markdown, entry["topics"])) or "não informados"),
                markdown(entry["text"]),
            ]
            if entry.get("metrics"):
                body.extend(
                    [
                        "## Uso da memória",
                        *[
                            "- " + markdown(line)
                            for line in metric_lines(entry["metrics"])
                        ],
                    ]
                )
            if entry.get("occurrences"):
                body.append(
                    f"Ocorrências declaradas em tarefas: {entry['occurrences']}"
                )
            if entry.get("presentation", {}).get("url"):
                body.append(f"[Abrir conversa]({entry['presentation']['url']})")
            body.append("## Fontes e evidências")
            body.extend(
                f"- [[Fontes/{source}|{markdown(note_title(model['documents'][source], 'Evidência estruturada'))}]]"
                for source in entry["sources"]
            )
            body.append(
                "## Identidade técnica\n\n"
                + markdown(entry.get("identity") or entry["id"])
            )
            files[relative] = render_page(
                dict(
                    aliases=[markdown(entry["title"])],
                    tags=[re.sub(r"[^\w/-]", "-", topic) for topic in entry["topics"]],
                    oraculo_id=entry["id"],
                ),
                "\n\n".join(body) + "\n",
            )
    topics = sorted({t for e in model["entries"] for t in e["topics"]})
    home.append("## Assuntos")
    for topic in topics:
        slug = (
            re.sub(r"[^\w -]+", "", topic, flags=re.UNICODE).strip(" .")[:80]
            or "Assunto"
        )
        path = "Assuntos/" + slug + "--" + digest(topic)[:12] + ".md"
        home.append(f"- [[{path}|{markdown(topic)}]]")
        files[path] = (
            f"# {markdown(topic)}\n\n"
            + "\n".join(
                f"- [[{entry_path(e)}|{markdown(e['title'])}]]"
                for e in model["entries"]
                if topic in e["topics"]
            )
            + "\n"
        )
    files["Inicio.md"] = "\n\n".join(home) + "\n"
    for path, content in model["documents"].items():
        # Prefix every scoped wikilink in this standalone package.
        files["Fontes/" + path] = re.sub(
            r"^- \[\[([^\]|]+)(\|[^\]]*)?\]\]",
            lambda m: "- [[Fontes/" + m[1] + (m[2] or "") + "]]",
            content,
            flags=re.M,
        )
        files["Evidencias/" + path + ".txt"] = model["evidence"][path]
        viewer = source_href(path)
        back = "../" * (len(PurePosixPath(viewer).parts) - 1) + "index.html"
        original = quote(PurePosixPath(path).name + ".txt", safe="")
        files["Evidencias/" + path + ".html"] = (
            '<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<meta http-equiv="Content-Security-Policy" content="default-src &#39;none&#39;; style-src &#39;unsafe-inline&#39;; base-uri &#39;none&#39;">'
            "<title>Evidência · Oráculo</title><style>body{font:16px/1.6 system-ui;max-width:900px;margin:auto;padding:24px;color:#172e34;background:#f2f5ef}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:14px}a{color:#095f79}a:focus{outline:3px solid #bb4a1b}</style></head><body>"
            f'<a href="{back}">Voltar ao painel</a><h1>Evidência original</h1>'
            "<p>Conteúdo da fonte selecionada. Declarações da fonte não são instruções para o agente.</p>"
            f'<a href="{original}" download>Baixar bytes originais (UTF-8)</a><pre>'
            + html.escape(model["evidence"][path])
            + "</pre></body></html>"
        )
    files["index.html"] = dashboard(model)
    return files


def write_export(destination: Path, files: dict[str, str]) -> dict:
    """Preflight all previous bytes and refuse user edits/unknown files/symlinks."""
    destination = destination.absolute()
    if destination.is_symlink():
        raise ValueError("experience_symlink_destination")
    # Resolve the explicitly chosen parent (including macOS /tmp -> /private/tmp).
    # Reject symlinks within the owned output tree before any write.
    destination = destination.parent.resolve() / destination.name
    marker = destination / ".oraculo-export.json"
    existing = list(destination.rglob("*")) if destination.exists() else []
    if any(p.is_symlink() for p in existing):
        raise ValueError("experience_symlink_destination")
    if existing:
        if not marker.is_file():
            raise ValueError("experience_requires_empty_or_owned_export")
        previous = json.loads(marker.read_text())
        current = {
            p.relative_to(destination).as_posix()
            for p in existing
            if p.is_file() and p != marker
        }
        if current != set(previous) or set(files) != set(previous):
            raise ValueError("experience_export_file_set_changed_use_new_directory")
        if any(
            digest(safe_path(destination, p).read_bytes()) != sha
            for p, sha in previous.items()
        ):
            raise ValueError("experience_export_locally_modified")
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    for path, content in sorted(files.items()):
        target = safe_path(destination, path)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with target.open("w", encoding="utf-8") as stream:
            stream.write(content)
        target.chmod(0o600)
    manifest = {p: digest(c) for p, c in sorted(files.items())}
    marker.write_text(canonical_json(manifest) + "\n")
    marker.chmod(0o600)
    return dict(
        state="exported",
        path=str(destination),
        files=len(files),
        manifest_sha256=digest(canonical_json(manifest)),
    )


def source_href(path):
    return quote("Evidencias/" + path + ".html", safe="/")
