from __future__ import annotations

import hashlib
import re
from datetime import date
from pathlib import Path, PurePosixPath
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field


class MemoryError(ValueError):
    pass


class ConflictError(MemoryError):
    pass


# Page envelope stays schema_version 1. Typed claims opt in separately.
KNOWLEDGE_SCHEMA_VERSION = 1


class PageChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    page_id: str
    path: str
    base_sha256: str | None = None
    content: str


class MemoryProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1
    batch_id: str
    operation_id: str
    project_id: str
    base_commit: str | None = None
    source_hashes: dict[str, str] = Field(default_factory=dict)
    changes: list[PageChange] = Field(min_length=1, max_length=20)
    citations: list[str] = Field(default_factory=list)
    findings: list[Any] = Field(default_factory=list)


def digest(content: bytes | str) -> str:
    return hashlib.sha256(
        content.encode() if isinstance(content, str) else content
    ).hexdigest()


def identifier(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,119}", value):
        raise MemoryError("invalid_identifier")
    return value


def project_key(project_id: str) -> str:
    if (
        not isinstance(project_id, str)
        or not project_id
        or len(project_id.encode("utf-8")) > 512
    ):
        raise MemoryError("invalid_project_id")
    if (
        any(ord(char) < 32 or ord(char) == 127 for char in project_id)
        or any(char in project_id for char in ("/", "\\"))
        or ".." in project_id
        or project_id != project_id.strip()
    ):
        raise MemoryError("invalid_project_id")
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,119}", project_id):
        return project_id
    return "p-" + digest(project_id)


def corpus(project_id: str) -> str:
    key = project_key(project_id)
    return "shared" if project_id == "shared" else f"projects/{key}"


def safe_path(root: Path, relative: str) -> Path:
    p = PurePosixPath(relative)
    if (
        not relative
        or p.is_absolute()
        or ".." in p.parts
        or "\\" in relative
        or str(p) != relative
    ):
        raise MemoryError("unsafe_path")
    root = root.absolute()
    if root.is_symlink():
        raise MemoryError("symlink_root")
    target = root.joinpath(*p.parts)
    for ancestor in (target, *target.parents):
        if ancestor.is_symlink():
            raise MemoryError("symlink_path")
        if ancestor == root:
            break
    if not target.resolve().is_relative_to(root.resolve()):
        raise MemoryError("path_escape")
    return target


def parse_page(content: str) -> tuple[dict[str, Any], str]:
    if not content.startswith("---\n"):
        raise MemoryError("frontmatter_required")
    parts = content.split("\n---\n", 1)
    if len(parts) != 2:
        raise MemoryError("frontmatter_unclosed")
    try:
        metadata = yaml.safe_load(parts[0][4:])
    except yaml.YAMLError as exc:
        raise MemoryError("invalid_yaml") from exc
    if not isinstance(metadata, dict):
        raise MemoryError("frontmatter_mapping_required")
    return metadata, parts[1]


def render_page(metadata: dict[str, Any], body: str) -> str:
    return (
        "---\n"
        + yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False)
        + "---\n"
        + body
    )


def validate_page(
    content: str, project_id: str, page_id: str, tags: set[str] | None = None
) -> dict[str, Any]:
    meta, _ = parse_page(content)
    if meta.get("type") not in {"entity", "concept", "source"}:
        raise MemoryError("invalid_page_type")
    if (
        meta.get("page_id") != page_id
        or meta.get("project_id") != project_id
        or meta.get("schema_version") != 1
    ):
        raise MemoryError("page_identity_mismatch")
    identifier(page_id)
    for key in ("sources", "tags", "aliases"):
        if not isinstance(meta.get(key), list) or not all(
            isinstance(x, str) for x in meta[key]
        ):
            raise MemoryError(f"invalid_{key}")
    if any(not re.fullmatch(r"\[\[[^\]]+\]\]", source) for source in meta["sources"]):
        raise MemoryError("sources_require_wikilinks")
    if tags is not None and not set(meta["tags"]).issubset(tags):
        raise MemoryError("unknown_tag")
    if type(meta.get("reviewed")) is not bool:
        raise MemoryError("reviewed_boolean_required")
    for key in ("created", "updated"):
        try:
            date.fromisoformat(str(meta[key]))
        except (ValueError, KeyError) as exc:
            raise MemoryError("invalid_date") from exc
    knowledge_domain(meta)
    return meta


def knowledge_domain(meta: dict[str, Any]) -> dict[str, Any] | None:
    """Return the opt-in typed block, or None when the page is legacy.

    Missing knowledge_schema means not evaluated. A present but unapproved
    version is refused instead of being rewritten into the installed contract.
    """
    if "knowledge_schema" not in meta and "claims" not in meta:
        return None
    claims = meta.get("claims")
    if meta.get("knowledge_schema") != KNOWLEDGE_SCHEMA_VERSION or not isinstance(
        claims, list
    ):
        raise MemoryError("invalid_knowledge_schema")
    return {"knowledge_schema": KNOWLEDGE_SCHEMA_VERSION, "claims": claims}


def links(content: str) -> list[str]:
    return [
        m.group(1).split("|", 1)[0].split("#", 1)[0]
        for m in re.finditer(r"\[\[([^\]]+)\]\]", content)
    ]


def redact_wikilink(content: str, target: str) -> str:
    """Replace every wikilink whose stripped target is ``target``.

    ``links()`` drops ``#anchor`` and ``|alias``. The substitution has to
    accept both or the anchor form stays visible after redaction.
    """
    pattern = (
        r"\[\[" + re.escape(target) + r"(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]"
    )
    return re.sub(pattern, "[referência fora do escopo omitida]", content)


# Shared with markdown_targets. The lookbehind leaves image embeds alone.
MARKDOWN_LINK = re.compile(r"(?<!!)\[([^\]\n]*)\]\(([^)\n]+)\)")


def resolve_link(target: str, paths: set[str], source: str = "") -> str:
    if (
        not target
        or target.startswith(("/", "~"))
        or ".." in PurePosixPath(target).parts
        or "\\" in target
    ):
        raise MemoryError("unsafe_link")
    options = {target, target + ".md"}
    if source:
        options |= {
            str(PurePosixPath(source).parent / option) for option in tuple(options)
        }
    exact = paths & options
    if len(exact) == 1:
        return next(iter(exact))
    matches = {
        p
        for p in paths
        if PurePosixPath(p).name in options or PurePosixPath(p).stem == target
    }
    if len(matches) != 1:
        raise MemoryError("unresolved_or_ambiguous_link")
    return next(iter(matches))


def _shield_claims(content: str) -> tuple[str, Any]:
    """Keep claim quotes out of link rewriting.

    Evidence quotes are byte locators. A quote may contain wiki syntax from
    the captured source; rewriting it would break the hash and the citation.
    """
    if not content.startswith("---\n"):
        return content, None
    try:
        meta, body = parse_page(content)
    except MemoryError:
        return content, None
    if "claims" not in meta:
        return content, None
    claims = meta.pop("claims")
    return render_page(meta, body), claims


def rewrite_links(content: str, mapping: dict[str, str], source: str = "") -> str:
    content, claims = _shield_claims(content)

    def replace(match: re.Match[str]) -> str:
        raw = match.group(1)
        target, *alias = raw.split("|", 1)
        target, *anchor = target.split("#", 1)
        resolved = resolve_link(target, set(mapping), source)
        return (
            "[["
            + mapping[resolved].removesuffix(".md")
            + ("#" + anchor[0] if anchor else "")
            + ("|" + alias[0] if alias else "")
            + "]]"
        )

    result = re.sub(r"\[\[([^\]]+)\]\]", replace, content)

    def replace_markdown(match: re.Match[str]) -> str:
        target = match.group(2).strip().strip("<>")
        if target.startswith(("https://", "http://", "mailto:", "#")):
            return match.group(0)
        target, *anchor = target.split("#", 1)
        resolved = resolve_link(target, set(mapping), source)
        return (
            "["
            + match.group(1)
            + "]("
            + mapping[resolved]
            + ("#" + anchor[0] if anchor else "")
            + ")"
        )

    result = MARKDOWN_LINK.sub(replace_markdown, result)
    if result.startswith("---\n"):
        meta, body = parse_page(result)
        note = meta.get("source_note")
        if isinstance(note, str) and not note.startswith("[["):
            meta["source_note"] = mapping[resolve_link(note, set(mapping), source)]
            result = render_page(meta, body)
    if claims is not None and result.startswith("---\n"):
        meta, body = parse_page(result)
        meta["claims"] = claims
        result = render_page(meta, body)
    return result


def markdown_targets(content: str) -> list[str]:
    targets = []
    for match in MARKDOWN_LINK.finditer(content):
        target = match.group(2).strip().strip("<>")
        if target.startswith(("https://", "http://", "mailto:", "#")):
            continue
        targets.append(target.split("#", 1)[0])
    return targets
