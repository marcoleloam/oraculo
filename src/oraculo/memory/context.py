from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .schema import MemoryError, corpus, links, project_key, resolve_link, safe_path


class Context:
    def __init__(self, vault: Path) -> None:
        self.vault = Path(vault)

    def files(self, project_id: str) -> dict[str, str]:
        result = {}
        for prefix in dict.fromkeys((corpus(project_id), "shared")):
            root = safe_path(self.vault, prefix)
            if not root.exists():
                continue
            marker = safe_path(self.vault, f"{prefix}/.oraculo-project.json")
            expected_id = "shared" if prefix == "shared" else project_id
            if (
                marker.exists()
                and json.loads(marker.read_bytes())["project_id"] != expected_id
            ):
                raise MemoryError("project_key_collision")
            for path in sorted(root.rglob("*")):
                relative = path.relative_to(self.vault).as_posix()
                safe_path(self.vault, relative)
                if path.is_file() and path.suffix == ".md":
                    result[relative] = path.read_bytes().decode("utf-8")
        return result

    def bundle(
        self, project_id: str, query: str = "", limit: int = 20
    ) -> dict[str, Any]:
        if not 1 <= limit <= 100:
            raise MemoryError("invalid_context_limit")
        all_files = self.files(project_id)
        paths = set(all_files)
        prefix = corpus(project_id)
        indexes = [
            p
            for p in (f"{prefix}/wiki/index.md", "shared/wiki/index.md")
            if p in all_files
        ]
        terms = set(re.findall(r"\w+", query.casefold()))
        candidates = [
            p
            for p in paths
            if "/wiki/" in p
            and "/schema/" not in p
            and not p.endswith("/log.md")
            and p not in indexes
        ]
        candidates.sort(
            key=lambda p: (-sum(term in all_files[p].casefold() for term in terms), p)
        )
        selected = list(dict.fromkeys(indexes + candidates[:limit]))
        warnings = []
        for path in tuple(selected):
            for link in links(all_files[path]):
                try:
                    linked = resolve_link(link, paths, path)
                except MemoryError:
                    warnings.append(
                        {
                            "path": path,
                            "code": "out_of_scope_or_broken_link",
                            "target": link,
                        }
                    )
                    continue
                if linked not in selected and len(selected) < limit + len(indexes):
                    selected.append(linked)
        documents = []
        for path in selected:
            content = all_files[path]
            for target in links(content):
                try:
                    resolve_link(target, paths, path)
                except MemoryError:
                    content = re.sub(
                        r"\[\[" + re.escape(target) + r"(?:\|[^\]]*)?\]\]",
                        "[referência fora do escopo omitida]",
                        content,
                    )
            documents.append({"path": path, "content": content})
        return {
            "project_id": project_id,
            "project_key": project_key(project_id),
            "corpus_path": corpus(project_id),
            "page_prefix": project_key(project_id) + "--",
            "allowed_roots": list(dict.fromkeys((corpus(project_id), "shared"))),
            "documents": documents,
            "findings": warnings,
            "index_first": True,
        }
