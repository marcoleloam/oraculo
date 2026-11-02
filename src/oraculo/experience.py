"""One scoped presentation model for Obsidian, HTML and CLI. No live I/O here."""

from __future__ import annotations

import html
import json
import posixpath
import re
import unicodedata
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
from oraculo.presentation import markdown, plain, task_tracking, task_view, timestamp
from oraculo.protocol import canonical_json

KINDS = {
    "concept": "Assunto",
    "entity": "Entidade",
    "source": "Memória",
    "raw": "Evidência",
    "task": "Tarefa",
    "fix": "Fix",
    "decision": "Decisão",
    "lesson": "Aprendizado",
    "error": "Erro",
}

NO_WAVE = "Sem onda informada"


def content_texts(value) -> list[str]:
    """Content is lossless; only labels and excerpts may be bounded."""
    values = value if isinstance(value, list) else [value]
    return [v for v in values if isinstance(v, str) and v.strip()]


def hierarchy(metadata, title, topics=()):
    """Explicit fields win; never infer a wave from chronology or errors."""
    for key in ("wave", "onda"):
        value = metadata.get(key)
        if isinstance(value, (str, int)) and not isinstance(value, bool):
            label = plain(value)
            if label:
                if re.fullmatch(r"\d+", label):
                    label = "Onda " + label
                return label, "metadado " + key
    for topic in topics:
        match = re.fullmatch(r"onda[\s/_:-]+(\d+)", topic, re.I)
        if match:
            return "Onda " + match[1], "tag/topic"
    match = re.search(r"(?<!\w)onda\s+(\d+)(?!\w)", title, re.I)
    return ("Onda " + match[1], "título") if match else (NO_WAVE, "ausente")


def entry_kind(metadata, title, fallback):
    explicit = metadata.get("kind") or metadata.get("type")
    if not isinstance(explicit, str):
        explicit = None
    if (
        explicit in {"fix", "correction", "correção"}
        or re.match(r"^(?:fix|correção)\s*(?::|—|–|-|\])\s*\S", title, re.I)
        or re.search(r"(?<!\w)fix(?=\s*(?:[:—–-]|onda\s+\d+\b|$))", title, re.I)
    ):
        return "fix"
    return explicit if explicit in {"task", "fix"} else fallback


def structured_reading(report):
    """Human sections are a projection; the complete JSON stays in evidence."""
    evidence = report.get("evidence")
    value = report | (evidence if isinstance(evidence, dict) else {})
    sections = []
    for key, label in (
        ("summary", "Resumo"),
        ("content", "Conteúdo"),
        ("body", "Texto"),
        ("text", "Texto"),
        ("decisions", "Decisões"),
        ("lessons", "Aprendizados"),
        ("changes", "Mudanças"),
        ("results", "Resultados"),
        ("errors", "Erros registrados"),
        ("limitations", "Limitações"),
        ("pending", "Pendências"),
        ("next_steps", "Próximos passos"),
    ):
        if key in value:
            content = value[key]
            text = (
                "\n\n".join(content_texts(content))
                if isinstance(content, (str, list))
                else json.dumps(content, ensure_ascii=False, indent=2)
            )
            if text:
                sections.append(label + "\n\n" + text)
    return (
        "\n\n".join(sections)
        or "Fonte estruturada sem resumo. O conteúdo integral está em Evidências."
    )


def texts(value) -> list[str]:
    if value is None:
        return []
    values = value if isinstance(value, list) else [value]
    return [plain(v, 2000) for v in values if isinstance(v, str) and v.strip()]


def note_title(content: str, fallback="Memória sem título") -> str:
    try:
        structured = json.loads(content)
    except (ValueError, TypeError):
        structured = None
    if isinstance(structured, dict):
        evidence = structured.get("evidence")
        evidence = evidence if isinstance(evidence, dict) else {}
        for value in (
            structured.get("title"),
            evidence.get("title"),
            structured.get("summary"),
            evidence.get("summary"),
        ):
            if isinstance(value, str) and value.strip():
                return plain(value)
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


def reading_text(body, path, docs):
    def label(match):
        target, _, alias = match[1].partition("|")
        try:
            resolved = resolve_link(target, set(docs), path)
        except MemoryError:
            return alias or "Referência indisponível neste recorte"
        return alias or note_title(docs[resolved], "Fonte sem título")

    return re.sub(r"\[\[([^\]]+)\]\]", label, html.unescape(body))


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
        try:
            report = json.loads(content)
        except (ValueError, TypeError):
            report = {}
        if "/raw/" in path:
            if (
                isinstance(report, dict)
                and report.get("project_id") == project
                and isinstance(report.get("cycle_id"), str)
            ):
                reports[report["cycle_id"]] = (report, path)
        try:
            meta, body = parse_page(content)
        except MemoryError:
            meta, body = {}, content
        kind = "raw" if "/raw/" in path else meta.get("type", "source")
        if kind not in KINDS:
            kind = "source"
        evidence = report.get("evidence", {}) if isinstance(report, dict) else {}
        evidence = evidence if isinstance(evidence, dict) else {}
        metadata = (report if isinstance(report, dict) else {}) | meta | evidence
        title = note_title(
            content, "Fonte sem título" if kind == "raw" else "Nota sem título"
        )
        topics = texts(meta.get("tags")) + texts(metadata.get("topics"))
        wave, basis = hierarchy(metadata, title, topics)
        kind = entry_kind(metadata, title, kind)
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
                title=title,
                topics=topics,
                wave=wave,
                wave_basis=basis,
                document_path=path,
                updated_at=timestamp(str(meta.get("updated"))),
                state="Revisada"
                if meta.get("reviewed") is True
                else "Revisão não confirmada",
                text=structured_reading(report)
                if isinstance(report, dict) and report
                else reading_text(body, path, docs),
                structured=bool(isinstance(report, dict) and report),
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
        metadata = evidence | task
        topics = list(
            dict.fromkeys(topics + texts(task.get("topics")) + texts(task.get("tags")))
        )
        source_title = (
            task.get("title") if isinstance(task.get("title"), str) else view["title"]
        )
        wave, basis = hierarchy(metadata, source_title, topics)
        task_kind = entry_kind(metadata, source_title, "task")
        entries.append(
            dict(
                id=digest("task:" + ident),
                identity=ident,
                kind=task_kind,
                title=view["title"],
                topics=topics,
                wave=wave,
                wave_basis=basis,
                updated_at=view["updated_at"],
                state=view["phase"],
                text="\n\n".join(content_texts(evidence.get("summary")))
                or view["activity"],
                decisions=content_texts(evidence.get("decisions")),
                lessons=content_texts(evidence.get("lessons")),
                sources=sources,
                metrics=metrics,
                presentation=view,
                tracking=task_tracking(task),
            )
        )
        for field, kind in (
            ("decisions", "decision"),
            ("lessons", "lesson"),
            ("errors", "error"),
        ):
            for index, value in enumerate(content_texts(evidence.get(field))):
                entry = dict(
                    id=digest(f"{ident}:{kind}:{index}"),
                    kind=kind,
                    title=value[:140],
                    text=value,
                    topics=topics,
                    wave=wave,
                    wave_basis=basis,
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
    relate_tasks(entries, snapshot.get("tasks", []), project)
    # All source documents have an entry, even raw, index, log and legacy notes.
    # A hierarchy is a reading aid; it never changes canonical paths or identity.
    waves = sorted({e["wave"] for e in entries}, key=wave_sort)
    model = dict(
        schema_version=1,
        project_id=project,
        project_title=name,
        demonstration=snapshot.get("demonstration") is True,
        waves=waves,
        observed_at=plain(snapshot.get("observed_at")) or "desconhecida",
        entries=entries,
        documents={p: _clean_document(c, p, paths) for p, c in sorted(docs.items())},
        evidence={p: c for p, c in sorted(docs.items())},
    )
    assign_reading_paths(model)
    return model


def relate_tasks(entries, source_tasks, project):
    """Only declared parent IDs or unique native thread relations create edges."""
    tasks = {e["identity"]: e for e in entries if e.get("identity")}
    selected = {
        t["id"]: t
        for t in source_tasks
        if (t.get("project") or {}).get("project_id") == project
    }
    threads = {}
    for ident, task in selected.items():
        thread = (task.get("child") or {}).get("childThreadId")
        if isinstance(thread, str) and thread:
            threads.setdefault(thread, []).append(ident)
    parents = {}
    for ident, entry in tasks.items():
        task = selected[ident]
        parent = task.get("parent_task_id")
        basis = "parent_task_id" if parent is not None else "ausente"
        if parent is None:
            origin = (task.get("origin") or {}).get("thread_id")
            candidates = (
                [p for p in threads.get(origin, []) if p != ident]
                if isinstance(origin, str)
                else []
            )
            if len(candidates) == 1:
                parent, basis = candidates[0], "relação nativa de threads"
            elif candidates:
                basis = "relação nativa ambígua"
        entry["relationship_status"] = (
            "none"
            if parent is None
            else "linked"
            if isinstance(parent, str) and parent in tasks
            else "orphan"
        )
        entry["relationship_basis"] = basis
        if entry["relationship_status"] == "linked":
            parents[ident] = parent
    # Remove every edge participating in a cycle. Keep nodes readable as roots.
    cyclic = set()
    for ident in tasks:
        chain, cursor = [], ident
        while cursor in parents:
            if cursor in chain:
                cyclic.update(chain[chain.index(cursor) :])
                break
            chain.append(cursor)
            cursor = parents[cursor]
    for ident in cyclic:
        parents.pop(ident, None)
        tasks[ident]["relationship_status"] = "cycle"
    for ident, entry in tasks.items():
        parent = parents.get(ident)
        entry["parent_entry_id"] = tasks[parent]["id"] if parent else None
        entry["subtask_ids"] = [
            tasks[c]["id"] for c in tasks if parents.get(c) == ident
        ]
        ancestors, cursor = [], ident
        while cursor in parents:
            cursor = parents[cursor]
            ancestors.insert(0, tasks[cursor]["id"])
        entry["ancestor_ids"] = ancestors
    by_identity = tasks
    for entry in entries:
        if entry.get("task_id") in by_identity:
            entry["related_task_id"] = by_identity[entry["task_id"]]["id"]


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


def wave_sort(label):
    parts = re.split(r"(\d+)", label.casefold())
    return (label == NO_WAVE, [(1, int(p)) if p.isdigit() else (0, p) for p in parts])


def friendly_name(value, fallback="Sem título"):
    # macOS compares names case-insensitively and with Unicode decomposition.
    value = unicodedata.normalize("NFC", value)
    value = re.sub(r'[/\\:*?"<>|#\[\]\^\x00-\x1f\x7f]', " ", value)
    value = " ".join(value.split()).strip(" .") or fallback
    while len(value.encode("utf-8")) > 120:
        value = value[:-1]
    return value.rstrip(" .") or fallback


def _name_key(value):
    return unicodedata.normalize("NFD", value).casefold()


def _allocate_names(items):
    """Stable even on case-insensitive APFS; suffix only actual collisions."""
    groups, result, used = {}, {}, set()
    for identity, title in sorted(items):
        name = friendly_name(title)
        groups.setdefault(_name_key(name), []).append((identity, name))
    # Reserve natural names before choosing any suffixed variant.
    for group in groups.values():
        if len(group) == 1:
            identity, name = group[0]
            result[identity] = name
            used.add(_name_key(name))
    for group in groups.values():
        if len(group) < 2:
            continue
        for identity, name in group:
            size = 10
            candidate = name + "--" + digest(identity)[:size]
            while _name_key(candidate) in used:
                size += 2
                candidate = name + "--" + digest(identity)[:size]
                if size > 64:
                    raise ValueError("experience_projection_path_collision")
            result[identity] = candidate
            used.add(_name_key(candidate))
    return result


def assign_reading_paths(model):
    root = "Projetos/" + friendly_name(model["project_title"])
    waves = _allocate_names((w, w) for w in model["waves"])
    model["project_path"] = root + "/Projeto.md"
    model["task_index_path"] = root + "/Tarefas.md"
    model["wave_paths"] = {
        w: root + "/Ondas/" + n + "/Onda.md" for w, n in waves.items()
    }
    buckets = {}
    for entry in model["entries"]:
        if entry.get("document_path"):
            folder = (
                "Biblioteca/Fontes"
                if "/raw/" in entry["document_path"]
                else "Biblioteca/Notas"
            )
        else:
            folder = root
            folder += (
                "/"
                + {
                    "task": "Tarefas",
                    "fix": "Fixes",
                    "decision": "Registros/Decisões",
                    "lesson": "Registros/Aprendizados",
                    "error": "Registros/Erros",
                }[entry["kind"]]
            )
        buckets.setdefault(folder, []).append(entry)
    for folder, entries in buckets.items():
        names = _allocate_names((e["id"], e["title"]) for e in entries)
        for entry in entries:
            entry["reading_path"] = folder + "/" + names[entry["id"]] + ".md"


def entry_path(entry):
    return entry["reading_path"]


def _literal_markdown(value):
    # Indentation displays arbitrary source text without active embeds or HTML.
    return "\n".join("    " + line for line in value.splitlines()) + "\n"


def _reading_markdown(value):
    return re.sub(r"([\\`*_{}\[\]()#!|>~])", r"\\\1", html.escape(value))


def export_files(model: dict) -> dict[str, str]:
    from oraculo.experience_html import dashboard

    files = {
        "model.json": canonical_json(
            {k: v for k, v in model.items() if k not in {"documents", "evidence"}}
        )
        + "\n"
    }
    by_source = {
        e["document_path"]: e for e in model["entries"] if e.get("document_path")
    }
    by_id = {e["id"]: e for e in model["entries"]}
    tasks = [e for e in model["entries"] if e.get("identity")]

    def link(entry):
        return f"[[{entry_path(entry)}|{markdown(entry['title'])}]]"

    def index(title, lines):
        return (
            f"# {markdown(title)}\n\n[[Inicio|Início]] · [[Biblioteca/Biblioteca|Biblioteca completa]]\n\n"
            + "\n\n".join(lines)
            + "\n"
        )

    demo = (
        ["**Demonstração sintética — todos os dados deste exemplo são fictícios.**"]
        if model.get("demonstration")
        else []
    )
    home = [
        *demo,
        "Explore Projeto → Tarefas → Subtarefas. Ondas são um agrupamento opcional.",
        f"Observado em: {markdown(model['observed_at'])}. Snapshot offline, sem atualização automática.",
        f"[[{model['project_path']}|Abrir projeto]]",
        f"[[{model['task_index_path']}|Todas as tarefas e subtarefas]]",
        "## Tarefas",
    ]

    def task_lines(entry, depth=0):
        lines = ["  " * depth + "- " + link(entry) + " · " + KINDS[entry["kind"]]]
        for child in entry["subtask_ids"]:
            lines += task_lines(by_id[child], depth + 1)
        return lines

    task_index = [*demo]
    for task in tasks:
        if not task.get("parent_entry_id"):
            task_index.extend(task_lines(task))
    if not tasks:
        task_index.append(
            "Sem tarefas registradas neste snapshot. Consulte a Biblioteca para o legado de memória."
        )
    files[model["task_index_path"]] = index(
        "Tarefas · " + model["project_title"], task_index
    )
    home += task_index + ["## Ondas (opcional)"]
    for wave, path in model["wave_paths"].items():
        group = [
            e for e in model["entries"] if e["wave"] == wave and not e.get("task_id")
        ]
        home.append(f"- [[{path}|{markdown(wave)}]] — {len(group)} entradas")
        lines = [*demo]
        for kind, label in KINDS.items():
            entries = [e for e in group if e["kind"] == kind]
            if entries:
                lines += [
                    "## " + label,
                    "\n".join(f"- {link(e)} — {markdown(e['state'])}" for e in entries),
                ]
        if wave == NO_WAVE:
            lines.insert(
                0, "Este legado não informa onda; nenhuma sequência foi inferida."
            )
        files[path] = index(wave, lines)
    home += [
        "## Biblioteca completa",
        f"[[Biblioteca/Biblioteca|Todas as {len(by_source)} fontes e notas do recorte]]",
    ]
    files["Inicio.md"] = index(model["project_title"], home)
    files[model["project_path"]] = index(model["project_title"], home)
    files["Biblioteca/Biblioteca.md"] = index(
        "Biblioteca completa",
        [
            *demo,
            "Inclui fontes isoladas, índices, notas legadas e conhecimento compartilhado selecionado.",
            "\n".join(
                f"- {link(e)} · {KINDS[e['kind']]} · {markdown(e['wave'])}"
                for e in by_source.values()
            ),
        ],
    )
    for entry in model["entries"]:
        body = [
            f"# {markdown(entry['title'])}",
            "[[Inicio|Início]] · [[Biblioteca/Biblioteca|Biblioteca completa]]",
            f"Projeto: [[{model['project_path']}|{markdown(model['project_title'])}]] · {KINDS[entry['kind']]} · {markdown(entry['state'])}",
            f"Atualização: {markdown(entry['updated_at'])}",
            "## Conteúdo",
            _reading_markdown(entry["text"]),
        ]
        if entry.get("identity"):
            if entry.get("parent_entry_id"):
                body += ["Tarefa pai: " + link(by_id[entry["parent_entry_id"]])]
            body += [
                "## Subtarefas",
                "\n".join("- " + link(by_id[c]) for c in entry["subtask_ids"])
                or "Sem subtarefas registradas.",
            ]
            if entry["relationship_status"] in {"orphan", "cycle"}:
                body.append(
                    "Relação de tarefa pai indisponível neste recorte ou inconsistente; registro preservado sem inferir parentesco."
                )
        for key, label in (("decisions", "Decisões"), ("lessons", "Aprendizados")):
            if entry.get(key):
                body += ["## " + label, _reading_markdown("\n\n".join(entry[key]))]
        children = [
            e
            for e in model["entries"]
            if e.get("task_id") == entry.get("identity") and entry.get("identity")
        ]
        if children:
            body += [
                "## Registros da tarefa",
                "\n".join("- " + link(e) for e in children),
            ]
        body += [
            "## Evidências",
            "\n".join("- " + link(by_source[p]) for p in entry["sources"]),
        ]
        if entry.get("unavailable_sources"):
            body.append(
                f"{entry['unavailable_sources']} referências indisponíveis neste recorte."
            )
        if entry.get("presentation", {}).get("url"):
            body.append(f"[Abrir conversa]({entry['presentation']['url']})")
        technical = [
            entry.get("identity") or entry["id"],
            "Origem da onda: " + entry["wave_basis"],
        ]
        if entry.get("identity"):
            technical += [
                "Relação: " + entry["relationship_basis"],
                "Estado da relação: " + entry["relationship_status"],
            ]
        if entry.get("document_path"):
            path = entry["document_path"]
            technical += [
                "Caminho canônico: " + path,
                "SHA-256: " + digest(model["evidence"][path]),
            ]
            original = posixpath.relpath(
                "Evidencias/" + path + ".txt", posixpath.dirname(entry_path(entry))
            )
            body.append(f"[Texto original preservado]({quote(original, safe='/')})")
            if entry.get("structured"):
                body += [
                    "### Conteúdo original integral (JSON)",
                    _literal_markdown(model["evidence"][path]),
                ]
        if entry.get("metrics"):
            technical += metric_lines(entry["metrics"])
        body += ["## Identidade técnica", _literal_markdown("\n".join(technical))]
        files[entry_path(entry)] = render_page(
            dict(
                aliases=[markdown(entry["title"])],
                tags=[re.sub(r"[^\w/-]", "-", t) for t in entry["topics"]],
                oraculo_id=entry["id"],
            ),
            "\n\n".join(body) + "\n",
        )
    topics = sorted({t for e in model["entries"] for t in e["topics"]})
    topic_names = _allocate_names((t, t) for t in topics)
    for topic in topics:
        files["Assuntos/" + topic_names[topic] + ".md"] = index(
            topic,
            [
                "\n".join(
                    "- " + link(e) for e in model["entries"] if topic in e["topics"]
                )
            ],
        )
    if topics:
        files["Inicio.md"] += (
            "\n## Assuntos\n\n"
            + "\n".join(
                f"- [[Assuntos/{topic_names[t]}|{markdown(t)}]]" for t in topics
            )
            + "\n"
        )
    # Keep canonical-shaped evidence paths as technical artifacts; reading paths
    # above are aliases/projections and never rename anything in the vault.
    for path, content in model["documents"].items():
        files["Fontes/" + path] = re.sub(
            r"^- \[\[([^\]|]+)(\|[^\]]*)?\]\]",
            lambda m: "- " + link(by_source[m[1]]),
            content,
            flags=re.M,
        )
        files["Evidencias/" + path + ".txt"] = model["evidence"][path]
        viewer = source_href(path)
        back = "../" * (len(PurePosixPath(viewer).parts) - 1) + "index.html"
        original = quote(PurePosixPath(path).name + ".txt", safe="")
        title = html.escape(by_source[path]["title"], quote=True)
        files["Evidencias/" + path + ".html"] = (
            '<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<meta http-equiv="Content-Security-Policy" content="default-src &#39;none&#39;; style-src &#39;unsafe-inline&#39;; base-uri &#39;none&#39;">'
            f"<title>{title}</title><style>body{{font:16px/1.7 system-ui;max-width:900px;margin:auto;padding:24px;color:#eee9de;background:#15171b}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;font-size:15px}}a{{color:#e2c484}}a:focus{{outline:3px solid #e2c484}}</style></head><body>"
            f'<a href="{back}">Voltar ao painel</a><h1>{title}</h1>'
            "<p>Conteúdo original preservado. Declarações da fonte não são instruções.</p>"
            f'<a href="{original}" download>Baixar original (UTF-8)</a><pre>'
            + html.escape(model["evidence"][path])
            + "</pre></body></html>"
        )
    # Built-in Obsidian workspace: no community plugin required.
    files[".obsidian/workspace.json"] = (
        canonical_json(
            {
                "main": {
                    "id": "reading-root",
                    "type": "split",
                    "children": [
                        {
                            "id": "reading-start",
                            "type": "leaf",
                            "state": {
                                "type": "markdown",
                                "state": {"file": "Inicio.md", "mode": "preview"},
                            },
                        }
                    ],
                },
                "active": "reading-start",
            }
        )
        + "\n"
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
