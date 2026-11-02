"""Native Obsidian presentation for one portfolio vault.

Colors, the graph filter and the canvas are a reading aid. They do not add
canonical links, promote shared knowledge, or drop evidence files.
"""

from __future__ import annotations

import re
import unicodedata
from itertools import combinations

from oraculo.memory.schema import digest
from oraculo.presentation import markdown, plain
from oraculo.protocol import canonical_json

CANVAS_PATH = "Mapa dos projetos.canvas"
GUIDE_PATH = "Guia-do-mapa.md"
GRAPH_PATH = ".obsidian/graph.json"
SHARED_ROOT = "Compartilhado"

# Closed list. A title matches only when one of these is a whole token.
# "PostgreSQL" is its own token so it does not count as "SQL" or "Postgres".
VOCABULARY = {
    "api": "API",
    "autenticacao": "autenticação",
    "pdf": "PDF",
    "sql": "SQL",
    "rag": "RAG",
    "ingestao": "ingestão",
    "oauth": "OAuth",
    "webhook": "webhook",
    "graphql": "GraphQL",
    "postgres": "Postgres",
    "postgresql": "PostgreSQL",
    "bigquery": "BigQuery",
}
STOPWORDS = frozenset({"fix", "tarefa", "dev", "onda", "projeto", "testes"})
PER_PAIR_LIMIT = 2
PER_PROJECT_LIMIT = 4
TOTAL_LIMIT = 8
EVIDENCE_LIMIT = 2
CANVAS_CONFIRMED_LIMIT = 6

# Distinct on a dark graph. The name in the guide is the identifier; the hex is secondary.
_PALETTE = (
    {"name": "azul", "hex": "#2F6FDB"},
    {"name": "laranja", "hex": "#E07A2F"},
    {"name": "verde", "hex": "#2E8B57"},
    {"name": "violeta", "hex": "#7A4EAB"},
    {"name": "vermelho", "hex": "#C44536"},
    {"name": "ciano", "hex": "#1F8A8A"},
    {"name": "magenta", "hex": "#B43B73"},
    {"name": "oliva", "hex": "#6F7A2F"},
    {"name": "índigo", "hex": "#3D4C9E"},
    {"name": "terracota", "hex": "#A85A3A"},
    {"name": "petróleo", "hex": "#1F6F6A"},
    {"name": "ameixa", "hex": "#8C4A6A"},
)
SHARED_COLOR = {"name": "cinza", "hex": "#8E939A"}
NEUTRAL_COLOR = {"name": "neutro", "hex": "#5C6570"}
SUGGESTION_COLOR = {"name": "areia", "hex": "#8A8172"}

# Root hubs link every project, so the default graph hides them. The notes stay.
# path:/regex/ is Obsidian search syntax. Fontes/ also matches Biblioteca/Fontes.
GRAPH_SEARCH = " ".join(
    (
        "-path:Fontes/",
        "-path:Evidencias/",
        "-path:/^Inicio\\.md$/",
        "-path:/^Referencias\\.md$/",
        "-path:/^Guia-do-mapa\\.md$/",
    )
)

_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)


def _paint(color: dict) -> dict:
    return {
        "name": color["name"],
        "hex": color["hex"],
        "rgb": int(color["hex"].removeprefix("#"), 16),
    }


def palette() -> tuple[dict, ...]:
    return tuple(_paint(color) for color in _PALETTE)


def shared_color() -> dict:
    return _paint(SHARED_COLOR)


def _hsl_hex(hue: int, saturation: float, lightness: float) -> str:
    h = (hue % 360) / 60
    chroma = (1 - abs(2 * lightness - 1)) * saturation
    x = chroma * (1 - abs(h % 2 - 1))
    m = lightness - chroma / 2
    sector = int(h) % 6
    red, green, blue = (
        (chroma, x, 0),
        (x, chroma, 0),
        (0, chroma, x),
        (0, x, chroma),
        (x, 0, chroma),
        (chroma, 0, x),
    )[sector]
    channels = [round((channel + m) * 255) for channel in (red, green, blue)]
    return "#" + "".join(f"{channel:02X}" for channel in channels)


def assign_project_colors(project_ids: list[str]) -> dict[str, dict]:
    """Distinct colors for one set of ids. Input order and titles are ignored."""
    if len(project_ids) != len(set(project_ids)):
        raise ValueError("experience_duplicate_project")
    paints = palette()
    used_hex = {
        shared_color()["hex"],
        _paint(NEUTRAL_COLOR)["hex"],
        _paint(SUGGESTION_COLOR)["hex"],
    }
    used_names = {item["name"] for item in paints}
    used_names.update(
        (shared_color()["name"], NEUTRAL_COLOR["name"], SUGGESTION_COLOR["name"])
    )
    assigned: dict[str, dict] = {}
    taken: set[int] = set()
    for project_id in sorted(project_ids, key=lambda item: (digest(item), item)):
        slot = int(digest("color:" + project_id)[:8], 16) % len(paints)
        for step in range(len(paints)):
            index = (slot + step) % len(paints)
            if index in taken:
                continue
            taken.add(index)
            assigned[project_id] = paints[index]
            break
        else:
            seed = int(digest("palette:" + project_id)[:8], 16)
            extra = None
            for salt in range(360):
                hue = (seed + salt * 47) % 360
                hex_color = _hsl_hex(hue, 0.55, 0.46)
                name = f"tom {hue}"
                if hex_color in used_hex or name in used_names:
                    continue
                extra = _paint({"name": name, "hex": hex_color})
                break
            if extra is None:
                raise ValueError("experience_color_exhausted")
            assigned[project_id] = extra
        used_hex.add(assigned[project_id]["hex"])
        used_names.add(assigned[project_id]["name"])
    return assigned


def path_query(root: str) -> str:
    """Quote a folder. The trailing slash does not match a longer sibling name."""
    folder = str(root).strip("/") + "/"
    escaped = folder.replace("\\", "\\\\").replace('"', '\\"')
    return f'path:"{escaped}"'


def graph_document(model: dict) -> dict:
    colors = assign_project_colors(
        [project["project_id"] for project in model["projects"]]
    )
    groups = [
        {
            "query": path_query(project["export_root"]),
            "color": {"a": 1, "rgb": colors[project["project_id"]]["rgb"]},
        }
        for project in model["projects"]
    ]
    groups.append(
        {
            "query": path_query(SHARED_ROOT),
            "color": {"a": 1, "rgb": shared_color()["rgb"]},
        }
    )
    # Keys mirror graph.json as saved by Obsidian's built-in graph plugin.
    # The help page describes search groups, not this file's fields.
    return {
        "collapse-filter": False,
        "search": GRAPH_SEARCH,
        "showTags": False,
        "showAttachments": False,
        "hideUnresolved": True,
        "showOrphans": True,
        "collapse-color-groups": False,
        "colorGroups": groups,
        "collapse-display": False,
        "showArrow": False,
        "textFadeMultiplier": 0,
        "nodeSizeMultiplier": 1,
        "lineSizeMultiplier": 1,
        "collapse-forces": True,
        "centerStrength": 0.518713248970312,
        "repelStrength": 10,
        "linkStrength": 1,
        "linkDistance": 250,
        "scale": 1,
        "close": False,
    }


def render_graph(model: dict) -> str:
    return canonical_json(graph_document(model)) + "\n"


def _fold(value: str) -> str:
    normalized = unicodedata.normalize("NFD", value.casefold())
    return "".join(char for char in normalized if unicodedata.category(char) != "Mn")


def _title(entry: dict) -> str:
    title = entry.get("title")
    return title.strip() if isinstance(title, str) else ""


def _topics(entry: dict) -> list[str]:
    raw = entry.get("topics")
    if not isinstance(raw, list):
        return []
    return [item.strip() for item in raw if isinstance(item, str) and item.strip()]


def _topic_key(value: str) -> str:
    return re.sub(r"\s+", " ", _fold(value)).strip()


def _usable_topic(key: str) -> bool:
    return len(key) >= 3 and key not in STOPWORDS


def _title_tokens(title: str) -> set[str]:
    return {_fold(token) for token in _TOKEN.findall(title)}


def _project_map(model: dict) -> dict[str, dict]:
    return {project["project_id"]: project for project in model["projects"]}


def _note_path(project: dict, entry: dict) -> str | None:
    reading = entry.get("reading_path")
    if not isinstance(reading, str) or not reading.endswith(".md"):
        return None
    return project["export_root"] + "/" + reading


def _shared_path(entry: dict) -> str | None:
    path = entry.get("vault_path")
    if not isinstance(path, str) or not path:
        return None
    return path if path.endswith(".md") else path + ".md"


def _bounded(entries: list[dict], project: dict) -> list[dict]:
    ranked = sorted(
        entries,
        key=lambda entry: (entry.get("reading_path") or "", entry.get("id") or ""),
    )
    notes = []
    seen = set()
    for entry in ranked:
        path = _note_path(project, entry)
        if path is None or path in seen:
            continue
        seen.add(path)
        notes.append({"title": _title(entry) or "Nota sem título", "path": path})
        if len(notes) == EVIDENCE_LIMIT:
            break
    return notes


def _reason(term: str, bases: tuple[str, ...]) -> str:
    shown = plain(term, 120)
    if bases == ("topico",):
        return (
            f"Possível porque os dois projetos declaram o tópico {shown}. "
            "Não é uma ligação confirmada."
        )
    if bases == ("titulo",):
        return (
            f"Possível porque o termo {shown} aparece inteiro no título de notas "
            "dos dois projetos. Não é uma ligação confirmada."
        )
    return (
        f"Possível porque os dois projetos declaram o tópico {shown} e o termo "
        "também aparece inteiro em títulos. Não é uma ligação confirmada."
    )


def connection_suggestions(model: dict) -> dict:
    """Title vocabulary or declared topics. Never note bodies, ids, or equal titles."""
    projects = _project_map(model)
    vocabulary: dict[str, dict[str, list[dict]]] = {}
    topics: dict[str, dict[str, list[dict]]] = {}
    originals: dict[tuple[str, str], set[str]] = {}
    for project_id, project in projects.items():
        for entry in project.get("entries") or []:
            if not isinstance(entry, dict):
                continue
            document_path = entry.get("document_path")
            if isinstance(document_path, str) and document_path.startswith("shared/"):
                continue
            title = _title(entry)
            declared = _topics(entry)
            # No title and no declared topic: the body is not a bridge.
            if not title and not declared:
                continue
            for token in _title_tokens(title):
                if token not in VOCABULARY:
                    continue
                vocabulary.setdefault(project_id, {}).setdefault(token, []).append(
                    entry
                )
            for topic in declared:
                key = _topic_key(topic)
                if not _usable_topic(key):
                    continue
                topics.setdefault(project_id, {}).setdefault(key, []).append(entry)
                originals.setdefault((project_id, key), set()).add(topic)
    candidates = []
    ordered_ids = sorted(projects)
    for left_id, right_id in combinations(ordered_ids, 2):
        left_words = vocabulary.get(left_id, {})
        right_words = vocabulary.get(right_id, {})
        left_topics = topics.get(left_id, {})
        right_topics = topics.get(right_id, {})
        keys = (set(left_words) & set(right_words)) | (
            set(left_topics) & set(right_topics)
        )
        for key in keys:
            bases = []
            if key in left_topics and key in right_topics:
                bases.append("topico")
            if key in left_words and key in right_words:
                bases.append("titulo")
            if not bases:
                continue
            basis = tuple(bases)
            if key in VOCABULARY:
                term = VOCABULARY[key]
            else:
                term = sorted(
                    originals.get((left_id, key), set())
                    | originals.get((right_id, key), set())
                )[0]
            support = []
            for side in (left_id, right_id):
                found = []
                if "topico" in basis:
                    found.extend(topics[side][key])
                if "titulo" in basis:
                    found.extend(vocabulary[side][key])
                support.append(_bounded(found, projects[side]))
            if not support[0] or not support[1]:
                continue
            candidates.append(
                {
                    "term": term,
                    "term_key": key,
                    "bases": basis,
                    "reason": _reason(term, basis),
                    "left_project_id": left_id,
                    "right_project_id": right_id,
                    "left_notes": support[0],
                    "right_notes": support[1],
                }
            )
    candidates.sort(
        key=lambda item: (
            item["term_key"],
            item["left_project_id"],
            item["right_project_id"],
        )
    )
    kept = []
    per_pair: dict[tuple[str, str], int] = {}
    per_project: dict[str, int] = {}
    omitted = 0
    for item in candidates:
        pair = (item["left_project_id"], item["right_project_id"])
        if (
            per_pair.get(pair, 0) >= PER_PAIR_LIMIT
            or per_project.get(pair[0], 0) >= PER_PROJECT_LIMIT
            or per_project.get(pair[1], 0) >= PER_PROJECT_LIMIT
            or len(kept) >= TOTAL_LIMIT
        ):
            omitted += 1
            continue
        kept.append(item)
        per_pair[pair] = per_pair.get(pair, 0) + 1
        per_project[pair[0]] = per_project.get(pair[0], 0) + 1
        per_project[pair[1]] = per_project.get(pair[1], 0) + 1
    return {"kept": kept, "omitted": omitted}


def _wiki(path: str, label: str) -> str:
    return "[[" + path.removesuffix(".md") + "|" + markdown(label) + "]]"


def _confirmed_rows(model: dict) -> list[dict]:
    projects = _project_map(model)
    shared = {
        entry["id"]: entry
        for entry in model.get("shared", {}).get("entries") or []
        if isinstance(entry, dict) and entry.get("id")
    }
    rows = []
    for link in model.get("links") or []:
        source_id = link["source_project_id"]
        target_id = link["target_project_id"]
        if source_id == "shared":
            source = shared[link["source_entry_id"]]
            source_path = _shared_path(source)
        else:
            source = next(
                entry
                for entry in projects[source_id]["entries"]
                if entry["id"] == link["source_entry_id"]
            )
            source_path = _note_path(projects[source_id], source)
        if target_id == "shared":
            target = shared[link["target_entry_id"]]
            target_path = _shared_path(target)
        else:
            target = next(
                entry
                for entry in projects[target_id]["entries"]
                if entry["id"] == link["target_entry_id"]
            )
            target_path = _note_path(projects[target_id], target)
        if not source_path or not target_path:
            raise ValueError("experience_unresolved_confirmed_link")
        rows.append(
            {
                "relation": str(link.get("relation") or ""),
                "basis": str(link.get("basis") or ""),
                "source_title": _title(source) or "Nota sem título",
                "target_title": _title(target) or "Nota sem título",
                "source_path": source_path,
                "target_path": target_path,
                "source_project_id": source_id,
                "target_project_id": target_id,
            }
        )
    return rows


def _confirmed_text(rows: list[dict]) -> str:
    if not rows:
        return "Ligações confirmadas: 0."
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["relation"] or "sem tipo"] = (
            counts.get(row["relation"] or "sem tipo", 0) + 1
        )
    parts = [f"{name} {count}" for name, count in sorted(counts.items())]
    shown = min(len(rows), CANVAS_CONFIRMED_LIMIT)
    extra = ""
    if len(rows) > shown:
        extra = (
            f" O mapa desenha {shown} de {len(rows)}. A nota Referencias lista todas."
        )
    return (
        f"Ligações confirmadas: {len(rows)}. Tipos: {', '.join(parts)}. "
        "Somente relações explícitas já resolvidas." + extra
    )


def _nid(*parts: str) -> str:
    return "n" + digest("\n".join(parts))[:16]


def _eid(*parts: str) -> str:
    return "e" + digest("\n".join(parts))[:16]


def _node(
    kind: str, node_id: str, x: int, y: int, width: int, height: int, **extra
) -> dict:
    return {
        "id": node_id,
        "type": kind,
        "x": x,
        "y": y,
        "width": width,
        "height": height,
        **extra,
    }


def _contains(outer: dict, inner: dict, margin: int = 0) -> bool:
    return (
        inner["x"] >= outer["x"] + margin
        and inner["y"] >= outer["y"] + margin
        and inner["x"] + inner["width"] <= outer["x"] + outer["width"] - margin
        and inner["y"] + inner["height"] <= outer["y"] + outer["height"] - margin
    )


def _separated(left: dict, right: dict, gap: int) -> bool:
    return (
        left["x"] + left["width"] + gap <= right["x"]
        or right["x"] + right["width"] + gap <= left["x"]
        or left["y"] + left["height"] + gap <= right["y"]
        or right["y"] + right["height"] + gap <= left["y"]
    )


def _assert_layout(nodes: list[dict]) -> None:
    for index, left in enumerate(nodes):
        for right in nodes[index + 1 :]:
            if _contains(left, right) or _contains(right, left):
                outer, inner = (
                    (left, right) if _contains(left, right) else (right, left)
                )
                if outer["type"] != "group" or inner["type"] == "group":
                    raise ValueError("experience_canvas_overlap")
                if not _contains(outer, inner, margin=12):
                    raise ValueError("experience_canvas_overlap")
            elif not _separated(left, right, 12):
                raise ValueError("experience_canvas_overlap")


def render_canvas(model: dict) -> str:
    colors = assign_project_colors(
        [project["project_id"] for project in model["projects"]]
    )
    suggestions = connection_suggestions(model)
    confirmed = _confirmed_rows(model)
    neutral = _paint(NEUTRAL_COLOR)["hex"]
    suggestion_hex = _paint(SUGGESTION_COLOR)["hex"]
    groups = []
    front = []
    edges = []
    card_w, card_h, card_gap = 260, 120, 20
    pad, label_h = 24, 44
    y = 0
    nav = (
        ("Inicio.md", "Início do cofre"),
        ("Referencias.md", "Referências confirmadas"),
        (f"{SHARED_ROOT}/Inicio.md", "Biblioteca compartilhada"),
        (GUIDE_PATH, "Guia das cores"),
    )
    for index, (path, _label) in enumerate(nav):
        front.append(
            _node(
                "file",
                _nid("nav", path),
                index * (card_w + card_gap),
                y,
                card_w,
                card_h,
                color=neutral,
                file=path,
            )
        )
    y += card_h + 48
    group_w = pad + 3 * card_w + 2 * card_gap + pad
    group_h = pad + label_h + card_h + pad
    columns = 2 if len(model["projects"]) > 1 else 1
    for index, project in enumerate(model["projects"]):
        color = colors[project["project_id"]]
        column = index % columns
        row = index // columns
        origin_x = column * (group_w + 48)
        origin_y = y + row * (group_h + 48)
        label = plain(project["project_title"], 80) + " · " + color["name"]
        groups.append(
            _node(
                "group",
                _nid("project", project["project_id"]),
                origin_x,
                origin_y,
                group_w,
                group_h,
                color=color["hex"],
                label=label,
            )
        )
        cards = (
            (project["export_root"] + "/Inicio.md", "home"),
            (project["export_root"] + "/" + project["task_index_path"], "tasks"),
            (project["export_root"] + "/Biblioteca/Biblioteca.md", "library"),
        )
        for card_index, (path, role) in enumerate(cards):
            front.append(
                _node(
                    "file",
                    _nid("card", project["project_id"], role),
                    origin_x + pad + card_index * (card_w + card_gap),
                    origin_y + pad + label_h,
                    card_w,
                    card_h,
                    color=color["hex"],
                    file=path,
                )
            )
    if model["projects"]:
        rows = (len(model["projects"]) + columns - 1) // columns
        y += rows * group_h + (rows - 1) * 48
    y += 48
    summary = _confirmed_text(confirmed)
    front.append(
        _node(
            "text",
            _nid("confirmed", "summary"),
            0,
            y,
            720,
            100,
            color=neutral,
            text=summary,
        )
    )
    y += 120
    for row in confirmed[:CANVAS_CONFIRMED_LIMIT]:
        source = _node(
            "file",
            _nid(
                "confirmed-source",
                row["source_path"],
                row["target_path"],
                row["relation"],
            ),
            0,
            y,
            card_w,
            card_h,
            color=neutral,
            file=row["source_path"],
        )
        target = _node(
            "file",
            _nid(
                "confirmed-target",
                row["source_path"],
                row["target_path"],
                row["relation"],
            ),
            card_w + 48,
            y,
            card_w,
            card_h,
            color=neutral,
            file=row["target_path"],
        )
        front.extend((source, target))
        edges.append(
            {
                "id": _eid(
                    "confirmed", row["source_path"], row["target_path"], row["relation"]
                ),
                "fromNode": source["id"],
                "fromSide": "right",
                "toNode": target["id"],
                "toSide": "left",
                "toEnd": "arrow",
                "color": neutral,
                "label": "confirmada · " + plain(row["relation"] or "sem tipo", 40),
            }
        )
        y += card_h + 20
    y += 28
    kept = suggestions["kept"]
    if not kept:
        suggestion_w = 688
        suggestion_h = pad + label_h + 120 + pad
        groups.append(
            _node(
                "group",
                _nid("suggestions", "group"),
                0,
                y,
                suggestion_w,
                suggestion_h,
                color=suggestion_hex,
                label="Possível — a validar",
            )
        )
        front.append(
            _node(
                "text",
                _nid("suggestions", "empty"),
                pad,
                y + pad + label_h,
                640,
                120,
                color=suggestion_hex,
                text=(
                    "Nenhuma sugestão. Os títulos e os tópicos declarados "
                    "não sustentam um tema em comum."
                ),
            )
        )
    else:
        text_w, text_h = 420, 180
        note_w, note_h = 240, 110
        prepared = []
        cursor = 0
        for item in kept:
            stack = max(len(item["left_notes"]), len(item["right_notes"]))
            stack_h = stack * note_h + (stack - 1) * 16
            row_h = max(text_h, stack_h)
            prepared.append((item, cursor, row_h))
            cursor += row_h + 20
        cursor -= 20
        suggestion_w = pad + text_w + 20 + note_w + 20 + note_w + pad
        suggestion_h = pad + label_h + cursor + pad
        groups.append(
            _node(
                "group",
                _nid("suggestions", "group"),
                0,
                y,
                suggestion_w,
                suggestion_h,
                color=suggestion_hex,
                label="Possível — a validar",
            )
        )
        for item, cursor, row_h in prepared:
            origin_y = y + pad + label_h + cursor
            lines = [
                "Possível — " + plain(item["term"], 80),
                item["reason"],
                "Notas à esquerda: "
                + "; ".join(plain(note["title"], 80) for note in item["left_notes"]),
                "Notas à direita: "
                + "; ".join(plain(note["title"], 80) for note in item["right_notes"]),
            ]
            front.append(
                _node(
                    "text",
                    _nid(
                        "suggestion-text",
                        item["term_key"],
                        item["left_project_id"],
                        item["right_project_id"],
                    ),
                    pad,
                    origin_y,
                    text_w,
                    text_h if text_h <= row_h else row_h,
                    color=suggestion_hex,
                    text="\n\n".join(lines),
                )
            )
            placed = []
            for side, notes in (
                ("left", item["left_notes"]),
                ("right", item["right_notes"]),
            ):
                side_x = (
                    pad + text_w + 20
                    if side == "left"
                    else pad + text_w + 20 + note_w + 20
                )
                for note_index, note in enumerate(notes):
                    placed.append(
                        _node(
                            "file",
                            _nid(
                                "suggestion-note",
                                item["term_key"],
                                item["left_project_id"],
                                item["right_project_id"],
                                side,
                                note["path"],
                            ),
                            side_x,
                            origin_y + note_index * (note_h + 16),
                            note_w,
                            note_h,
                            color=suggestion_hex,
                            file=note["path"],
                        )
                    )
            front.extend(placed)
            left_node = next(
                node
                for node in placed
                if item["left_notes"][0]["path"] == node["file"]
                and node["x"] == pad + text_w + 20
            )
            right_node = next(
                node
                for node in placed
                if item["right_notes"][0]["path"] == node["file"]
                and node["x"] == pad + text_w + 20 + note_w + 20
            )
            edges.append(
                {
                    "id": _eid(
                        "suggestion",
                        item["term_key"],
                        item["left_project_id"],
                        item["right_project_id"],
                    ),
                    "fromNode": left_node["id"],
                    "fromSide": "right",
                    "toNode": right_node["id"],
                    "toSide": "left",
                    "toEnd": "arrow",
                    "color": suggestion_hex,
                    "label": "Possível — " + plain(item["term"], 40),
                }
            )
    nodes = groups + front
    _assert_layout(nodes)
    return canonical_json({"nodes": nodes, "edges": edges}) + "\n"


def render_guide(model: dict) -> str:
    colors = assign_project_colors(
        [project["project_id"] for project in model["projects"]]
    )
    suggestions = connection_suggestions(model)
    confirmed = _confirmed_rows(model)
    lines = [
        "# Cores, ligações e sugestões",
        "A cor tem nome. Dois projetos com o mesmo título continuam separados pela identidade e pela cor.",
        "O mapa não cria ligação canônica. Sugestão não entra em Referencias nem promove conhecimento compartilhado.",
        "## Cores dos projetos",
    ]
    if not model["projects"]:
        lines.append("Nenhum projeto neste recorte.")
    for project in model["projects"]:
        color = colors[project["project_id"]]
        lines.append(
            "- "
            + _wiki(project["export_root"] + "/Inicio.md", project["project_title"])
            + f" — {color['name']}, {color['hex']}. Identidade: "
            + markdown(project["project_id"])
            + "."
        )
    shared = shared_color()
    lines += [
        f"- {_wiki(SHARED_ROOT + '/Inicio.md', 'Conhecimento compartilhado')} — {shared['name']}, {shared['hex']}. Não é um projeto.",
        "## Gráfico",
        "O arquivo `.obsidian/graph.json` pinta cada pasta de projeto e a pasta compartilhada.",
        "A busca inicial esconde `Fontes/`, `Evidencias/` e as notas-raiz Inicio, Referencias e este guia, que ligariam projetos inteiros entre si.",
        "Os arquivos continuam no cofre. `Biblioteca/Fontes` também sai do gráfico porque o filtro usa o trecho `Fontes/`. Abra a biblioteca pelo mapa para ler essas notas.",
        "A ajuda do Obsidian descreve grupos por busca. As chaves internas de `graph.json` não estão nessa página; o arquivo segue o formato que o plugin nativo de grafo grava. A geração valida a estrutura e os links; confira a aparência na versão do Obsidian usada.",
        "## Ligações confirmadas",
        _confirmed_text(confirmed),
        _wiki("Referencias.md", "Abrir a lista canônica"),
    ]
    if confirmed:
        for row in confirmed:
            lines.append(
                "- "
                + _wiki(row["source_path"], row["source_title"])
                + " → "
                + _wiki(row["target_path"], row["target_title"])
                + f" · tipo {markdown(row['relation'])} · base {markdown(row['basis'])}."
            )
    lines += [
        "## Possível — a validar",
        "Esta seção não é ligação confirmada. O critério é explícito: o mesmo tópico já declarado nos dois projetos, ou a mesma palavra de um vocabulário fechado no título das notas.",
        "Não usa o corpo da nota, o identificador, a pasta, nem o fato de dois títulos serem iguais. Não há nota de confiança.",
        "Uma palavra só conta inteira. SQL não casa com NoSQL nem com PostgreSQL. Hífen separa tokens: no-sql encontra SQL.",
        "Palavras ignoradas mesmo como tópico: " + ", ".join(sorted(STOPWORDS)) + ".",
        "Vocabulário dos títulos: " + ", ".join(VOCABULARY.values()) + ".",
        (
            f"Limite do mapa: {PER_PAIR_LIMIT} temas por par, {PER_PROJECT_LIMIT} por projeto "
            f"e {TOTAL_LIMIT} no total, em ordem alfabética do termo normalizado. "
            f"Até {EVIDENCE_LIMIT} notas de cada lado."
        ),
    ]
    if not suggestions["kept"]:
        lines.append(
            "Nenhuma sugestão. Os títulos e os tópicos declarados não sustentam um tema em comum."
        )
    for item in suggestions["kept"]:
        lines.append("### Possível — " + plain(item["term"], 80))
        lines.append(item["reason"])
        lines.append(
            "Projetos: "
            + markdown(item["left_project_id"])
            + " e "
            + markdown(item["right_project_id"])
            + "."
        )
        for label, notes in (
            ("Um lado", item["left_notes"]),
            ("Outro lado", item["right_notes"]),
        ):
            rendered = ", ".join(_wiki(note["path"], note["title"]) for note in notes)
            lines.append(f"{label}: {rendered}.")
    if suggestions["omitted"]:
        lines.append(
            f"Ficaram de fora {suggestions['omitted']} temas, pela ordem alfabética do termo normalizado. Eles não viram ligação."
        )
    lines += [
        "## Como ler sem depender da cor",
        "No mapa, cada grupo tem o título do projeto e o nome da cor. As três notas de dentro abrem o início, as tarefas e a biblioteca.",
        "Subtarefas estão na nota de tarefas, não como dezenas de cartões.",
        "Linha com rótulo `confirmada` repete uma relação explícita. Linha com rótulo `Possível` fica só na área Possível — a validar.",
    ]
    return "\n\n".join(lines) + "\n"
