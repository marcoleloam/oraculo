from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timezone
from html import escape
from pathlib import Path
from typing import Any

from .context import Context
from .publish import Publisher, atomic_write
from .schema import (
    ConflictError,
    MemoryError,
    corpus,
    digest,
    identifier,
    parse_page,
    project_key,
    render_page,
    rewrite_links,
    safe_path,
)


def render_raw_projection(content: str) -> str:
    """Show source bytes as literal text, never as executable vault references.

    Raw may contain arbitrary Markdown, HTML, aliases and links from a previous
    review. It has no trusted link namespace. Only the canonical snapshot keeps
    the original bytes; the review's immutable display copy must not register
    frontmatter aliases, resolve links globally or embed another vault file.
    """
    literal = escape(content).replace("[", "&#91;").replace("]", "&#93;")
    return (
        "# Fonte bruta — somente leitura\n\n"
        f"SHA-256 do conteúdo original: `{digest(content)}`.\n\n"
        "Exibição literal da fonte; links, embeds e metadados não são ativados.\n\n"
        f"<pre>{literal}</pre>\n"
    )


class ReviewManager:
    def __init__(self, publisher: Publisher) -> None:
        self.publisher = publisher
        self.sessions = publisher.state_dir / "reviews"
        self.sessions.mkdir(exist_ok=True)

    def _metadata(self, session_id: str) -> tuple[Path, dict[str, Any]]:
        path = safe_path(self.sessions, f"{identifier(session_id)}/session.json")
        if not path.exists():
            raise MemoryError("unknown_review")
        return path, json.loads(path.read_bytes().decode("utf-8"))

    def _save(self, path: Path, session: dict[str, Any]) -> None:
        atomic_write(path, json.dumps(session, ensure_ascii=False).encode())

    def open(self, project_id: str, session_id: str | None = None) -> dict[str, Any]:
        project_key(project_id)
        session_id = identifier(session_id or "r" + uuid.uuid4().hex)
        prefix = f"reviews/{session_id}"
        with self.publisher.locked():
            self.publisher._recover()
            existing = self.sessions / session_id / "session.json"
            if existing.exists():
                session = json.loads(existing.read_bytes().decode("utf-8"))
                if session["project_id"] != project_id:
                    raise ConflictError("review_id_reused")
                if session["state"] == "preparing":
                    for relative, content in session["projected_base"].items():
                        target = safe_path(self.publisher.vault, relative)
                        if (
                            target.exists()
                            and target.read_bytes().decode("utf-8") != content
                        ):
                            raise ConflictError("review_preparation_modified")
                    for relative, content in session["projected_base"].items():
                        target = safe_path(self.publisher.vault, relative)
                        if not target.exists():
                            atomic_write(target, content.encode())
                    atomic_write(
                        safe_path(self.publisher.vault, f"{prefix}/review.json"),
                        json.dumps(
                            {
                                k: session[k]
                                for k in (
                                    "session_id",
                                    "project_id",
                                    "base_commit",
                                    "created_at",
                                    "wiki_folder",
                                )
                            }
                        ).encode(),
                    )
                    session["state"] = "open"
                    self._save(existing, session)
                return {
                    k: session[k]
                    for k in (
                        "session_id",
                        "project_id",
                        "state",
                        "base_commit",
                        "wiki_folder",
                    )
                }
            self.publisher._check_integrity()
            files = Context(self.publisher.vault).files(project_id)
            mapping: dict[str, str] = {}
            for path in files:
                parts = Path(path).parts
                name = parts[-1]
                if "/raw/" in path:
                    name = (
                        "shared--"
                        if path.startswith("shared/")
                        else project_key(project_id) + "--"
                    ) + name
                    mapping[path] = f"{prefix}/raw/{session_id}--{name}"
                elif "/wiki/" in path and parts[-2] in {
                    "entities",
                    "concepts",
                    "sources",
                }:
                    mapping[path] = f"{prefix}/wiki/{parts[-2]}/{session_id}--{name}"
            projected: dict[str, str] = {}
            for canonical, projected_path in mapping.items():
                content = files[canonical]
                projected[projected_path] = (
                    render_raw_projection(content)
                    if "/raw/" in canonical
                    else rewrite_links(content, mapping, canonical)
                )
            schema_path = f"{corpus(project_id)}/wiki/schema/config.md"
            if schema_path in files:
                projected[f"{prefix}/wiki/schema/config.md"] = files[schema_path]
            projected[f"{prefix}/wiki/index.md"] = "# Revisão\n\n" + "\n".join(
                f"- [[{path.removesuffix('.md')}]]"
                for path in mapping.values()
                if "/wiki/" in path
            )
            projected[f"{prefix}/wiki/log.md"] = "# Log da revisão\n"
            session = {
                "session_id": session_id,
                "project_id": project_id,
                "state": "preparing",
                "base_commit": self.publisher.git.head(),
                "created_at": datetime.now(timezone.utc).isoformat(),
                "mapping": mapping,
                "base": {p: files[p] for p in mapping},
                "projected_base": projected,
                "wiki_folder": f"{prefix}/wiki",
            }
            metadata_path = self.sessions / session_id / "session.json"
            self._save(metadata_path, session)
            for path, content in projected.items():
                atomic_write(safe_path(self.publisher.vault, path), content.encode())
            atomic_write(
                safe_path(self.publisher.vault, f"{prefix}/review.json"),
                json.dumps(
                    {
                        k: session[k]
                        for k in (
                            "session_id",
                            "project_id",
                            "base_commit",
                            "created_at",
                            "wiki_folder",
                        )
                    }
                ).encode(),
            )
            session["state"] = "open"
            self._save(metadata_path, session)
        return {
            k: session[k]
            for k in ("session_id", "project_id", "state", "base_commit", "wiki_folder")
        }

    def _capture(self, session_id: str) -> dict[str, str]:
        root = safe_path(self.publisher.vault, f"reviews/{identifier(session_id)}")
        result = {}
        for path in sorted(root.rglob("*")):
            relative = path.relative_to(self.publisher.vault).as_posix()
            safe_path(self.publisher.vault, relative)
            if path.is_file() and path.suffix == ".md":
                result[relative] = path.read_bytes().decode("utf-8")
        return result

    def close(self, session_id: str) -> dict[str, Any]:
        with self.publisher.locked():
            self.publisher._recover()
            metadata_path, session = self._metadata(session_id)
            observed = self._capture(session_id)
            operation = self.publisher.journals / f"review-{session_id}.json"
            if session["state"] == "sealing" and operation.exists():
                journal = json.loads(operation.read_bytes().decode("utf-8"))
                if journal["state"] == "confirmed":
                    session["receipt"] = {
                        **journal["receipt"],
                        "state": "imported",
                        "session_id": session_id,
                    }
                    session["state"] = "imported"
                    self._save(metadata_path, session)
            if session["state"] == "imported":
                if observed != session.get("sealed"):
                    return {
                        "state": "pending",
                        "reason": "post_seal_changes",
                        "session_id": session_id,
                    }
                return session["receipt"]
            if session["state"] == "conflict":
                return {
                    "state": "conflict",
                    "session_id": session_id,
                    "findings": session.get("findings", []),
                }
            session["state"] = "sealing"
            session["sealed"] = observed
            self._save(metadata_path, session)
            for path, content in observed.items():
                relative = str(Path(path).relative_to(f"reviews/{session_id}"))
                atomic_write(
                    safe_path(metadata_path.parent / "sealed", relative),
                    content.encode(),
                )
            if self._capture(session_id) != observed:
                session["state"] = "open"
                self._save(metadata_path, session)
                return {
                    "state": "pending",
                    "reason": "changed_during_seal",
                    "session_id": session_id,
                }
            try:
                proposal = self._proposal(session, observed)
                raw_sources = proposal.pop("_raw_sources", {})
                if self._capture(session_id) != observed:
                    session["state"] = "open"
                    self._save(metadata_path, session)
                    return {
                        "state": "pending",
                        "reason": "changed_during_seal",
                        "session_id": session_id,
                    }
                self.publisher._check_integrity()
                if proposal["changes"]:
                    from .schema import MemoryProposal

                    model = MemoryProposal.model_validate(proposal)
                    if len(model.model_dump_json().encode()) > 2 * 1024 * 1024:
                        raise MemoryError("proposal_too_large")
                    receipt = self.publisher._publish(
                        model, digest(model.model_dump_json()), raw_sources=raw_sources
                    )
                else:
                    receipt = {"commit": self.publisher.git.head(), "paths": []}
            except (MemoryError, ValueError) as exc:
                session.update(state="conflict", findings=[str(exc)])
                self._save(metadata_path, session)
                return {
                    "state": "conflict",
                    "session_id": session_id,
                    "findings": session["findings"],
                }
            receipt.update(state="imported", session_id=session_id)
            session.update(
                state="imported",
                receipt=receipt,
                imported_at=datetime.now(timezone.utc).isoformat(),
            )
            self._save(metadata_path, session)
            if self._capture(session_id) != observed:
                return {**receipt, "post_seal_changes": True}
            return receipt

    def _proposal(
        self, session: dict[str, Any], observed: dict[str, str]
    ) -> dict[str, Any]:
        reverse = {
            projected: canonical for canonical, projected in session["mapping"].items()
        }
        changes = []
        new_sources = {}
        project_id = session["project_id"]
        session_id = session["session_id"]
        new_pages = {}
        for path in observed:
            if path in reverse or path in session["projected_base"]:
                continue
            if "/wiki/" not in path or Path(path).parent.name not in {
                "entities",
                "concepts",
                "sources",
            }:
                raise MemoryError("unsupported_review_file")
            slug = (
                Path(path)
                .stem.removeprefix(session_id + "--")
                .removeprefix(project_key(project_id) + "--")
            )
            identifier(slug)
            canonical = f"{corpus(project_id)}/wiki/{Path(path).parent.name}/{project_key(project_id)}--{slug}.md"
            if canonical in reverse.values():
                raise ConflictError("new_page_identity_collision")
            reverse[path] = canonical
            new_pages[path] = canonical
        for projected, canonical in reverse.items():
            if projected not in observed:
                raise ConflictError("review_deletion_not_supported")
            value = observed[projected]
            if "/raw/" in canonical:
                if value != session["projected_base"][projected]:
                    raise ConflictError("review_raw_immutable")
                continue
            if value == session["projected_base"].get(projected):
                continue
            translated = rewrite_links(value, reverse, projected)
            current_path = safe_path(self.publisher.vault, canonical)
            current = (
                current_path.read_bytes().decode("utf-8")
                if current_path.exists()
                else None
            )
            base = session["base"].get(canonical)
            if current is not None and parse_page(current)[0].get("reviewed"):
                raise ConflictError("reviewed_page_protected")
            if current == translated:
                continue
            if current != base:
                raise ConflictError(f"concurrent_edit:{canonical}")
            if canonical.startswith("shared/") and project_id != "shared":
                raise ConflictError("shared_changes_require_shared_review")
            metadata, body = parse_page(translated)
            if projected in new_pages:
                page_id = f"{project_key(project_id)}--{Path(canonical).stem.removeprefix(project_key(project_id) + '--')}"
                metadata.update(
                    page_id=page_id, project_id=project_id, schema_version=1
                )
                metadata.setdefault("sources", [])
                metadata.setdefault("tags", [])
                metadata.setdefault("aliases", [])
                metadata.setdefault("reviewed", False)
                metadata.setdefault("created", date.today().isoformat())
                metadata.setdefault("updated", date.today().isoformat())
                raw_hash = digest(value)
                raw_path = f"{corpus(project_id)}/raw/{raw_hash}.md"
                new_sources[raw_path] = value.encode()
                metadata["sources"].append(f"[[{raw_path.removesuffix('.md')}]]")
            translated = render_page(metadata, body)
            changes.append(
                {
                    "page_id": metadata.get("page_id"),
                    "path": canonical,
                    "base_sha256": digest(current) if current is not None else None,
                    "content": translated,
                }
            )
        if len(changes) > 20:
            raise MemoryError("review_too_many_pages")
        return {
            "_raw_sources": new_sources,
            "schema_version": 1,
            "batch_id": f"review-{session_id}",
            "operation_id": f"review-{session_id}",
            "project_id": project_id,
            "base_commit": session["base_commit"],
            "source_hashes": {},
            "changes": changes,
            "citations": [],
            "findings": [],
        }

    def cleanup_candidates(self, retention_days: int = 30) -> list[str]:
        if retention_days < 1:
            raise MemoryError("invalid_retention_days")
        result = []
        now = datetime.now(timezone.utc)
        with self.publisher.locked():
            for metadata in self.sessions.glob("*/session.json"):
                session = json.loads(metadata.read_bytes().decode("utf-8"))
                if session["state"] != "imported" or not session.get("imported_at"):
                    continue
                age = now - datetime.fromisoformat(session["imported_at"])
                if age.days >= retention_days and self._capture(
                    session["session_id"]
                ) == session.get("sealed"):
                    result.append(session["session_id"])
        return result
