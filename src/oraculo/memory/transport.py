"""Bounded T3 dispatches and immutable, job-scoped memory snapshots."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path, PurePosixPath

from oraculo.protocol import canonical_json
from oraculo.state import StateError

from .schema import corpus, safe_path

MAX_MESSAGE_UNITS = 120000
# Leave room for the coordinator to add grant/identity to the child's brief.
INLINE_MESSAGE_UNITS = 80000
CHUNK_CHARACTERS = 4096
MAX_ARTIFACT_BYTES = 64 * 1024 * 1024


def utf16_units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def compact_memory_body(body: dict) -> dict:
    """Keep the current bundle and one copy of the newly captured evidence."""
    context = dict(body["context"])
    initial = context.get("context", {})
    context["context"] = {"raw_sources": initial.get("raw_sources", [])}
    payload = dict(body["payload"])
    if "source_content" in context and "execution_evidence" in payload:
        payload.pop("execution_evidence")
        payload["execution_source"] = {
            "path": context["raw_path"],
            "sha256": hashlib.sha256(context["source_content"].encode()).hexdigest(),
        }
    if context.get("raw_path"):
        context["context"]["raw_sources"] = [
            source
            for source in context["context"]["raw_sources"]
            if source["path"] != context["raw_path"]
        ]
    return dict(body, context=context, payload=payload)


class MemoryArtifacts:
    def __init__(self, state_dir: Path):
        self.root = state_dir / "memory-contexts"

    def _path(self, sha256: str) -> Path:
        if not isinstance(sha256, str) or not re.fullmatch(r"[a-f0-9]{64}", sha256):
            raise StateError("invalid_memory_artifact")
        return safe_path(self.root, sha256 + ".json")

    @staticmethod
    def _validate_scope(snapshot: dict, project_id: str, job_id: str) -> None:
        if snapshot.get("project_id") != project_id or snapshot.get("job_id") != job_id:
            raise StateError("memory_artifact_scope_mismatch")
        context = snapshot["context"]
        allowed = {corpus(project_id), "shared"}
        bundle = context["bundle"]
        if (
            bundle.get("project_id", project_id) not in {project_id, "shared"}
            or not set(bundle.get("allowed_roots", [])).issubset(allowed)
            or bundle.get("corpus_path", corpus(project_id)) not in allowed
        ):
            raise StateError("memory_artifact_document_outside_scope")
        paths = [d["path"] for d in bundle["documents"]]
        paths += [d["path"] for d in context["context"].get("raw_sources", [])]
        if context.get("raw_path"):
            paths.append(context["raw_path"])
        for path in paths:
            # No path is opened here; validate the manifest's canonical namespace.
            if (
                not isinstance(path, str)
                or ".." in path.split("/")
                or "\\" in path
                or str(PurePosixPath(path)) != path
                or not any(path.startswith(root + "/") for root in allowed)
            ):
                raise StateError("memory_artifact_document_outside_scope")

    def create(self, body: dict) -> dict:
        snapshot = {
            "schema_version": 1,
            "project_id": body["project_id"],
            "job_id": body["job_id"],
            "payload": body["payload"],
            "context": body["context"],
        }
        self._validate_scope(snapshot, body["project_id"], body["job_id"])
        raw = canonical_json(snapshot).encode()
        if len(raw) > MAX_ARTIFACT_BYTES:
            raise StateError("memory_artifact_too_large")
        sha256 = hashlib.sha256(raw).hexdigest()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self._path(sha256)
        # Publish an already-fsynced file without replacing an existing identity.
        descriptor, name = tempfile.mkstemp(prefix=".writing-", dir=self.root)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(name, path)
            except FileExistsError:
                self.load(sha256, body["project_id"], body["job_id"])
            directory = os.open(self.root, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            os.unlink(name)
        return {
            "artifact": sha256,
            "sha256": sha256,
            "bytes": len(raw),
            "format": "oraculo-memory-context-v1",
            "read_via": "memory.read",
            "chunk_characters": CHUNK_CHARACTERS,
        }

    def load(self, sha256: str, project_id: str, job_id: str) -> str:
        path = self._path(sha256)
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as stream:
            if os.fstat(stream.fileno()).st_uid != os.getuid():
                raise StateError("memory_artifact_owner_mismatch")
            raw = stream.read(MAX_ARTIFACT_BYTES + 1)
        if len(raw) > MAX_ARTIFACT_BYTES or hashlib.sha256(raw).hexdigest() != sha256:
            raise StateError("memory_artifact_integrity_error")
        text = raw.decode("utf-8")
        self._validate_scope(json.loads(text), project_id, job_id)
        return text

    def read(self, sha256: str, project_id: str, job_id: str, offset: int = 0) -> dict:
        text = self.load(sha256, project_id, job_id)
        if type(offset) is not int or not 0 <= offset <= len(text):
            raise StateError("invalid_memory_artifact_offset")
        chunk = text[offset : offset + CHUNK_CHARACTERS]
        next_offset = offset + len(chunk)
        return {
            "artifact": sha256,
            "sha256": sha256,
            "project_id": project_id,
            "job_id": job_id,
            "content": chunk,
            "offset": offset,
            "next_offset": next_offset,
            "total_characters": len(text),
            "eof": next_offset == len(text),
        }


def memory_message(body: dict, artifacts: MemoryArtifacts) -> tuple[str, dict | None]:
    body = compact_memory_body(body)
    message = canonical_json(body)
    reference = None
    if utf16_units(message) > INLINE_MESSAGE_UNITS:
        reference = artifacts.create(body)
        body = dict(
            body,
            payload={
                "kind": "memory",
                "memory_operation": body["context"]["operation"],
            },
            context=reference,
        )
        message = canonical_json(body)
    if utf16_units(message) > MAX_MESSAGE_UNITS:
        raise StateError("memory_dispatch_instructions_too_large")
    return message, reference
