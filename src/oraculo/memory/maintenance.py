from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any

from .context import Context
from .publish import Publisher
from .retrieval import estimate_tokens, retrieve
from .schema import (
    MemoryError,
    corpus,
    digest,
    links,
    markdown_targets,
    parse_page,
    project_key,
    resolve_link,
    safe_path,
    validate_page,
)

# Same ceilings as budgeted retrieve. Routines must not invent a larger budget
# or a query whose only purpose is to pass abstention.
DETAIL_TOKENS = retrieve.__kwdefaults__["detail_tokens"]
BRIEF_TOKENS = retrieve.__kwdefaults__["brief_tokens"]
MAX_RAW_SOURCES = 20
MAX_RAW_BYTES = 2 * 1024 * 1024
OPERATION_CONTEXT_VERSION = "f6-operation-context-v1"


class Maintenance:
    def __init__(self, publisher: Publisher) -> None:
        self.publisher = publisher
        self.context = Context(publisher.vault)

    def lint(self, project_id: str) -> list[dict[str, Any]]:
        with self.publisher.locked():
            self.publisher._recover()
            return self._lint_locked(project_id)

    def _lint_locked(self, project_id: str) -> list[dict[str, Any]]:
        findings = []
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
                findings.append({"path": path, "code": "schema", "detail": str(exc)})
        for path in files:
            if (
                path.rsplit("/", 2)[-2] in {"entities", "concepts", "sources"}
                and path not in incoming
            ):
                findings.append({"path": path, "code": "orphan"})
        return findings

    def ingest_bundle(
        self,
        project_id: str,
        raw_sources: list[dict[str, Any]] | None,
        *,
        read_only: bool = False,
    ) -> dict[str, Any]:
        """Canonical snapshot for an empty-query ingest.

        Caller holds the publisher lock. Pages are chosen because they cite the
        frozen raw selection, not because a query was invented for the ranker.
        ``read_only`` is recorded on the bundle policy. It does not publish and
        does not replace the job-level ``memory.propose`` rejection.
        """
        if raw_sources is None:
            raw_sources = []
        if len(raw_sources) > MAX_RAW_SOURCES:
            raise MemoryError("too_many_ingest_sources")
        files = self.context.files(project_id)
        own = corpus(project_id)
        accepted, conflicts = self._frozen_sources(project_id, raw_sources, files)
        frozen = {item["path"] for item in accepted}
        scoped = set(files)
        relevant = [
            path
            for path, content in sorted(files.items())
            if _is_canonical(path) and _cites(content, path, frozen, scoped)
        ]
        own_relevant = [path for path in relevant if path.startswith(own + "/")]
        shared_relevant = [path for path in relevant if path.startswith("shared/")]
        own_canonical = [
            path for path in files if path.startswith(own + "/") and _is_canonical(path)
        ]
        bases, reviewed, page_conflicts = _page_records(own_relevant, files)
        conflicts.extend(page_conflicts)
        index = _index_records(files, own)
        documents, oversize = _pack(
            _existing(files, [index[0]["path"], *own_relevant, *shared_relevant]),
            files,
            {item["path"]: item["reviewed"] for item in bases},
        )
        if not own_canonical:
            state = "empty_canon"
        elif not bases:
            state = "no_relevant_bases"
        else:
            state = "bounded"
        coverage = _coverage(
            state=state,
            canonical_pages=len(own_canonical),
            raw_sources=len(accepted),
            documents=documents,
            oversize=oversize,
            pages_scanned=len(files),
            findings=conflicts,
            bytes_scanned=_bytes(files),
        )
        coverage["relevant_canonical_pages"] = len(bases)
        return _operation_bundle(
            project_id,
            operation="ingest",
            documents=documents,
            findings=conflicts,
            files=files,
            coverage=coverage,
            extra={
                "bases": bases,
                "reviewed": reviewed,
                "conflicts": conflicts,
                "source_hashes": {item["path"]: item["sha256"] for item in accepted},
                "index": index,
                "oversize": oversize,
                "readable_shared": shared_relevant,
                "policy": _policy(own, read_only=read_only),
            },
        )

    def lint_bundle(self, project_id: str, *, read_only: bool = True) -> dict[str, Any]:
        """Deterministic lint snapshot. Caller holds the publisher lock.

        ``read_only`` is forced on: a lint dispatch cannot publish.
        """
        del read_only
        files = self.context.files(project_id)
        findings = self._lint_locked(project_id)
        own = corpus(project_id)
        own_canonical = [
            path for path in files if path.startswith(own + "/") and _is_canonical(path)
        ]
        own_raw = [path for path in files if path.startswith(own + "/raw/")]
        if not own_canonical and not own_raw:
            state = "no_content"
        elif findings:
            state = "findings"
        else:
            state = "inspected"
        interesting = []
        for finding in findings:
            path = finding.get("path")
            if (
                finding.get("code")
                in {
                    "broken_link",
                    "duplicate_alias",
                    "schema",
                    "raw_hash_mismatch",
                    "integrity",
                }
                and path in files
                and path not in interesting
            ):
                interesting.append(path)
        index = _index_records(files, own)
        documents, oversize = _pack(
            _existing(files, [index[0]["path"], *interesting]),
            files,
            {},
        )
        reviewed = []
        for path in sorted(own_canonical):
            try:
                meta, _ = parse_page(files[path])
            except MemoryError:
                continue
            if meta.get("reviewed") is True:
                reviewed.append({"path": path, "sha256": digest(files[path])})
        return _operation_bundle(
            project_id,
            operation="lint",
            documents=documents,
            findings=findings,
            files=files,
            coverage=_coverage(
                state=state,
                canonical_pages=len(own_canonical),
                raw_sources=len(own_raw),
                documents=documents,
                oversize=oversize,
                pages_scanned=len(files),
                findings=findings,
                bytes_scanned=_bytes(files),
            ),
            extra={
                "reviewed": reviewed,
                "index": index,
                "oversize": oversize,
                "policy": _policy(own, read_only=True),
            },
        )

    def _frozen_sources(
        self,
        project_id: str,
        raw_sources: list[dict[str, Any]],
        files: dict[str, str],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Accept only frozen raw that already matches the scoped vault.

        Invalid input raises before a bundle exists. The declared hash is kept
        when it matches and is never rewritten to the bytes on disk.
        """
        own_raw = corpus(project_id) + "/raw"
        accepted = []
        for source in raw_sources:
            if not isinstance(source, dict):
                raise MemoryError("ingest_source_outside_scope")
            path = source.get("path")
            content = source.get("content")
            sha = source.get("sha256")
            if (
                not isinstance(path, str)
                or not isinstance(content, str)
                or not isinstance(sha, str)
            ):
                raise MemoryError("ingest_source_outside_scope")
            path = _canonical_raw_path(path, own_raw)
            if path not in files:
                raise MemoryError("ingest_source_outside_scope")
            encoded = len(content.encode("utf-8"))
            if encoded > MAX_RAW_BYTES:
                raise MemoryError("source_too_large")
            if files[path] != content:
                raise MemoryError("source_hash_mismatch")
            actual = digest(content)
            name = path.rsplit("/", 1)[-1]
            if actual != sha or not name.endswith(".md") or actual != name[:-3]:
                raise MemoryError("source_hash_mismatch")
            accepted.append(
                {
                    "path": path,
                    "sha256": sha,
                    "bytes": encoded,
                    "mutable": not path.startswith("shared/raw/"),
                }
            )
        return accepted, []

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
            if operation == "ingest":
                self.publisher._recover()
            else:
                self.publisher._check_integrity()
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


def _is_canonical(path: str) -> bool:
    return (
        "/wiki/" in path
        and "/schema/" not in path
        and not path.endswith("/log.md")
        and not path.endswith("/index.md")
        and path.rsplit("/", 2)[-2] in {"entities", "concepts", "sources"}
    )


def _canonical_raw_path(path: str, own_raw: str) -> str:
    pure = PurePosixPath(path)
    if (
        not path
        or path.startswith(("/", "~"))
        or "\\" in path
        or ".." in pure.parts
        or pure.is_absolute()
        or str(pure) != path
        or pure.suffix != ".md"
        or str(pure.parent) not in {own_raw, "shared/raw"}
    ):
        raise MemoryError("ingest_source_outside_scope")
    return path


def _cites(content: str, page: str, frozen: set[str], scoped: set[str]) -> bool:
    """True when a link resolves unambiguously to one frozen raw path.

    ``resolve_link`` is the publisher's resolver: full path, raw stem,
    ``filename.md``, and a path relative to ``page``. Ambiguous, unsafe, and
    out-of-scope targets do not count.
    """
    if not frozen:
        return False
    for target in (*links(content), *markdown_targets(content)):
        try:
            resolved = resolve_link(target, scoped, page)
        except MemoryError:
            continue
        if resolved in frozen:
            return True
    return False


def _page_records(
    paths: list[str], files: dict[str, str]
) -> tuple[list[dict[str, Any]], list[dict[str, str]], list[dict[str, Any]]]:
    bases = []
    reviewed = []
    conflicts = []
    aliases: dict[str, str] = {}
    for path in paths:
        content = files[path]
        sha = digest(content)
        try:
            meta, _ = parse_page(content)
        except MemoryError as exc:
            conflicts.append({"path": path, "code": "schema", "detail": str(exc)})
            bases.append(
                {"path": path, "sha256": sha, "reviewed": False, "page_id": None}
            )
            continue
        is_reviewed = meta.get("reviewed") is True
        bases.append(
            {
                "path": path,
                "sha256": sha,
                "reviewed": is_reviewed,
                "page_id": meta.get("page_id"),
            }
        )
        if is_reviewed:
            reviewed.append({"path": path, "sha256": sha})
            conflicts.append({"path": path, "code": "reviewed_page_protected"})
        for target in links(content):
            try:
                resolve_link(target, set(files), path)
            except MemoryError:
                conflicts.append(
                    {"path": path, "code": "broken_link", "target": target}
                )
        for alias in meta.get("aliases") or []:
            if not isinstance(alias, str):
                continue
            key = f"{meta.get('project_id')}:{alias.casefold()}"
            if key in aliases:
                conflicts.append(
                    {"path": path, "code": "duplicate_alias", "other": aliases[key]}
                )
            else:
                aliases[key] = path
        for claim in meta.get("claims") or []:
            if isinstance(claim, dict) and claim.get("support") == "contested":
                conflicts.append(
                    {"path": path, "code": "contested_claim", "id": claim.get("id")}
                )
    return bases, reviewed, conflicts


def _index_records(files: dict[str, str], own: str) -> list[dict[str, Any]]:
    records = []
    for path in (f"{own}/wiki/index.md", "shared/wiki/index.md"):
        content = files.get(path)
        records.append(
            {
                "path": path,
                "present": content is not None,
                "sha256": digest(content) if content is not None else None,
            }
        )
    return records


def _existing(files: dict[str, str], paths: list[str]) -> list[str]:
    seen = set()
    ordered = []
    for path in paths:
        if path in files and path not in seen:
            seen.add(path)
            ordered.append(path)
    return ordered


def _pack(
    paths: list[str], files: dict[str, str], reviewed_by_path: dict[str, bool]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    documents = []
    oversize = []
    remaining = DETAIL_TOKENS
    for path in paths:
        content = files[path]
        tokens = estimate_tokens(content)
        record = {
            "path": path,
            "sha256": digest(content),
            "tokens": tokens,
            "bytes": len(content.encode("utf-8")),
            "coverage": 0,
            "reviewed": reviewed_by_path.get(path),
        }
        if tokens > DETAIL_TOKENS or tokens > remaining:
            record["reason"] = (
                "exceeds_detail_budget"
                if tokens > DETAIL_TOKENS
                else "exceeds_remaining_budget"
            )
            oversize.append(record)
            continue
        documents.append({"path": path, "content": content, "sha256": record["sha256"]})
        remaining -= tokens
    return documents, oversize


def _bytes(files: dict[str, str]) -> int:
    return sum(len(content.encode("utf-8")) for content in files.values())


def _coverage(
    *,
    state: str,
    canonical_pages: int,
    raw_sources: int,
    documents: list[dict[str, Any]],
    oversize: list[dict[str, Any]],
    pages_scanned: int,
    findings: list[dict[str, Any]],
    bytes_scanned: int,
) -> dict[str, Any]:
    return {
        "state": state,
        "corpus_acceptance": False,
        "canonical_pages": canonical_pages,
        "raw_sources": raw_sources,
        "documents_included": len(documents),
        "references_omitted": len(oversize),
        "pages_scanned": pages_scanned,
        "finding_count": len(findings),
        "bytes_scanned": bytes_scanned,
        "bytes_selected": sum(
            len(document["content"].encode("utf-8")) for document in documents
        ),
    }


def _policy(own: str, *, read_only: bool) -> dict[str, Any]:
    return {
        "read_only": read_only,
        "shared_promotion": False,
        "autoapproval": False,
        "mutable_root": own,
        "max_raw_sources": MAX_RAW_SOURCES,
        "max_raw_bytes": MAX_RAW_BYTES,
        "detail_tokens": DETAIL_TOKENS,
        "brief_tokens": BRIEF_TOKENS,
    }


PREPARED_COMPILATION_SCHEMA = "f6-prepared-compilation-v1"
DEFAULT_NIGHTLY_CONCEPT = "Compilação noturna incremental"
_CALLER_PREPARED_KEYS = frozenset(
    {
        "prepared_compilation",
        "compilation_receipt",
        "compiler_preview",
        "proposal",
        "target_exists",
        "proposal_sha256",
        "base_sha256",
    }
)


def nightly_compilation_receipt(
    publisher: Any,
    project_id: str,
    payload: dict[str, Any],
    bundle: dict[str, Any],
    *,
    blocked_code: str | None = None,
) -> dict[str, Any]:
    """Compact authoritative preview for one nightly concept.

    ``apply`` stays false. The proposal body is not copied into the receipt.
    Caller-supplied prepared metadata cannot mark the preview ready.
    """
    from oraculo.knowledge import validate_nightly_proposal
    from oraculo.memory.compiler import (
        compile_knowledge,
        concept_page_id,
        concept_page_path,
    )

    receipt = _blank_receipt(bundle)
    receipt["caller_prepared_metadata"] = bool(_CALLER_PREPARED_KEYS & payload.keys())
    concept, concept_error = _nightly_concept(payload)
    receipt["concept"] = concept
    registry = bundle.get("base_commit")
    if concept_error:
        receipt["status"] = "blocked"
        receipt["code"] = concept_error
        return _seal_receipt(receipt, payload)
    page_id = concept_page_id(project_id, concept)
    path = concept_page_path(project_id, page_id)
    receipt["page_id"] = page_id
    receipt["target_path"] = path
    sources = payload.get("knowledge_sources")
    if blocked_code:
        receipt["status"] = "blocked"
        receipt["code"] = blocked_code
        receipt["selection_refused"] = True
        _observe_target(publisher, receipt)
        return _seal_receipt(receipt, payload)
    if payload.get("memory_scope") == "shared":
        receipt["status"] = "blocked"
        receipt["code"] = "shared_promotion_refused"
        _observe_target(publisher, receipt)
        return _seal_receipt(receipt, payload)
    if (
        not isinstance(sources, list)
        or not sources
        or not all(isinstance(item, str) for item in sources)
    ):
        receipt["status"] = "blocked"
        receipt["code"] = "knowledge_routine_selection_required"
        _observe_target(publisher, receipt)
        return _seal_receipt(receipt, payload)
    ordered: list[str] = []
    for item in sources:
        if item not in ordered:
            ordered.append(item)
    receipt["frozen_source_count"] = len(ordered)
    proposal: dict[str, Any] | None = None
    with publisher.locked():
        publisher._check_integrity()
        head = publisher.git.head()
        receipt["registry_base_commit"] = head
        if head != registry:
            receipt["status"] = "blocked"
            receipt["code"] = "registry_base_changed"
            return _seal_receipt(receipt, payload)
        try:
            preview = compile_knowledge(
                publisher,
                project_id,
                ordered,
                concept=concept,
                apply=False,
            )
        except MemoryError as exc:
            receipt["status"] = "blocked"
            receipt["code"] = str(exc)
            _observe_target(publisher, receipt)
            return _seal_receipt(receipt, payload)
        proposal = _bind_preview(publisher, project_id, receipt, preview, ordered)
    if (
        receipt["status"] == "ready"
        and isinstance(proposal, dict)
        and receipt["preview_object"] == "proposal"
    ):
        try:
            validate_nightly_proposal(publisher, project_id, proposal, ordered)
        except MemoryError as exc:
            receipt["status"] = "blocked"
            receipt["code"] = str(exc)
            receipt["proposal_sha256"] = None
    return _seal_receipt(receipt, payload)


def _nightly_concept(payload: dict[str, Any]) -> tuple[str | None, str | None]:
    from oraculo.memory.compiler import _explicit_text

    if "knowledge_concept" in payload:
        value = payload["knowledge_concept"]
    elif "concept" in payload:
        value = payload["concept"]
    else:
        value = DEFAULT_NIGHTLY_CONCEPT
    try:
        return _explicit_text(value, "invalid_concept"), None
    except MemoryError as exc:
        return None, str(exc)


def _blank_receipt(bundle: dict[str, Any]) -> dict[str, Any]:
    selection = bundle.get("selection")
    coverage: dict[str, Any] = {}
    if isinstance(selection, dict) and isinstance(selection.get("coverage"), dict):
        raw = selection["coverage"]
        for key in (
            "state",
            "canonical_pages",
            "relevant_canonical_pages",
            "documents_included",
            "references_omitted",
        ):
            if key in raw:
                coverage[key] = raw[key]
    coverage["corpus_acceptance"] = False
    return {
        "schema": PREPARED_COMPILATION_SCHEMA,
        "status": "blocked",
        "code": None,
        "concept": None,
        "page_id": None,
        "target_path": None,
        "target_exists": None,
        "reviewed": None,
        "base_sha256": None,
        "proposal_base_commit": None,
        "registry_base_commit": bundle.get("base_commit"),
        "base_contract": (
            "proposal.base_commit null is the compiler's optional global base; "
            "page CAS is base_sha256 and source CAS is source_hashes; "
            "registry_base_commit is coverage provenance only"
        ),
        "alias_conflicts": [],
        "alias_pages_checked": 0,
        "coverage": coverage,
        "frozen_sources": [],
        "frozen_source_count": None,
        "frozen_manifest_sha256": None,
        "proposal_source_hashes": {},
        "retained_source_hashes": {},
        "retained_claim_ids": [],
        "new_claim_count": None,
        "claim_count": None,
        "compiler_state": None,
        "operation_id": None,
        "proposal_sha256": None,
        "preview_sha256": None,
        "preview_object": None,
        "proposal_bytes": None,
        "change_count": None,
        "compiler_finding_counts": {},
        "selection_refused": False,
        "unrelated_canon_required": False,
        "receipt_publishes": False,
        "semantic_support": False,
        "autoapproval": False,
        "shared_promotion": False,
        "caller_prepared_metadata": False,
        "lookup": None,
        "next_action": "report_blocked",
    }


def _bind_preview(
    publisher: Any,
    project_id: str,
    receipt: dict[str, Any],
    preview: dict[str, Any],
    ordered: list[str],
) -> dict[str, Any] | None:
    from oraculo.protocol import canonical_json, content_hash

    state = preview.get("state")
    receipt["compiler_state"] = state
    receipt["operation_id"] = preview.get("operation_id")
    if isinstance(preview.get("page_id"), str):
        receipt["page_id"] = preview["page_id"]
    if isinstance(preview.get("path"), str):
        receipt["target_path"] = preview["path"]
    counts: dict[str, int] = {}
    for finding in preview.get("findings") or []:
        if isinstance(finding, dict) and isinstance(finding.get("code"), str):
            counts[finding["code"]] = counts.get(finding["code"], 0) + 1
    receipt["compiler_finding_counts"] = counts
    _observe_target(publisher, receipt)
    receipt["alias_pages_checked"] = _alias_pages_checked(publisher, project_id)
    proposal = preview.get("proposal")
    suggestion = preview.get("suggestion")
    obj = proposal if isinstance(proposal, dict) else None
    kind = "proposal" if obj is not None else None
    if obj is None and isinstance(suggestion, dict):
        obj = suggestion
        kind = "suggestion"
    if state == "noop":
        receipt["status"] = "noop"
        receipt["code"] = "sources_already_incorporated"
        receipt["next_action"] = "report_no_changes"
        _lookup(receipt, ordered, obj, kind)
        return None
    if state == "suggestion" or receipt.get("reviewed") is True:
        receipt["status"] = "review_required"
        receipt["code"] = "reviewed_page_protected"
        receipt["next_action"] = "report_review_required"
        _hash_preview(receipt, obj, kind)
        _lookup(receipt, ordered, obj, kind)
        if isinstance(obj, dict):
            _note_aliases(publisher, project_id, receipt, obj)
        return None
    if state != "proposed" or obj is None or kind != "proposal":
        receipt["status"] = "blocked"
        receipt["code"] = next(iter(counts), None) or "compiler_" + str(state)
        _hash_preview(receipt, obj, kind)
        _lookup(receipt, ordered, obj, kind)
        return None
    encoded = canonical_json(obj).encode()
    receipt["proposal_bytes"] = len(encoded)
    receipt["preview_sha256"] = content_hash(obj)
    receipt["preview_object"] = "proposal"
    receipt["proposal_sha256"] = receipt["preview_sha256"]
    changes = obj.get("changes")
    if not isinstance(changes, list) or len(changes) != 1:
        receipt["status"] = "blocked"
        receipt["code"] = "expected_single_target"
        receipt["proposal_sha256"] = None
        receipt["change_count"] = len(changes) if isinstance(changes, list) else None
        _lookup(receipt, ordered, obj, "proposal")
        return None
    change = changes[0]
    receipt["change_count"] = 1
    if (
        not isinstance(change, dict)
        or change.get("path") != receipt["target_path"]
        or change.get("page_id") != receipt["page_id"]
        or obj.get("project_id") != project_id
    ):
        receipt["status"] = "blocked"
        receipt["code"] = "target_mismatch"
        receipt["proposal_sha256"] = None
        _lookup(receipt, ordered, obj, "proposal")
        return None
    proposal_base = obj.get("base_commit", "missing")
    if proposal_base == "missing":
        receipt["status"] = "blocked"
        receipt["code"] = "proposal_base_missing"
        receipt["proposal_sha256"] = None
        return None
    receipt["proposal_base_commit"] = proposal_base
    if proposal_base is not None and proposal_base != receipt["registry_base_commit"]:
        receipt["status"] = "blocked"
        receipt["code"] = "proposal_base_unbound"
        receipt["proposal_sha256"] = None
        _lookup(receipt, ordered, obj, "proposal")
        return None
    page_base = change.get("base_sha256", "missing")
    if page_base == "missing" or page_base != receipt["base_sha256"]:
        receipt["status"] = "blocked"
        receipt["code"] = "page_cas_mismatch"
        receipt["proposal_sha256"] = None
        _lookup(receipt, ordered, obj, "proposal")
        return None
    source_hashes = obj.get("source_hashes")
    if not isinstance(source_hashes, dict):
        receipt["status"] = "blocked"
        receipt["code"] = "source_hashes_required"
        receipt["proposal_sha256"] = None
        return None
    if not _bind_sources(publisher, receipt, ordered, source_hashes):
        _lookup(receipt, ordered, obj, "proposal")
        return None
    if not _bind_claims(receipt, change):
        _lookup(receipt, ordered, obj, "proposal")
        return None
    conflicts = _alias_conflicts(publisher, project_id, receipt, change)
    receipt["alias_conflicts"] = conflicts
    if conflicts:
        receipt["status"] = "blocked"
        receipt["code"] = "alias_conflict"
        receipt["proposal_sha256"] = None
        _lookup(receipt, ordered, obj, "proposal")
        return None
    receipt["status"] = "ready"
    receipt["code"] = None
    receipt["next_action"] = "lookup_then_grant_bound_memory_propose"
    _lookup(receipt, ordered, obj, "proposal")
    return obj


def _hash_preview(
    receipt: dict[str, Any], obj: dict[str, Any] | None, kind: str | None
) -> None:
    from oraculo.protocol import canonical_json, content_hash

    if not isinstance(obj, dict) or kind is None:
        return
    receipt["preview_object"] = kind
    receipt["preview_sha256"] = content_hash(obj)
    receipt["proposal_bytes"] = len(canonical_json(obj).encode())


def _bind_sources(
    publisher: Any,
    receipt: dict[str, Any],
    ordered: list[str],
    source_hashes: dict[Any, Any],
) -> bool:
    from oraculo.protocol import content_hash

    frozen = []
    for path in ordered:
        expected = source_hashes.get(path)
        if not isinstance(expected, str) or not _hash_matches(
            publisher, path, expected
        ):
            receipt["status"] = "blocked"
            receipt["code"] = "frozen_source_not_bound"
            receipt["proposal_sha256"] = None
            return False
        frozen.append({"path": path, "sha256": expected})
    retained = {}
    for path, expected in source_hashes.items():
        if path in ordered:
            continue
        if not isinstance(path, str) or not isinstance(expected, str):
            receipt["status"] = "blocked"
            receipt["code"] = "retained_source_invalid"
            receipt["proposal_sha256"] = None
            return False
        if not _hash_matches(publisher, path, expected):
            receipt["status"] = "blocked"
            receipt["code"] = "retained_source_mismatch"
            receipt["proposal_sha256"] = None
            return False
        retained[path] = expected
    receipt["frozen_sources"] = frozen
    receipt["frozen_manifest_sha256"] = content_hash(frozen)
    receipt["proposal_source_hashes"] = {
        path: source_hashes[path] for path in sorted(source_hashes)
    }
    receipt["retained_source_hashes"] = retained
    return True


def _hash_matches(publisher: Any, path: str, expected: str) -> bool:
    try:
        target = safe_path(publisher.vault, path)
    except MemoryError:
        return False
    if not target.is_file():
        return False
    return digest(target.read_bytes()) == expected


def _bind_claims(receipt: dict[str, Any], change: dict[str, Any]) -> bool:
    content = change.get("content")
    if not isinstance(content, str):
        receipt["status"] = "blocked"
        receipt["code"] = "target_mismatch"
        receipt["proposal_sha256"] = None
        return False
    try:
        meta, _body = parse_page(content)
    except MemoryError as exc:
        receipt["status"] = "blocked"
        receipt["code"] = str(exc)
        receipt["proposal_sha256"] = None
        return False
    if meta.get("reviewed") is True:
        receipt["status"] = "review_required"
        receipt["code"] = "reviewed_page_protected"
        receipt["reviewed"] = True
        receipt["proposal_sha256"] = None
        receipt["next_action"] = "report_review_required"
        return False
    proposed_ids = _claim_ids(meta.get("claims"))
    current_ids = _current_claim_ids(receipt)
    if any(item not in proposed_ids for item in current_ids):
        receipt["status"] = "blocked"
        receipt["code"] = "retained_claim_missing"
        receipt["proposal_sha256"] = None
        return False
    retained = [item for item in current_ids if item in proposed_ids]
    receipt["retained_claim_ids"] = retained
    receipt["claim_count"] = len(proposed_ids)
    receipt["new_claim_count"] = len(proposed_ids) - len(retained)
    return True


def _current_claim_ids(receipt: dict[str, Any]) -> list[str]:
    return receipt.get("_current_claim_ids", [])


def _claim_ids(claims: Any) -> list[str]:
    if not isinstance(claims, list):
        return []
    return [
        claim["id"]
        for claim in claims
        if isinstance(claim, dict) and isinstance(claim.get("id"), str)
    ]


def _observe_target(publisher: Any, receipt: dict[str, Any]) -> None:
    path = receipt.get("target_path")
    if not isinstance(path, str):
        return
    try:
        target = safe_path(publisher.vault, path)
    except MemoryError:
        receipt["status"] = "blocked"
        receipt["code"] = "unsafe_path"
        return
    if not target.is_file():
        receipt["target_exists"] = False
        receipt["base_sha256"] = None
        receipt["reviewed"] = False
        receipt["_current_claim_ids"] = []
        return
    text = target.read_text(encoding="utf-8")
    receipt["target_exists"] = True
    receipt["base_sha256"] = digest(text)
    try:
        meta, _body = parse_page(text)
    except MemoryError:
        receipt["reviewed"] = None
        receipt["_current_claim_ids"] = []
        return
    receipt["reviewed"] = meta.get("reviewed") is True
    receipt["_current_claim_ids"] = _claim_ids(meta.get("claims"))


def _alias_pages_checked(publisher: Any, project_id: str) -> int:
    files = Context(publisher.vault).files(project_id)
    own = corpus(project_id) + "/"
    return sum(1 for path in files if path.startswith(own) and _is_canonical(path))


def _note_aliases(
    publisher: Any,
    project_id: str,
    receipt: dict[str, Any],
    preview: dict[str, Any],
) -> None:
    changes = preview.get("changes")
    if isinstance(changes, list) and len(changes) == 1:
        receipt["alias_conflicts"] = _alias_conflicts(
            publisher, project_id, receipt, changes[0]
        )


def _alias_conflicts(
    publisher: Any,
    project_id: str,
    receipt: dict[str, Any],
    change: dict[str, Any],
) -> list[dict[str, str]]:
    content = change.get("content") if isinstance(change, dict) else None
    if not isinstance(content, str):
        return []
    try:
        meta, _body = parse_page(content)
    except MemoryError:
        return []
    proposed = [
        alias
        for alias in meta.get("aliases") or []
        if isinstance(alias, str) and alias.strip()
    ]
    if not proposed:
        return []
    files = Context(publisher.vault).files(project_id)
    own = corpus(project_id) + "/"
    target = receipt.get("target_path")
    owners: dict[str, str] = {}
    checked = 0
    for path, page in files.items():
        if not path.startswith(own) or not _is_canonical(path):
            continue
        checked += 1
        if path == target:
            continue
        try:
            existing, _body = parse_page(page)
        except MemoryError:
            continue
        if existing.get("project_id") not in (None, project_id):
            continue
        for alias in existing.get("aliases") or []:
            if isinstance(alias, str):
                owners.setdefault(alias.casefold(), path)
    receipt["alias_pages_checked"] = checked
    conflicts = []
    for alias in proposed:
        other = owners.get(alias.casefold())
        if other:
            conflicts.append({"alias": alias, "path": str(target), "other": other})
    return conflicts


def _lookup(
    receipt: dict[str, Any],
    ordered: list[str],
    obj: dict[str, Any] | None,
    kind: str | None,
) -> None:
    source_hashes = {}
    proposal_base = None
    page_base = receipt.get("base_sha256")
    if isinstance(obj, dict):
        proposal_base = obj.get("base_commit")
        raw_hashes = obj.get("source_hashes")
        if isinstance(raw_hashes, dict):
            source_hashes = raw_hashes
        changes = obj.get("changes")
        if (
            isinstance(changes, list)
            and len(changes) == 1
            and isinstance(changes[0], dict)
        ):
            page_base = changes[0].get("base_sha256")
    receipt["lookup"] = {
        "interface": "knowledge.compile",
        "cli": ["brain", "compile"],
        "apply": False,
        "payload": {"concept": receipt.get("concept"), "sources": list(ordered)},
        "hash_algorithm": "sha256-canonical-json-v1",
        "preview_object": kind,
        "expect": {
            "state": receipt.get("compiler_state"),
            "operation_id": receipt.get("operation_id"),
            "page_id": receipt.get("page_id"),
            "path": receipt.get("target_path"),
            "proposal_base_commit": proposal_base,
            "base_sha256": page_base,
            "source_manifest_sha256": receipt.get("frozen_manifest_sha256"),
            "preview_sha256": receipt.get("preview_sha256"),
            "source_hashes": source_hashes,
        },
    }


def _seal_receipt(receipt: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    receipt.pop("_current_claim_ids", None)
    receipt["receipt_publishes"] = False
    receipt["semantic_support"] = False
    receipt["autoapproval"] = False
    receipt["shared_promotion"] = False
    receipt["unrelated_canon_required"] = False
    coverage = receipt.get("coverage")
    if isinstance(coverage, dict):
        coverage["corpus_acceptance"] = False
    if payload.get("memory_scope") == "shared":
        receipt["status"] = "blocked"
        receipt["code"] = "shared_promotion_refused"
        receipt["proposal_sha256"] = None
    elif receipt["caller_prepared_metadata"]:
        receipt["status"] = "blocked"
        receipt["code"] = "caller_prepared_metadata_refused"
        receipt["proposal_sha256"] = None
    elif payload.get("read_only") is True and receipt["status"] == "ready":
        receipt["status"] = "blocked"
        receipt["code"] = "read_only_memory_operation"
        receipt["proposal_sha256"] = None
    receipt["next_action"] = {
        "ready": "lookup_then_grant_bound_memory_propose",
        "noop": "report_no_changes",
        "review_required": "report_review_required",
        "blocked": "report_blocked",
    }.get(receipt["status"], "report_blocked")
    return receipt


def _operation_bundle(
    project_id: str,
    *,
    operation: str,
    documents: list[dict[str, Any]],
    findings: list[dict[str, Any]],
    files: dict[str, str],
    coverage: dict[str, Any],
    extra: dict[str, Any],
) -> dict[str, Any]:
    own = corpus(project_id)
    bundle = {
        "project_id": project_id,
        "project_key": project_key(project_id),
        "corpus_path": own,
        "page_prefix": project_key(project_id) + "--",
        "allowed_roots": list(dict.fromkeys((own, "shared"))),
        "documents": documents,
        "findings": findings,
        "index_first": bool(documents)
        and documents[0]["path"].endswith("/wiki/index.md"),
        "selection": {
            "version": OPERATION_CONTEXT_VERSION,
            "operation": operation,
            "estimator": "utf8_bytes_div_4_v1",
            "budget": {
                "detail_tokens": DETAIL_TOKENS,
                "brief_tokens": BRIEF_TOKENS,
            },
            "abstained": False,
            "abstain_reason": None,
            "compatibility": None,
            "coverage": coverage,
            "tokens": {
                "detail": sum(estimate_tokens(item["content"]) for item in documents),
                "brief": 0,
            },
            "pages_read": len(files),
            "pages_selected": len(documents),
            "bytes_read": _bytes(files),
            "bytes_selected": coverage["bytes_selected"],
        },
    }
    bundle["selection"].update(extra)
    return bundle
