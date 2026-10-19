from __future__ import annotations

from typing import Any

from .context import Context
from .publish import Publisher
from .schema import MemoryError, digest, links, parse_page, resolve_link, validate_page


class Maintenance:
    def __init__(self, publisher: Publisher) -> None:
        self.publisher = publisher
        self.context = Context(publisher.vault)

    def lint(self, project_id: str) -> list[dict[str, Any]]:
        findings = []
        with self.publisher.locked():
            self.publisher._recover()
            try:
                self.publisher._check_integrity()
            except MemoryError as exc:
                findings.append({"code": "integrity", "detail": str(exc)})
            files = self.context.files(project_id)
            aliases: dict[str, str] = {}
            incoming: set[str] = set()
            for path, content in files.items():
                if "/raw/" in path:
                    if digest(content) != path.rsplit("/", 1)[1][:-3]:
                        findings.append({"path": path, "code": "raw_hash_mismatch"})
                    continue
                for target in links(content):
                    try:
                        incoming.add(resolve_link(target, set(files), path))
                    except MemoryError:
                        findings.append(
                            {"path": path, "code": "broken_link", "target": target}
                        )
                if path.rsplit("/", 2)[-2] not in {"entities", "concepts", "sources"}:
                    continue
                try:
                    meta, _ = parse_page(content)
                    validate_page(content, meta.get("project_id"), meta.get("page_id"))
                    for alias in meta["aliases"]:
                        key = f"{meta['project_id']}:{alias.casefold()}"
                        if key in aliases:
                            findings.append(
                                {
                                    "path": path,
                                    "code": "duplicate_alias",
                                    "other": aliases[key],
                                }
                            )
                        aliases[key] = path
                except (MemoryError, TypeError) as exc:
                    findings.append(
                        {"path": path, "code": "schema", "detail": str(exc)}
                    )
            for path in files:
                if (
                    path.rsplit("/", 2)[-2] in {"entities", "concepts", "sources"}
                    and path not in incoming
                ):
                    findings.append({"path": path, "code": "orphan"})
        return findings

    def request(
        self,
        project_id: str,
        operation: str,
        query: str = "",
        sources: list[str] | None = None,
    ) -> dict[str, Any]:
        if operation not in {"ingest", "query", "lint"}:
            raise MemoryError("invalid_memory_operation")
        with self.publisher.locked():
            self.publisher._recover()
            bundle = self.context.bundle(project_id, query)
            if operation == "ingest":
                files = self.context.files(project_id)
                source_paths = (
                    sources
                    if sources is not None
                    else [p for p in files if "/raw/" in p][-20:]
                )
                if len(source_paths) > 20:
                    raise MemoryError("too_many_ingest_sources")
                for source in source_paths:
                    if source not in files or "/raw/" not in source:
                        raise MemoryError("ingest_source_outside_scope")
                bundle["raw_sources"] = [
                    {"path": p, "content": files[p], "sha256": digest(files[p])}
                    for p in source_paths
                ]
        return {
            "kind": "memory",
            "project_id": project_id,
            "operation": operation,
            "query": query,
            "context": bundle,
            "requires_executor_grant": True,
            "billing": "existing_subscription_only",
        }
