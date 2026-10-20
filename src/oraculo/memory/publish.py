from __future__ import annotations

import base64
import contextlib
import fcntl
import json
import os
import tempfile
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from oraculo.config import resources_root

from .git import VaultGit
from .schema import (
    ConflictError,
    MemoryError,
    MemoryProposal,
    corpus,
    digest,
    identifier,
    links,
    markdown_targets,
    parse_page,
    project_key,
    render_page,
    resolve_link,
    safe_path,
    validate_page,
)


def atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".oraculo-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class Publisher:
    def __init__(
        self, vault: Path, state_dir: Path, *, lock_timeout: float | None = None
    ) -> None:
        self.vault = Path(vault).expanduser().absolute()
        self.state_dir = Path(state_dir).expanduser().absolute() / "memory"
        self.git = VaultGit(self.vault)
        self.lock_timeout = lock_timeout
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.state_dir, 0o700)
        self.journals = self.state_dir / "journals"
        self.journals.mkdir(exist_ok=True)
        self.catalog = self.state_dir / "catalog.json"

    @contextlib.contextmanager
    def locked(self) -> Iterator[None]:
        with (self.state_dir / "writer.lock").open("a") as lock:
            if self.lock_timeout is None:
                fcntl.flock(lock, fcntl.LOCK_EX)
            else:
                deadline = time.monotonic() + self.lock_timeout
                while True:
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise MemoryError("memory_writer_busy") from None
                        time.sleep(0.02)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def initialize(self, templates: Path | None = None) -> None:
        with self.locked():
            self.git.initialize()
            self._recover()
            if not self.catalog.exists():
                atomic_write(self.catalog, b"{}")
            templates = templates or resources_root() / "templates/vault"
            defaults = {
                ".gitignore": ".obsidian/\nreviews/\n.oraculo-*/\n",
                "AGENTS.md": "# Oráculo\n\nCanonical memory has one publisher. Use versioned review sessions. Raw sources are immutable. Client context is project + shared only.\n",
                "CLAUDE.md": "# Oráculo\n\nFollow AGENTS.md. Publish structured proposals; never edit canonical memory directly.\n",
                "index.md": "# Oráculo\n\nMemória por projeto e conhecimento compartilhado. Revisões em reviews/.\n",
            }
            updates = {}
            for name, fallback in defaults.items():
                target = safe_path(self.vault, name)
                source = templates / ("gitignore" if name == ".gitignore" else name)
                if not target.exists():
                    updates[name] = (
                        source.read_bytes() if source.is_file() else fallback.encode()
                    )
            if updates:
                self._transaction(
                    "initialize-" + digest(json.dumps(sorted(updates))), updates
                )

    def initialize_project(
        self, project_id: str, templates: Path | None = None
    ) -> None:
        with self.locked():
            self._recover()
            self._initialize_project(project_id, templates)

    def _initialize_project(
        self, project_id: str, templates: Path | None = None
    ) -> None:
        prefix = corpus(project_id)
        marker_relative = f"{prefix}/.oraculo-project.json"
        marker = safe_path(self.vault, marker_relative)
        if marker.exists():
            if json.loads(marker.read_bytes())["project_id"] != project_id:
                raise ConflictError("project_key_collision")
        else:
            self._transaction(
                "scope-" + digest(project_id),
                {marker_relative: json.dumps({"project_id": project_id}).encode()},
            )
        templates = templates or resources_root() / "templates/vault"
        source = templates / "schema-config.md"
        default = "---\nschema_version: 1\n---\n# Schema\n\nTypes: entity, concept, source. Sources are wiki-links; reviewed pages are immutable.\n"
        path = f"{prefix}/wiki/schema/config.md"
        if not safe_path(self.vault, path).exists():
            self._transaction(
                "schema-" + digest(project_id),
                {path: source.read_bytes() if source.is_file() else default.encode()},
            )

    def _hashes(self) -> dict[str, str]:
        return (
            json.loads(self.catalog.read_bytes().decode("utf-8"))
            if self.catalog.exists()
            else {}
        )

    def _check_integrity(self) -> None:
        for relative, expected in self._hashes().items():
            path = safe_path(self.vault, relative)
            if not path.is_file() or digest(path.read_bytes()) != expected:
                raise ConflictError(f"external_change:{relative}")

    def _save_journal(self, path: Path, journal: dict[str, Any]) -> None:
        atomic_write(path, json.dumps(journal, ensure_ascii=False).encode())

    def _transaction(
        self, operation_id: str, updates: dict[str, bytes], payload_hash: str = ""
    ) -> dict[str, Any]:
        identifier(operation_id)
        journal_path = self.journals / f"{operation_id}.json"
        if journal_path.exists():
            old = json.loads(journal_path.read_bytes().decode("utf-8"))
            if old.get("payload_hash") != payload_hash:
                raise ConflictError("operation_id_reused")
            if old["state"] == "confirmed":
                return old["receipt"]
            raise ConflictError("operation_pending_recovery")
        self._check_integrity()
        before = {}
        for relative in updates:
            target = safe_path(self.vault, relative)
            before[relative] = (
                base64.b64encode(target.read_bytes()).decode()
                if target.exists()
                else None
            )
        journal = {
            "operation_id": operation_id,
            "payload_hash": payload_hash,
            "state": "prepared",
            "before": before,
            "after": {
                p: base64.b64encode(data).decode() for p, data in updates.items()
            },
        }
        self._save_journal(journal_path, journal)
        return self._finish(journal_path, journal)

    def _finish(self, journal_path: Path, journal: dict[str, Any]) -> dict[str, Any]:
        updates = {p: base64.b64decode(data) for p, data in journal["after"].items()}
        for relative, data in updates.items():
            target = safe_path(self.vault, relative)
            current = target.read_bytes() if target.exists() else None
            before = journal["before"][relative]
            original = base64.b64decode(before) if before is not None else None
            if current not in (original, data):
                journal["state"] = "conflict"
                self._save_journal(journal_path, journal)
                raise ConflictError(f"recovery_external_change:{relative}")
        for relative, data in updates.items():
            target = safe_path(self.vault, relative)
            if not target.exists() or target.read_bytes() != data:
                atomic_write(target, data)
        commit = self.git.operation_commit(journal["operation_id"])
        if commit is None:
            commit = self.git.commit(list(updates), journal["operation_id"])
        hashes = self._hashes()
        hashes.update({p: digest(data) for p, data in updates.items()})
        atomic_write(self.catalog, json.dumps(hashes, sort_keys=True).encode())
        receipt = {
            "state": "confirmed",
            "operation_id": journal["operation_id"],
            "commit": commit,
            "paths": list(updates),
        }
        journal.update(state="confirmed", receipt=receipt)
        self._save_journal(journal_path, journal)
        return receipt

    def _recover(self) -> list[dict[str, Any]]:
        result = []
        for path in sorted(self.journals.glob("*.json")):
            journal = json.loads(path.read_bytes().decode("utf-8"))
            if journal["state"] != "confirmed":
                result.append(self._finish(path, journal))
        return result

    def recover(self) -> list[dict[str, Any]]:
        with self.locked():
            return self._recover()

    def snapshot(
        self, project_id: str, content: str, provenance: dict[str, Any] | None = None
    ) -> str:
        data = content.encode()
        raw_hash = digest(data)
        relative = f"{corpus(project_id)}/raw/{raw_hash}.md"
        with self.locked():
            self._recover()
            self._check_integrity()
            self._initialize_project(project_id)
            target = safe_path(self.vault, relative)
            if target.exists():
                if target.read_bytes() != data:
                    raise ConflictError("raw_hash_mismatch")
                return relative
            self._transaction(
                "raw-" + digest(project_id + raw_hash), {relative: data}, raw_hash
            )
            provenance_dir = self.state_dir / "provenance"
            atomic_write(
                provenance_dir / f"{project_key(project_id)}-{raw_hash}.json",
                json.dumps(
                    {
                        **(provenance or {}),
                        "project_id": project_id,
                        "captured_at": datetime.now(timezone.utc).isoformat(),
                    },
                    ensure_ascii=False,
                ).encode(),
            )
        return relative

    def publish(self, proposal: MemoryProposal | dict[str, Any]) -> dict[str, Any]:
        proposal = MemoryProposal.model_validate(proposal)
        serialized = proposal.model_dump_json()
        if len(serialized.encode()) > 2 * 1024 * 1024:
            raise MemoryError("proposal_too_large")
        identifier(proposal.batch_id)
        identifier(proposal.operation_id)
        with self.locked():
            self._recover()
            journal = self.journals / f"{proposal.operation_id}.json"
            if journal.exists():
                stored = json.loads(journal.read_bytes().decode("utf-8"))
                if stored.get("payload_hash") != digest(serialized):
                    raise ConflictError("operation_id_reused")
                return stored["receipt"]
            batch_path = self.state_dir / "batches" / f"{proposal.batch_id}.json"
            if batch_path.exists():
                previous = json.loads(batch_path.read_bytes().decode("utf-8"))
                if previous["operation_id"] != proposal.operation_id:
                    raise ConflictError("batch_id_reused")
            atomic_write(
                batch_path, json.dumps({"operation_id": proposal.operation_id}).encode()
            )
            self._check_integrity()
            return self._publish(proposal, digest(serialized))

    def _publish(
        self,
        proposal: MemoryProposal,
        payload_hash: str,
        raw_sources: dict[str, bytes] | None = None,
    ) -> dict[str, Any]:
        from .context import Context

        prefix = corpus(proposal.project_id)
        self._initialize_project(proposal.project_id)
        pages = Context(self.vault).files(proposal.project_id)
        raw_sources = raw_sources or {}
        for relative, content in raw_sources.items():
            if relative != f"{prefix}/raw/{digest(content)}.md":
                raise MemoryError("invalid_raw_snapshot")
            target = safe_path(self.vault, relative)
            if target.exists() and target.read_bytes() != content:
                raise ConflictError("raw_hash_mismatch")
            pages[relative] = content.decode()
        for path, expected in proposal.source_hashes.items():
            if path not in pages or digest(pages[path]) != expected:
                raise ConflictError("source_changed_or_outside_scope")
        if len({change.path for change in proposal.changes}) != len(
            proposal.changes
        ) or len({change.page_id for change in proposal.changes}) != len(
            proposal.changes
        ):
            raise MemoryError("duplicate_change")
        updates: dict[str, bytes] = dict(raw_sources)
        today = date.today().isoformat()
        for change in proposal.changes:
            target = safe_path(self.vault, change.path)
            relative = Path(change.path)
            if (
                relative.parent.as_posix()
                not in {
                    f"{prefix}/wiki/{folder}"
                    for folder in ("entities", "concepts", "sources")
                }
                or not relative.name.startswith(project_key(proposal.project_id) + "--")
                or relative.suffix != ".md"
            ):
                raise MemoryError("invalid_page_path")
            current = target.read_bytes().decode("utf-8") if target.exists() else None
            if (digest(current) if current is not None else None) != change.base_sha256:
                raise ConflictError("base_hash_mismatch")
            meta, body = parse_page(change.content)
            if current is not None:
                old, _ = parse_page(current)
                if old.get("reviewed") and change.content != current:
                    raise ConflictError("reviewed_page_protected")
                if old.get("page_id") != change.page_id:
                    raise MemoryError("page_id_changed")
                meta["created"] = old["created"]
            else:
                meta["created"] = today
            meta["updated"] = today
            normalized = render_page(meta, body)
            validate_page(
                normalized, proposal.project_id, change.page_id, self._tags(prefix)
            )
            expected_type = {
                "entities": "entity",
                "concepts": "concept",
                "sources": "source",
            }[relative.parent.name]
            if meta["type"] != expected_type:
                raise MemoryError("page_folder_type_mismatch")
            for existing_path, existing_content in pages.items():
                if (
                    existing_path != change.path
                    and "/wiki/" in existing_path
                    and Path(existing_path).parent.name
                    in {"entities", "concepts", "sources"}
                ):
                    existing_meta, _ = parse_page(existing_content)
                    if existing_meta.get("page_id") == change.page_id:
                        raise MemoryError("duplicate_page_identity")
            updates[change.path] = normalized.encode()
        known = set(pages) | set(updates)
        for path, content in updates.items():
            if "/raw/" in path:
                continue
            for target in links(content.decode()) + markdown_targets(content.decode()):
                resolve_link(target, known, path)
            meta, _ = parse_page(content.decode())
            note = meta.get("source_note")
            if isinstance(note, str) and not note.startswith("[["):
                resolve_link(note, known, path)
            if "reviews/" in content.decode():
                raise MemoryError("review_reference_in_canonical")
        for citation in proposal.citations:
            resolve_link(citation.removeprefix("[[").removesuffix("]]"), known)
        all_pages = {**pages, **{p: b.decode() for p, b in updates.items()}}
        own_pages = sorted(
            p
            for p in all_pages
            if p.startswith(f"{prefix}/wiki/")
            and p.split("/")[-2] in {"entities", "concepts", "sources"}
        )
        index = "# Índice\n\n" + "".join(
            f"- [[{p.removesuffix('.md')}]]\n" for p in own_pages
        )
        updates[f"{prefix}/wiki/index.md"] = index.encode()
        log_path = f"{prefix}/wiki/log.md"
        log_file = safe_path(self.vault, log_path)
        previous = (
            log_file.read_bytes().decode("utf-8") if log_file.exists() else "# Log\n"
        )
        updates[log_path] = (
            previous
            + f"\n## [{today}] ingest | {proposal.batch_id}\n\nbatch_id: {proposal.batch_id}\noperation_id: {proposal.operation_id}\n"
        ).encode()
        return self._transaction(proposal.operation_id, updates, payload_hash)

    def _tags(self, prefix: str) -> set[str] | None:
        path = safe_path(self.vault, f"{prefix}/wiki/schema/config.md")
        if not path.exists():
            return None
        metadata, _ = parse_page(path.read_bytes().decode("utf-8"))
        tags = metadata.get("tags") or metadata.get("allowed_tags")
        return set(tags) if isinstance(tags, list) else None
