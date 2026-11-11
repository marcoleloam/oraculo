"""Budgeted lexical retrieval for one project plus sanitized shared memory.

Scope is applied before ranking, cache lookup, snippets, or link expansion.
The index is derived from those files only and is addressed by a hash of every
scoped path and body. Token budgets use the declared estimator below; they are
not model-tokenizer counts. A page that does not fit is a reference, never a
silent prefix of the raw text.
"""

from __future__ import annotations

import json
import math
import re
import threading
import unicodedata
from collections import OrderedDict
from typing import Any

from .schema import (
    MemoryError,
    corpus,
    digest,
    links,
    parse_page,
    project_key,
    redact_wikilink,
    resolve_link,
)

TOKEN_ESTIMATOR = "utf8_bytes_div_4_v1"
RETRIEVAL_VERSION = "f1-v2"
INDEX_VERSION = "f1-index-v1"
# Chosen from the scale-50 tuning split only: normalized terms already met the
# recall gate, so the simpler variant stays the default. Field weights and BM25
# remain available for comparison and are not selected by the acceptance split.
FROZEN_RANKER = "normalized"
RANKER_SIMPLICITY = ("normalized", "field_weights", "bm25")
FIELD_WEIGHTS = {"title": 5, "alias": 4, "tag": 3, "body": 1}
BM25_K1 = 1.2
BM25_B = 0.75
_CACHE_LIMIT = 4
_STOPWORDS = frozenset(
    """
    a o os as um uma uns umas de da do das dos e ou em no na nos nas por para
    com sem sob sobre entre que qual quais como quando onde foi eram ser sao
    está esta isto isso aquele aquela the of in on for and or to from with
    without what which who is are was were be a an at by into over
    """.split()
)
_CONTROL_LINE = re.compile(
    r"^(?:"
    r"ORACULO_[A-Z0-9_]+"
    r"|marker\s*:"
    r"|task_id\s*:"
    r"|base_sha\s*:"
    r"|schema_version\s*:"
    r"|rules\s*:"
    r"|memory\s*:"
    r"|adapter\s*:"
    r"|origin\s*:"
    r"|ownership\s*:"
    r"|checks\s*:"
    r")"
)
_GOAL_KEYS = ("title", "goal", "objective", "request", "text")
_cache_lock = threading.Lock()
_index_cache: OrderedDict[str, dict[str, Any]] = OrderedDict()
_cache_stats = {"hits": 0, "misses": 0, "evictions": 0}


def estimate_tokens(text: str) -> int:
    """Declared budget estimator: one token per four UTF-8 bytes, rounded up."""
    if not text:
        return 0
    return (len(text.encode("utf-8")) + 3) // 4


def cache_stats() -> dict[str, int]:
    with _cache_lock:
        return {**_cache_stats, "size": len(_index_cache)}


def clear_index_cache() -> None:
    with _cache_lock:
        _index_cache.clear()
        _cache_stats.update(hits=0, misses=0, evictions=0)


def normalize_text(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def tokenize(text: str) -> list[str]:
    return re.findall(r"\w+", normalize_text(text), flags=re.UNICODE)


def useful_terms(text: str) -> list[str]:
    seen: dict[str, None] = {}
    for term in tokenize(text):
        if term in _STOPWORDS:
            continue
        if len(term) < 2 and not term.isdigit():
            continue
        if re.fullmatch(r"[0-9a-f]{32,}", term):
            continue
        seen.setdefault(term, None)
    return list(seen)


def derive_query(query: str) -> str:
    """Keep a goal and title; drop structured envelope boilerplate when present."""
    if not isinstance(query, str):
        raise MemoryError("invalid_query")
    stripped = query.strip()
    if not stripped:
        return ""
    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            parts = [
                payload[key].strip()
                for key in _GOAL_KEYS
                if isinstance(payload.get(key), str) and payload[key].strip()
            ]
            if parts:
                return "\n".join(parts)
    marker = (
        "ORACULO_MANAGED_TASK_V1" in stripped
        or "ORACULO_CONTROL_V1" in stripped
        or bool(re.search(r"(?m)^OWNERSHIP\s*:", stripped))
    )
    if marker:
        titled = re.search(r"(?im)^title\s*:\s*(.+)$", stripped)
        goal = re.search(r"(?im)^(?:goal|objetivo)\s*:\s*(.+)$", stripped)
        parts = [match.group(1).strip() for match in (titled, goal) if match]
        if parts:
            return "\n".join(parts)
    kept = []
    for line in stripped.splitlines() or [stripped]:
        if _CONTROL_LINE.match(line.strip()):
            continue
        if re.fullmatch(r"[0-9a-f]{40,64}", line.strip()):
            continue
        kept.append(line)
    return "\n".join(kept).strip()


def _frontmatter(content: str) -> tuple[dict[str, Any], str]:
    if not content.startswith("---\n"):
        return {}, content
    parts = content.split("\n---\n", 1)
    if len(parts) != 2:
        return {}, content
    meta: dict[str, Any] = {}
    for line in parts[0][4:].splitlines():
        if not line.strip() or line.startswith((" ", "\t", "-")):
            continue
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        meta[key.strip()] = value.strip().strip("\"'")
    return meta, parts[1]


def _list_field(content: str, key: str) -> list[str]:
    """Read a simple YAML list without interpreting the page as instructions."""
    if not content.startswith("---\n"):
        return []
    head = content.split("\n---\n", 1)[0]
    lines = head.splitlines()
    values: list[str] = []
    collecting = False
    for line in lines:
        if not collecting:
            if line.startswith(f"{key}:"):
                rest = line.split(":", 1)[1].strip()
                if rest.startswith("[") and rest.endswith("]"):
                    inner = rest[1:-1].strip()
                    if not inner:
                        return []
                    return [
                        item.strip().strip("\"'")
                        for item in inner.split(",")
                        if item.strip()
                    ]
                collecting = True
            continue
        if line.startswith((" ", "\t")) and line.strip().startswith("-"):
            values.append(line.strip()[1:].strip().strip("\"'"))
            continue
        break
    return values


def _heading(body: str, meta: dict[str, Any]) -> str:
    for line in body.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return str(meta.get("page_id") or "")


def _candidate_paths(files: dict[str, str]) -> list[str]:
    return sorted(
        path
        for path in files
        if "/wiki/" in path
        and "/schema/" not in path
        and not path.endswith("/log.md")
        and not path.endswith("/index.md")
    )


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _parse_page(path: str, content: str) -> dict[str, Any]:
    try:
        meta, body = parse_page(content)
    except MemoryError:
        meta, body = _frontmatter(content)
        aliases = _list_field(content, "aliases")
        tags = _list_field(content, "tags")
    else:
        # Publisher pages use yaml.safe_dump, whose list items start at column 0.
        aliases = _string_list(meta.get("aliases"))
        tags = _string_list(meta.get("tags"))
    title = _heading(body, meta)
    body_terms = tokenize(body)
    return {
        "path": path,
        "content": content,
        "sha256": digest(content),
        "title": title,
        "aliases": aliases,
        "tags": tags,
        "links": links(content),
        "title_terms": set(tokenize(title)),
        "alias_terms": set(tokenize(" ".join(aliases))),
        "tag_terms": set(tokenize(" ".join(tags))),
        "body_terms": body_terms,
        "body_term_set": set(body_terms),
        "all_terms": set(tokenize(content)),
    }


def corpus_fingerprint(files: dict[str, str]) -> str:
    rows = [f"{path}\0{digest(content)}" for path, content in sorted(files.items())]
    return digest("\n".join(rows))


def build_index(project_id: str, files: dict[str, str]) -> dict[str, Any]:
    pages = [_parse_page(path, files[path]) for path in _candidate_paths(files)]
    document_frequency: dict[str, int] = {}
    total_length = 0
    for page in pages:
        total_length += len(page["body_terms"])
        for term in page["body_term_set"]:
            document_frequency[term] = document_frequency.get(term, 0) + 1
    count = len(pages)
    return {
        "version": INDEX_VERSION,
        "project_id": project_id,
        "fingerprint": corpus_fingerprint(files),
        "pages": pages,
        "by_path": {page["path"]: page for page in pages},
        "document_frequency": document_frequency,
        "average_length": (total_length / count) if count else 0.0,
        "page_count": count,
    }


def _cache_get(key: str) -> dict[str, Any] | None:
    with _cache_lock:
        found = _index_cache.get(key)
        if found is None:
            _cache_stats["misses"] += 1
            return None
        _index_cache.move_to_end(key)
        _cache_stats["hits"] += 1
        return found


def _cache_put(key: str, index: dict[str, Any]) -> None:
    with _cache_lock:
        _index_cache[key] = index
        _index_cache.move_to_end(key)
        while len(_index_cache) > _CACHE_LIMIT:
            _index_cache.popitem(last=False)
            _cache_stats["evictions"] += 1


def scoped_index(project_id: str, files: dict[str, str]) -> dict[str, Any]:
    """Cache a derived index. ``files`` must already be the scoped corpus."""
    fingerprint = corpus_fingerprint(files)
    key = project_id + "\0" + fingerprint
    cached = _cache_get(key)
    if cached is not None:
        return cached
    index = build_index(project_id, files)
    _cache_put(key, index)
    return index


def _score_normalized(page: dict[str, Any], terms: list[str]) -> int:
    return sum(1 for term in terms if term in page["all_terms"])


def _score_fields(page: dict[str, Any], terms: list[str]) -> int:
    score = 0
    for term in terms:
        if term in page["title_terms"]:
            score += FIELD_WEIGHTS["title"]
        if term in page["alias_terms"]:
            score += FIELD_WEIGHTS["alias"]
        if term in page["tag_terms"]:
            score += FIELD_WEIGHTS["tag"]
        if term in page["body_term_set"]:
            score += FIELD_WEIGHTS["body"]
    return score


def _bm25(page: dict[str, Any], terms: list[str], index: dict[str, Any]) -> float:
    scores = 0.0
    length = len(page["body_terms"]) or 1
    average = index["average_length"] or 1.0
    count = index["page_count"] or 1
    frequencies: dict[str, int] = {}
    for term in page["body_terms"]:
        frequencies[term] = frequencies.get(term, 0) + 1
    for term in terms:
        tf = frequencies.get(term, 0)
        if not tf:
            continue
        df = index["document_frequency"].get(term, 0)
        idf = math.log(1.0 + (count - df + 0.5) / (df + 0.5))
        denom = tf + BM25_K1 * (1.0 - BM25_B + BM25_B * length / average)
        scores += idf * (tf * (BM25_K1 + 1.0)) / denom
    return scores


def _rank_pages(
    index: dict[str, Any], terms: list[str], ranker: str
) -> list[tuple[float, str]]:
    scored: list[tuple[float, str]] = []
    for page in index["pages"]:
        if ranker == "normalized":
            score: float = _score_normalized(page, terms)
        elif ranker == "field_weights":
            score = _score_fields(page, terms)
        elif ranker == "bm25":
            score = _bm25(page, terms, index)
        else:
            raise MemoryError("invalid_ranker")
        if score > 0:
            scored.append((score, page["path"]))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return scored


def _expand_links(
    ranked: list[str], index: dict[str, Any], allowed: set[str], limit: int
) -> list[str]:
    """Insert one in-scope hop immediately after its source, deterministically."""
    ordered: list[str] = []
    seen: set[str] = set()
    by_path = index["by_path"]
    for path in ranked:
        if path not in seen:
            ordered.append(path)
            seen.add(path)
        page = by_path.get(path)
        if page is None:
            continue
        for target in page["links"]:
            try:
                linked = resolve_link(target, allowed, path)
            except MemoryError:
                continue
            if linked in by_path and linked not in seen:
                ordered.append(linked)
                seen.add(linked)
                break
        if len(ordered) >= limit:
            break
    return ordered[:limit]


def _redact(content: str, allowed: set[str], source: str) -> str:
    for target in links(content):
        try:
            resolve_link(target, allowed, source)
        except MemoryError:
            content = redact_wikilink(content, target)
    return content


def _snippets(
    pages: list[dict[str, Any]], terms: list[str], allowed: set[str]
) -> list[dict[str, str]]:
    """Exact in-scope lines only. Callers must pass pages already scoped."""
    found: list[dict[str, str]] = []
    for page in pages:
        if page["path"] not in allowed:
            continue
        for line in page["content"].splitlines():
            if line.startswith("---") or line.startswith("[["):
                continue
            if not line.strip() or len(line) > 180:
                continue
            if any(term in set(tokenize(line)) for term in terms):
                found.append({"path": page["path"], "quote": line.strip()})
                break
        if len(found) >= 3:
            break
    return found


def _fit_brief(lines: list[str], snippets: list[dict[str, str]], budget: int) -> str:
    chosen_snips: list[dict[str, str]] = []
    for snippet in snippets:
        trial = chosen_snips + [snippet]
        text = _render_brief(lines, trial)
        if estimate_tokens(text) <= budget:
            chosen_snips.append(snippet)
        else:
            break
    # Drop whole trailing lines rather than slicing a title.
    kept = list(lines)
    while kept and estimate_tokens(_render_brief(kept, chosen_snips)) > budget:
        kept.pop()
    text = _render_brief(kept, chosen_snips)
    if estimate_tokens(text) <= budget:
        return text
    fallback = "Brief omitido: o cabeçalho excede brief_tokens."
    if estimate_tokens(fallback) <= budget:
        return fallback
    return ""


def _render_brief(lines: list[str], snippets: list[dict[str, str]]) -> str:
    body = list(lines)
    for snippet in snippets:
        body.append(f"Trecho: {snippet['quote']}")
    return "\n".join(body).strip()


def _pack_details(
    ordered: list[str],
    files: dict[str, str],
    allowed: set[str],
    detail_tokens: int,
) -> tuple[list[dict[str, str]], list[dict[str, Any]], list[dict[str, Any]]]:
    documents: list[dict[str, str]] = []
    oversize: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    remaining = detail_tokens
    for path in ordered:
        raw = files[path]
        for target in links(raw):
            try:
                resolve_link(target, allowed, path)
            except MemoryError:
                findings.append(
                    {
                        "path": path,
                        "code": "out_of_scope_or_broken_link",
                        "target": target,
                    }
                )
        content = _redact(raw, allowed, path)
        tokens = estimate_tokens(content)
        reference = {
            "path": path,
            "sha256": digest(raw),
            "tokens": tokens,
            "bytes": len(raw.encode("utf-8")),
            "coverage": 0,
        }
        if tokens > detail_tokens:
            oversize.append({**reference, "reason": "exceeds_detail_budget"})
            continue
        if tokens > remaining:
            oversize.append({**reference, "reason": "exceeds_remaining_budget"})
            continue
        documents.append({"path": path, "content": content})
        remaining -= tokens
    return documents, oversize, findings


def _envelope(
    context: Any,
    project_id: str,
    *,
    documents: list[dict[str, str]],
    findings: list[dict[str, Any]],
    selection: dict[str, Any],
    brief: str,
    index_first: bool,
) -> dict[str, Any]:
    return {
        "project_id": project_id,
        "project_key": project_key(project_id),
        "corpus_path": corpus(project_id),
        "page_prefix": project_key(project_id) + "--",
        "allowed_roots": list(dict.fromkeys((corpus(project_id), "shared"))),
        "documents": documents,
        "findings": findings,
        "index_first": index_first,
        "selection": selection,
        "brief": brief,
    }


def _validate_budgets(limit: int, detail_tokens: int, brief_tokens: int) -> None:
    if not 1 <= limit <= 100:
        raise MemoryError("invalid_context_limit")
    if (
        not isinstance(detail_tokens, int)
        or not isinstance(brief_tokens, int)
        or detail_tokens < 0
        or brief_tokens < 0
        or detail_tokens > 200_000
        or brief_tokens > 20_000
    ):
        raise MemoryError("invalid_context_budget")


def retrieve(
    context: Any,
    project_id: str,
    query: str,
    *,
    limit: int = 20,
    detail_tokens: int = 6000,
    brief_tokens: int = 800,
    strict_abstain: bool = True,
    ranker: str | None = None,
    force_fallback: bool = False,
) -> dict[str, Any]:
    """Return a ``Context.bundle`` envelope plus ``selection`` and ``brief``."""
    _validate_budgets(limit, detail_tokens, brief_tokens)
    selected_ranker = ranker or FROZEN_RANKER
    if selected_ranker not in RANKER_SIMPLICITY:
        raise MemoryError("invalid_ranker")
    derived = derive_query(query)
    terms = useful_terms(derived)
    # Scope before ranking, cache, snippets, and links.
    files = context.files(project_id)
    allowed = set(files)
    bytes_read = sum(len(content.encode("utf-8")) for content in files.values())
    fallback = False
    limitation = None
    try:
        if force_fallback:
            raise MemoryError("index_forced_unavailable")
        index = scoped_index(project_id, files)
    except MemoryError as exc:
        fallback = True
        limitation = f"index_unavailable:{exc}"
        index = {
            "version": INDEX_VERSION,
            "project_id": project_id,
            "fingerprint": corpus_fingerprint(files),
            "pages": [],
            "by_path": {},
            "document_frequency": {},
            "average_length": 0.0,
            "page_count": 0,
        }
    abstained = False
    reason = None
    ranked_paths: list[str] = []
    if fallback:
        lexical = context.legacy_bundle(project_id, derived or query, limit)
        documents, oversize, findings = [], [], list(lexical["findings"])
        if strict_abstain and not terms:
            abstained = True
            reason = "no_useful_terms"
        else:
            for document in lexical["documents"]:
                content = document["content"]
                tokens = estimate_tokens(content)
                if tokens > detail_tokens or tokens > (
                    detail_tokens
                    - sum(estimate_tokens(item["content"]) for item in documents)
                ):
                    raw = files.get(document["path"], content)
                    oversize.append(
                        {
                            "path": document["path"],
                            "sha256": digest(raw),
                            "tokens": estimate_tokens(raw),
                            "bytes": len(raw.encode("utf-8")),
                            "coverage": 0,
                            "reason": "exceeds_detail_budget",
                        }
                    )
                    continue
                documents.append({"path": document["path"], "content": content})
            ranked_paths = [document["path"] for document in lexical["documents"]]
        brief = _fit_brief(
            ["Fallback lexical com escopo. " + (limitation or "")],
            [],
            brief_tokens,
        )
        selection = _selection(
            ranker="scoped_lexical_fallback",
            derived=derived,
            original=query,
            terms=terms,
            detail_tokens=detail_tokens,
            brief_tokens=brief_tokens,
            brief=brief,
            files=files,
            bytes_read=bytes_read,
            documents=documents,
            oversize=oversize,
            ranked_paths=ranked_paths[:limit],
            abstained=abstained,
            reason=reason,
            index=index,
            fallback=limitation,
            compatibility=None,
        )
        return _envelope(
            context,
            project_id,
            documents=documents,
            findings=findings,
            selection=selection,
            brief=brief,
            index_first=True,
        )
    if not terms:
        abstained = True
        reason = "no_useful_terms"
    else:
        scored = _rank_pages(index, terms, selected_ranker)
        if not scored:
            abstained = True
            reason = "unsupported"
        else:
            paths = [path for _, path in scored]
            ranked_paths = _expand_links(paths, index, allowed, limit)
    documents: list[dict[str, str]] = []
    oversize: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    compatibility = None
    if abstained and strict_abstain:
        ordered: list[str] = []
    elif abstained and not strict_abstain:
        legacy = context.legacy_bundle(project_id, query, limit)
        legacy_tokens = sum(
            estimate_tokens(document["content"]) for document in legacy["documents"]
        )
        if legacy_tokens <= detail_tokens:
            documents = list(legacy["documents"])
            findings = list(legacy["findings"])
            compatibility = "query_unsupported_scoped_corpus_fits_budget"
            ordered = []
        else:
            ordered = []
    else:
        ordered = ranked_paths
        documents, oversize, findings = _pack_details(
            ordered, files, allowed, detail_tokens
        )
        if not strict_abstain:
            legacy = context.legacy_bundle(project_id, query, limit)
            legacy_tokens = sum(
                estimate_tokens(document["content"]) for document in legacy["documents"]
            )
            if legacy_tokens <= detail_tokens:
                documents = list(legacy["documents"])
                findings = list(legacy["findings"])
                oversize = []
                compatibility = "legacy_documents_fit_detail_budget"
    selected_paths = {document["path"] for document in documents}
    snippet_pages = [
        index["by_path"][path]
        for path in ordered
        if path in selected_paths and path in index["by_path"]
    ]
    snippets = _snippets(snippet_pages, terms, allowed) if terms else []
    header = [
        f"Consulta: {derived}" if derived else "Consulta vazia.",
        (f"Abstenção: {reason}." if abstained else f"Ranqueador: {selected_ranker}."),
    ]
    if limitation:
        header.append(limitation)
    if compatibility:
        header.append(compatibility)
    for path in [document["path"] for document in documents][:limit]:
        page = index["by_path"].get(path)
        title = page["title"] if page else path.rsplit("/", 1)[-1]
        header.append(f"{path} — {title}")
    brief = _fit_brief(header, snippets, brief_tokens)
    selection = _selection(
        ranker=selected_ranker,
        derived=derived,
        original=query,
        terms=terms,
        detail_tokens=detail_tokens,
        brief_tokens=brief_tokens,
        brief=brief,
        files=files,
        bytes_read=bytes_read,
        documents=documents,
        oversize=oversize,
        ranked_paths=ranked_paths,
        abstained=abstained,
        reason=reason,
        index=index,
        fallback=None,
        compatibility=compatibility,
        snippets=snippets,
    )
    return _envelope(
        context,
        project_id,
        documents=documents,
        findings=findings,
        selection=selection,
        brief=brief,
        index_first=False,
    )


def _selection(
    *,
    ranker: str,
    derived: str,
    original: str,
    terms: list[str],
    detail_tokens: int,
    brief_tokens: int,
    brief: str,
    files: dict[str, str],
    bytes_read: int,
    documents: list[dict[str, str]],
    oversize: list[dict[str, Any]],
    ranked_paths: list[str],
    abstained: bool,
    reason: str | None,
    index: dict[str, Any],
    fallback: str | None,
    compatibility: str | None,
    snippets: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    selected_bytes = sum(
        len(document["content"].encode("utf-8")) for document in documents
    )
    if not ranked_paths:
        coverage = 1.0
    else:
        present = {document["path"] for document in documents}
        coverage = len(present.intersection(ranked_paths)) / len(ranked_paths)
    return {
        "version": RETRIEVAL_VERSION,
        "estimator": TOKEN_ESTIMATOR,
        "ranker": ranker,
        "budget": {
            "detail_tokens": detail_tokens,
            "brief_tokens": brief_tokens,
        },
        "tokens": {
            "detail": sum(estimate_tokens(item["content"]) for item in documents),
            "brief": estimate_tokens(brief),
        },
        "pages_read": len(files),
        "pages_selected": len(documents),
        "bytes_read": bytes_read,
        "bytes_selected": selected_bytes,
        "coverage": coverage,
        "abstained": abstained,
        "abstain_reason": reason,
        "index": {
            "version": index.get("version", INDEX_VERSION),
            "fingerprint": index.get("fingerprint"),
            "pages": index.get("page_count", 0),
        },
        "fallback": fallback,
        "limitation": fallback or compatibility,
        "oversize": oversize,
        "ranked_paths": list(ranked_paths),
        "derived_query": derived,
        "original_query": original,
        "terms": terms,
        "snippets": snippets or [],
        "compatibility": compatibility,
    }
