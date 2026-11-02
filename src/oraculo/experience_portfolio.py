"""Offline portfolio projection: many authorized snapshots, one vault."""

from __future__ import annotations

import copy
import json
import posixpath
import re
from pathlib import PurePosixPath
from urllib.parse import quote, unquote

from oraculo.experience import (
    _name_key,
    export_files,
    friendly_name,
    note_title,
    project_model,
)
from oraculo.experience_obsidian import (
    CANVAS_PATH,
    GRAPH_PATH,
    GUIDE_PATH,
    render_canvas,
    render_graph,
    render_guide,
)
from oraculo.memory.schema import (
    MemoryError,
    digest,
    links,
    parse_page,
    project_key,
    resolve_link,
)
from oraculo.presentation import markdown
from oraculo.protocol import canonical_json

_SHARED_ROOT = "Compartilhado"
_ALLOWED_OBSIDIAN = frozenset({".obsidian/workspace.json", ".obsidian/graph.json"})
_WIKI = re.compile(r"\[\[([^\]]+)\]\]")
_MD_LINK = re.compile(r"\[([^\]\n]*)\]\(([^)\n]+)\)")
_HREF = re.compile(r"""href=(["'])(.*?)\1""")


def portfolio_model(
    snapshots: list[dict], project_titles: dict[str, str] | None = None
) -> dict:
    """Project each authorized snapshot. Shared bytes stay shared; task ids do not merge."""
    if not isinstance(snapshots, list):
        raise ValueError("experience_portfolio_snapshots")
    selected = [_accepted_snapshot(item) for item in snapshots]
    ids = [project_id for project_id, _ in selected]
    if len(ids) != len(set(ids)):
        raise ValueError("experience_duplicate_project")
    titles = _titles(ids, project_titles)
    projects = []
    for project_id, snapshot in sorted(selected, key=lambda item: item[0]):
        copied = copy.deepcopy(snapshot)
        if project_id in titles:
            copied["projects"][0]["title"] = titles[project_id]
        model = project_model(copied, project_id)
        projects.append(model)
    roots = _export_roots(projects)
    for project in projects:
        project["export_root"] = roots[project["project_id"]]
    shared = _shared_library(projects)
    links = _canonical_links(projects, shared)
    stamps = [
        project["observed_at"]
        for project in projects
        if project.get("observed_at") not in (None, "", "desconhecida")
    ]
    private_documents = sum(
        1
        for project in projects
        for path in project["evidence"]
        if not str(path).startswith("shared/")
    )
    return dict(
        schema_version=1,
        kind="portfolio",
        observed_at=max(stamps) if stamps else "desconhecida",
        projects=projects,
        shared=shared,
        links=links,
        counts=dict(
            projects=len(projects),
            tasks=sum(
                1
                for project in projects
                for entry in project["entries"]
                if entry.get("identity")
            ),
            documents=private_documents + len(shared["documents"]),
        ),
    )


def portfolio_export_files(model: dict) -> dict[str, str]:
    """One vault: per-project trees, one shared library, no nested Obsidian config."""
    from oraculo.experience_portfolio_html import dashboard

    if model.get("schema_version") != 1 or model.get("kind") != "portfolio":
        raise ValueError("experience_portfolio_model")
    files: dict[str, str] = {}
    shared_files, shared_map = _shared_export(model)
    for path, content in shared_files.items():
        _place(files, path, content)
    for project in model["projects"]:
        exported = _project_export(project, shared_map, model["shared"]["entries"])
        for path, content in exported.items():
            _place(files, path, content)
    _conflict_notes(files, model)
    _place(files, "Inicio.md", _home(model))
    _place(files, "Referencias.md", _references(model))
    _place(files, GUIDE_PATH, render_guide(model))
    _place(files, CANVAS_PATH, render_canvas(model))
    _check_reading_links(files)
    _place(files, "model.json", canonical_json(_projection(model)) + "\n")
    _place(files, "manifest.json", canonical_json(_manifest(model)) + "\n")
    if GRAPH_PATH not in _ALLOWED_OBSIDIAN:
        raise ValueError("experience_nested_obsidian")
    _place(files, GRAPH_PATH, render_graph(model))
    _place(
        files,
        ".obsidian/workspace.json",
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
        + "\n",
    )
    _place(files, "index.html", dashboard(model))
    if any(path not in _ALLOWED_OBSIDIAN and ".obsidian/" in path for path in files):
        raise ValueError("experience_nested_obsidian")
    return files


def _check_reading_links(files: dict[str, str]) -> None:
    """Root reading files must resolve. Indented evidence is not a link."""
    paths = set(files)
    for name in ("Inicio.md", GUIDE_PATH):
        content = files[name]
        active = "\n".join(
            line for line in content.splitlines() if not line.startswith(("    ", "\t"))
        )
        for target in links(active):
            try:
                resolve_link(target, paths, name)
            except MemoryError as exc:
                raise ValueError("experience_unresolved_guide_link") from exc
    canvas = json.loads(files[CANVAS_PATH])
    for node in canvas["nodes"]:
        if node.get("type") == "file" and node.get("file") not in files:
            raise ValueError("experience_unresolved_guide_link")
        if node.get("type") == "text":
            for target in links(node.get("text") or ""):
                try:
                    resolve_link(target, paths, CANVAS_PATH)
                except MemoryError as exc:
                    raise ValueError("experience_unresolved_guide_link") from exc


def _accepted_snapshot(snapshot: dict) -> tuple[str, dict]:
    if not isinstance(snapshot, dict) or snapshot.get("schema_version") != 1:
        raise ValueError("experience_snapshot_version")
    projects = snapshot.get("projects")
    if (
        not isinstance(projects, list)
        or len(projects) != 1
        or not isinstance(projects[0], dict)
    ):
        raise ValueError("experience_select_one_known_project")
    project_id = projects[0].get("id")
    if not isinstance(project_id, str) or not project_id or project_id == "shared":
        raise ValueError("experience_unknown_project")
    declared = snapshot.get("project_id")
    if declared is not None and declared != project_id:
        raise ValueError("experience_unknown_project")
    try:
        project_key(project_id)
    except MemoryError as exc:
        raise ValueError("experience_unknown_project") from exc
    return project_id, snapshot


def _titles(ids: list[str], project_titles: dict[str, str] | None) -> dict[str, str]:
    if project_titles is None:
        return {}
    if not isinstance(project_titles, dict):
        raise ValueError("experience_unknown_project")
    known = set(ids)
    cleaned = {}
    for key, value in project_titles.items():
        if not isinstance(key, str) or key not in known:
            raise ValueError("experience_unknown_project")
        if not isinstance(value, str) or not value.strip():
            raise ValueError("experience_project_name_required")
        cleaned[key] = value
    return cleaned


def _export_roots(projects: list[dict]) -> dict[str, str]:
    """Readable directory names; disambiguate collisions without exposing full ids."""
    used = set()
    roots = {}
    stems = {
        project["project_id"]: friendly_name(project["project_title"])
        for project in projects
    }
    counts = {}
    for stem in stems.values():
        key = _name_key(stem)
        counts[key] = counts.get(key, 0) + 1
    for project in projects:
        stem = stems[project["project_id"]]
        suffix = (
            "--" + digest(project["project_id"])[:12]
            if counts[_name_key(stem)] > 1
            else ""
        )
        leaf = stem + suffix
        if _name_key(leaf) in used:
            leaf = f"{stem}--{digest(project['project_id'])[:32]}"
        if _name_key(leaf) in used or ".." in PurePosixPath(leaf).parts:
            raise ValueError("experience_projection_path_collision")
        used.add(_name_key(leaf))
        roots[project["project_id"]] = "Projetos/" + leaf
    return roots


def _shared_library(projects: list[dict]) -> dict:
    grouped: dict[str, dict[str, dict]] = {}
    for project in projects:
        for path, content in project["evidence"].items():
            if not isinstance(path, str) or not path.startswith("shared/"):
                continue
            version = grouped.setdefault(path, {}).setdefault(
                digest(content),
                {"content": content, "project_ids": [], "observed_at": []},
            )
            if project["project_id"] not in version["project_ids"]:
                version["project_ids"].append(project["project_id"])
            stamp = project.get("observed_at")
            if (
                stamp
                and stamp != "desconhecida"
                and stamp not in version["observed_at"]
            ):
                version["observed_at"].append(stamp)
    documents = {}
    unanimous = []
    conflicts = []
    for path in sorted(grouped):
        versions = grouped[path]
        if len(versions) == 1:
            sha, version = next(iter(versions.items()))
            documents[path] = version["content"]
            unanimous.append(
                dict(
                    path=path,
                    content=version["content"],
                    sha256=sha,
                    project_ids=sorted(version["project_ids"]),
                    observed_at=sorted(version["observed_at"]),
                )
            )
            continue
        conflicts.append(path)
        for sha in sorted(versions):
            version = versions[sha]
            documents[_version_key(path, sha)] = version["content"]
    entries = []
    if unanimous:
        shared = project_model(
            dict(
                schema_version=1,
                observed_at="desconhecida",
                projects=[dict(id="shared", title="Conhecimento compartilhado")],
                documents=[
                    dict(
                        path=item["path"],
                        content=item["content"],
                        sha256=item["sha256"],
                    )
                    for item in unanimous
                ],
                tasks=[],
                cycles=[],
            ),
            "shared",
        )
        info = {item["path"]: item for item in unanimous}
        for entry in shared["entries"]:
            path = entry.get("document_path")
            if not path:
                continue
            item = info[path]
            entries.append(
                dict(entry)
                | dict(
                    scope="shared",
                    sha256=item["sha256"],
                    project_ids=item["project_ids"],
                    observed_at=item["observed_at"],
                    conflict=False,
                    versions=[],
                    vault_path=_SHARED_ROOT + "/" + entry["reading_path"],
                )
            )
    for path in conflicts:
        versions = grouped[path]
        rows = [
            dict(
                sha256=sha,
                project_ids=sorted(versions[sha]["project_ids"]),
                observed_at=sorted(versions[sha]["observed_at"]),
                content_key=_version_key(path, sha),
            )
            for sha in sorted(versions)
        ]
        title = note_title(
            versions[rows[0]["sha256"]]["content"], "Fonte compartilhada"
        )
        reading = "Conflitos/" + digest(path)[:16] + ".md"
        entries.append(
            dict(
                id=digest("shared-conflict:" + path),
                kind="source",
                title="Conflito · " + title,
                document_path=path,
                scope="shared",
                conflict=True,
                sha256=None,
                project_ids=sorted({pid for row in rows for pid in row["project_ids"]}),
                observed_at=sorted(
                    {stamp for row in rows for stamp in row["observed_at"]}
                ),
                versions=rows,
                text=(
                    "Caminho compartilhado com bytes divergentes. "
                    "Nenhuma versão foi promovida ou descartada."
                ),
                reading_path=reading,
                vault_path=_SHARED_ROOT + "/" + reading,
                sources=[],
            )
        )
    return dict(entries=entries, documents=documents)


def _version_key(path: str, sha: str) -> str:
    return f"portfolio-version/{sha}/{path}"


def _canonical_links(projects: list[dict], shared: dict) -> list[dict]:
    owners: dict[str, tuple[str, str]] = {}
    ambiguous = set()

    def own(path: str, owner: tuple[str, str]) -> None:
        if path in ambiguous:
            return
        current = owners.get(path)
        if current is not None and current != owner:
            ambiguous.add(path)
            owners.pop(path, None)
            return
        owners[path] = owner

    for project in projects:
        for entry in project["entries"]:
            path = entry.get("document_path")
            if isinstance(path, str) and not path.startswith("shared/"):
                own(path, (project["project_id"], entry["id"]))
    shared_entries = {
        entry["document_path"]: entry
        for entry in shared["entries"]
        if entry.get("document_path") and not entry.get("conflict")
    }
    for path, entry in shared_entries.items():
        own(path, ("shared", entry["id"]))
    found = set()
    rows = []

    def add(source: tuple[str, str], content: str) -> None:
        for target in _explicit_targets(content):
            path = _canonical_path(target, owners)
            if path is None:
                continue
            dest = owners[path]
            if dest == source or dest[0] == source[0]:
                continue
            row = (
                source[0],
                source[1],
                dest[0],
                dest[1],
                "references",
                "canonical_wikilink",
            )
            if row in found:
                continue
            found.add(row)
            rows.append(
                dict(
                    source_project_id=row[0],
                    source_entry_id=row[1],
                    target_project_id=row[2],
                    target_entry_id=row[3],
                    relation=row[4],
                    basis=row[5],
                )
            )

    for project in projects:
        by_path = {
            entry["document_path"]: entry
            for entry in project["entries"]
            if entry.get("document_path")
        }
        for path, content in sorted(project["evidence"].items()):
            if str(path).startswith("shared/"):
                continue
            entry = by_path.get(path)
            if entry:
                add((project["project_id"], entry["id"]), content)
    for path, entry in sorted(shared_entries.items()):
        add(("shared", entry["id"]), shared["documents"][path])
    return sorted(
        rows,
        key=lambda row: (
            row["source_project_id"],
            row["source_entry_id"],
            row["target_project_id"],
            row["target_entry_id"],
            row["relation"],
            row["basis"],
        ),
    )


def _explicit_targets(content: str) -> list[str]:
    found = list(links(content))
    try:
        meta, _body = parse_page(content)
    except MemoryError:
        meta = None
    if isinstance(meta, dict):
        note = meta.get("source_note")
        if isinstance(note, str) and note.strip():
            extracted = links(note)
            found.extend(extracted or [note.strip()])
    return list(dict.fromkeys(found))


def _canonical_path(target: str, owners: dict[str, tuple[str, str]]) -> str | None:
    if (
        not isinstance(target, str)
        or not target
        or target.startswith(("/", "~"))
        or "\\" in target
        or ".." in PurePosixPath(target).parts
    ):
        return None
    options = []
    if target in owners:
        options.append(target)
    suffixed = target if target.endswith(".md") else target + ".md"
    if suffixed in owners and suffixed not in options:
        options.append(suffixed)
    if len(options) != 1:
        return None
    path = options[0]
    if not (path.startswith("projects/") or path.startswith("shared/")):
        return None
    return path


def _shared_export(model: dict) -> tuple[dict[str, str], dict[str, str]]:
    unanimous = [
        entry
        for entry in model["shared"]["entries"]
        if entry.get("document_path") and not entry.get("conflict")
    ]
    produced: dict[str, str] = {}
    mapping: dict[str, str] = {}
    if unanimous:
        shared = project_model(
            dict(
                schema_version=1,
                observed_at=model.get("observed_at") or "desconhecida",
                projects=[dict(id="shared", title="Conhecimento compartilhado")],
                documents=[
                    dict(
                        path=entry["document_path"],
                        content=model["shared"]["documents"][entry["document_path"]],
                        sha256=entry["sha256"],
                    )
                    for entry in unanimous
                ],
                tasks=[],
                cycles=[],
            ),
            "shared",
        )
        exported = export_files(shared)
        by_path = {
            entry["document_path"]: entry
            for entry in shared["entries"]
            if entry.get("document_path")
        }
        for entry in unanimous:
            # Reading names are allocated inside the shared snapshot, not each project.
            if by_path[entry["document_path"]]["reading_path"] != entry["reading_path"]:
                raise ValueError("experience_projection_path_collision")
            evidence = "Evidencias/" + entry["document_path"] + ".txt"
            if (
                exported[evidence]
                != model["shared"]["documents"][entry["document_path"]]
            ):
                raise ValueError("experience_document_integrity")
        mapping = {
            rel: _SHARED_ROOT + "/" + rel
            for rel in exported
            if not rel.startswith(".obsidian/")
        }
        produced = {
            mapping[rel]: _rewrite(exported[rel], rel, mapping) for rel in mapping
        }
    if f"{_SHARED_ROOT}/Inicio.md" not in produced:
        produced[f"{_SHARED_ROOT}/Inicio.md"] = (
            "# Conhecimento compartilhado\n\n"
            "Nenhuma fonte compartilhada idêntica neste recorte.\n"
        )
    return produced, mapping


def _project_export(
    project: dict, shared_map: dict[str, str], shared_entries: list[dict]
) -> dict[str, str]:
    exported = export_files(project)
    root = project["export_root"]
    reading_dest = {
        entry["document_path"]: entry["vault_path"]
        for entry in shared_entries
        if entry.get("document_path") and not entry.get("conflict")
    }
    hoist = {}
    for entry in project["entries"]:
        path = entry.get("document_path")
        if not isinstance(path, str) or path not in reading_dest:
            continue
        evidence = "Evidencias/" + path + ".txt"
        if (
            evidence not in shared_map
            or exported.get(evidence) != project["evidence"][path]
        ):
            raise ValueError("experience_document_integrity")
        hoist[evidence] = shared_map[evidence]
        for rel in ("Evidencias/" + path + ".html", "Fontes/" + path):
            if rel not in shared_map:
                raise ValueError("experience_document_integrity")
            hoist[rel] = shared_map[rel]
        hoist[entry["reading_path"]] = reading_dest[path]
    mapping = {}
    for rel in exported:
        if rel.startswith(".obsidian/"):
            continue
        mapping[rel] = hoist.get(rel, root + "/" + rel)
    files = {}
    for rel, content in exported.items():
        if rel.startswith(".obsidian/") or rel in hoist:
            continue
        files[mapping[rel]] = _rewrite(content, rel, mapping)
    for path, content in project["evidence"].items():
        if str(path).startswith("shared/") and path in reading_dest:
            continue
        rel = root + "/Evidencias/" + path + ".txt"
        if files.get(rel) != content:
            raise ValueError("experience_document_integrity")
    return files


def _rewrite(content: str, source: str, mapping: dict[str, str]) -> str:
    if source.endswith((".txt", ".json")):
        return content
    if source.endswith(".html"):
        return _HREF.sub(lambda match: _href(match, source, mapping), content)
    if not source.endswith(".md"):
        return content
    return "".join(
        line if _inert(line) else _rewrite_line(line, source, mapping)
        for line in content.splitlines(keepends=True)
    )


def _inert(line: str) -> bool:
    # Indented blocks are preserved evidence, not vault links.
    return line.startswith(("    ", "\t"))


def _rewrite_line(line: str, source: str, mapping: dict[str, str]) -> str:
    def wiki(match: re.Match[str]) -> str:
        raw = match.group(1)
        target, *alias = raw.split("|", 1)
        target, *anchor = target.split("#", 1)
        try:
            resolved = resolve_link(target, set(mapping), source)
        except MemoryError:
            return match.group(0)
        dest = mapping[resolved].removesuffix(".md")
        return (
            "[["
            + dest
            + ("#" + anchor[0] if anchor else "")
            + ("|" + alias[0] if alias else "")
            + "]]"
        )

    updated = _WIKI.sub(wiki, line)

    def markdown_link(match: re.Match[str]) -> str:
        href = match.group(2).strip().strip("<>")
        moved = _retarget(href, source, mapping)
        if moved == href:
            return match.group(0)
        return "[" + match.group(1) + "](" + moved + ")"

    return _MD_LINK.sub(markdown_link, updated)


def _href(match: re.Match[str], source: str, mapping: dict[str, str]) -> str:
    quote_char = match.group(1)
    href = match.group(2)
    moved = _retarget(href, source, mapping)
    if moved == href:
        return match.group(0)
    return f"href={quote_char}{moved}{quote_char}"


def _retarget(href: str, source: str, mapping: dict[str, str]) -> str:
    if href.startswith(
        ("t3-thread:", "http://", "https://", "mailto:", "#", "javascript:", "data:")
    ):
        return href
    raw, sep, anchor = href.partition("#")
    path = unquote(raw.strip().strip("<>"))
    if not path or path.startswith(("/", "~")) or "\\" in path:
        return href
    norm = posixpath.normpath(posixpath.join(posixpath.dirname(source), path))
    if norm not in mapping:
        return href
    dest = mapping[norm]
    origin = mapping.get(source, source)
    relative = posixpath.relpath(dest, posixpath.dirname(origin) or ".")
    if relative.startswith("../"):
        vault = posixpath.normpath(posixpath.join(posixpath.dirname(origin), relative))
        if vault.startswith("../") or PurePosixPath(vault).is_absolute():
            return href
    return quote(relative, safe="/") + (("#" + anchor) if sep else "")


def _conflict_notes(files: dict[str, str], model: dict) -> None:
    notes = []
    for entry in model["shared"]["entries"]:
        if not entry.get("conflict"):
            continue
        lines = [
            f"# {markdown(entry['title'])}",
            "Caminho canônico: " + markdown(entry["document_path"]),
            "Nenhuma versão foi escolhida. Os bytes de cada origem autorizada permanecem separados.",
            "## Versões",
        ]
        for version in entry["versions"]:
            rel = (
                "Conflitos/"
                + version["sha256"]
                + "/Evidencias/"
                + entry["document_path"]
                + ".txt"
            )
            _place(
                files,
                _SHARED_ROOT + "/" + rel,
                model["shared"]["documents"][version["content_key"]],
            )
            href = posixpath.relpath(rel, posixpath.dirname(entry["reading_path"]))
            lines.append(
                "- SHA-256 "
                + version["sha256"]
                + " · projetos "
                + markdown(", ".join(version["project_ids"]))
                + f" · [bytes originais]({quote(href, safe='/')})"
            )
        _place(files, entry["vault_path"], "\n\n".join(lines) + "\n")
        notes.append(entry)
    if not notes:
        return
    home = files.get(f"{_SHARED_ROOT}/Inicio.md", "# Conhecimento compartilhado\n")
    home += (
        "\n## Conflitos de versão\n\n"
        + "\n".join(
            f"- [[{entry['vault_path'].removesuffix('.md')}|{markdown(entry['title'])}]]"
            for entry in notes
        )
        + "\n"
    )
    files[f"{_SHARED_ROOT}/Inicio.md"] = home


def _home(model: dict) -> str:
    lines = [
        "# Todos os projetos",
        "Leitura offline dos projetos autorizados neste recorte. Ondas são um agrupamento opcional e não criam parentesco.",
        f"Observação mais recente: {markdown(model['observed_at'])}. Cada projeto conserva a própria data.",
        "## Projetos",
    ]
    if not model["projects"]:
        lines.append("Nenhum projeto neste recorte.")
    for project in model["projects"]:
        root = project["export_root"]
        task_index = root + "/" + project["task_index_path"].removesuffix(".md")
        lines.append(
            f"- [[{root}/Inicio|{markdown(project['project_title'])}]] · "
            f"[[{task_index}|Tarefas]] · "
            f"{markdown(project['observed_at'])}"
        )
    lines += [
        "## Conhecimento compartilhado",
        f"[[{_SHARED_ROOT}/Inicio|Biblioteca compartilhada]]",
        "## Referências explícitas entre escopos",
        "[[Referencias|Relações canônicas resolvidas]]",
        "## Mapa dos projetos",
        "[[Mapa dos projetos.canvas|Mapa dos projetos]] · [[Guia-do-mapa|Cores, ligações e sugestões a validar]]",
        (
            f"Contagens: {model['counts']['projects']} projetos, "
            f"{model['counts']['tasks']} tarefas, "
            f"{model['counts']['documents']} documentos."
        ),
    ]
    return "\n\n".join(lines) + "\n"


def _references(model: dict) -> str:
    lines = [
        "# Referências entre escopos",
        "Somente ligações canônicas que resolvem nos dois lados deste recorte. Identificadores locais iguais não são parentesco.",
    ]
    if not model["links"]:
        lines.append(
            "Nenhuma referência explícita entre projetos ou com o conhecimento compartilhado."
        )
        return "\n\n".join(lines) + "\n"
    for link in model["links"]:
        source = _entry(model, link["source_project_id"], link["source_entry_id"])
        target = _entry(model, link["target_project_id"], link["target_entry_id"])
        lines.append(
            f"- [[{_vault_link(model, link['source_project_id'], source)}|{markdown(source['title'])}]] "
            f"({markdown(link['source_project_id'])}) → "
            f"[[{_vault_link(model, link['target_project_id'], target)}|{markdown(target['title'])}]] "
            f"({markdown(link['target_project_id'])})"
        )
    return "\n\n".join(lines) + "\n"


def _entry(model: dict, project_id: str, entry_id: str) -> dict:
    pool = (
        model["shared"]["entries"]
        if project_id == "shared"
        else next(
            project["entries"]
            for project in model["projects"]
            if project["project_id"] == project_id
        )
    )
    return next(entry for entry in pool if entry["id"] == entry_id)


def _vault_link(model: dict, project_id: str, entry: dict) -> str:
    if project_id == "shared":
        path = entry["vault_path"]
    else:
        project = next(
            item for item in model["projects"] if item["project_id"] == project_id
        )
        path = project["export_root"] + "/" + entry["reading_path"]
    return path.removesuffix(".md")


def _projection(model: dict) -> dict:
    return dict(
        schema_version=1,
        kind="portfolio",
        observed_at=model["observed_at"],
        projects=[
            {
                key: value
                for key, value in project.items()
                if key not in {"documents", "evidence"}
            }
            for project in model["projects"]
        ],
        shared=dict(entries=model["shared"]["entries"], documents={}),
        links=model["links"],
        counts=model["counts"],
    )


def _manifest(model: dict) -> dict:
    evidence = []
    for project in model["projects"]:
        for path, content in sorted(project["evidence"].items()):
            if str(path).startswith("shared/"):
                continue
            evidence.append(
                dict(scope=project["project_id"], path=path, sha256=digest(content))
            )
    for path, content in sorted(model["shared"]["documents"].items()):
        evidence.append(dict(scope="shared", path=path, sha256=digest(content)))
    return dict(schema_version=1, kind="portfolio_manifest", evidence=evidence)


def _place(files: dict[str, str], path: str, content: str) -> None:
    pure = PurePosixPath(path)
    if (
        not path
        or pure.is_absolute()
        or ".." in pure.parts
        or "\\" in path
        or str(pure) != path
        or any(part in {"", ".", ".."} for part in pure.parts)
    ):
        raise ValueError("experience_unsafe_document_path")
    if ".obsidian" in pure.parts and path not in _ALLOWED_OBSIDIAN:
        raise ValueError("experience_nested_obsidian")
    current = files.get(path)
    if current is not None and current != content:
        raise ValueError("experience_projection_path_collision")
    files[path] = content
