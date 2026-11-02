"""Explicit Second Brain commands over the authenticated local service socket."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from oraculo.memory.transport import MAX_ARTIFACT_BYTES

ACTIONS = {
    "query": "query",
    "source": "source",
    "capture-plan": "capture.plan",
    "capture-apply": "capture.apply",
    "save": "save",
    "audit": "audit",
    "report": "report",
    "compile": "compile",
    "review": "review",
    "migration-plan": "migration.plan",
    "migration-apply": "migration.apply",
    "routine-status": "routine.status",
    "routine-configure": "routine.configure",
    "routine-cancel": "routine.cancel",
    "routine-tick": "routine.tick",
    "pilot-start": "pilot.start",
    "pilot-review": "pilot.review",
    "pilot-status": "pilot.status",
}


def add_parser(commands):
    brain = commands.add_parser(
        "brain", help="Second Brain: captura e consulta explícitas"
    )
    brain.add_argument("action", choices=list(ACTIONS))
    brain.add_argument("--project", required=True)
    brain.add_argument(
        "--file", type=Path, help="Payload JSON selecionado; nunca o vault vivo"
    )
    brain.add_argument(
        "--source-file",
        type=Path,
        action="append",
        help="Captura: arquivo .md/.txt selecionado",
    )
    brain.add_argument("--query")
    brain.add_argument(
        "--confirmation", help="SHA-256 exato do manifesto ou plano revisado"
    )
    brain.add_argument(
        "--apply", action="store_true", help="Publicar proposta explicitamente"
    )
    brain.add_argument(
        "--output", type=Path, help="Salvar resultado JSON em arquivo novo"
    )


def payload(args):
    data = json.loads(args.file.read_text()) if args.file else {}
    if not isinstance(data, dict):
        raise ValueError("brain_payload_object_required")
    if args.source_file:
        if args.action != "capture-plan":
            raise ValueError("brain_source_file_requires_capture_plan")
        data["files"] = [str(path.expanduser().absolute()) for path in args.source_file]
    if args.query is not None:
        data["query"] = args.query
    if args.action == "capture-apply":
        if not args.confirmation:
            raise ValueError("brain_capture_confirmation_required")
        # The plan can be the exact JSON returned by capture-plan.
        if "manifest_sha256" in data and "files" in data:
            data = {"plan": data}
        data["confirmation"] = args.confirmation
    if args.action == "migration-apply":
        if not args.confirmation:
            raise ValueError("brain_migration_confirmation_required")
        plan = data.get("plan", data)
        if plan.get("plan_hash") != args.confirmation:
            raise ValueError("brain_migration_confirmation_mismatch")
        data = {"plan": dict(plan, approval=args.confirmation)}
    if args.action in {"save", "compile", "review"}:
        data["apply"] = args.apply
    return data


async def command(config, args):
    from oraculo.cli import make_envelope, request

    result = await request(
        config,
        make_envelope("knowledge." + ACTIONS[args.action], args.project, payload(args)),
    )
    if not result.get("ok"):
        raise ValueError("brain_service_failed:" + str(result.get("error")))
    data = result["data"]
    if data.get("state") == "artifact":
        data = await download(config, args.project, data["artifact"])
    if args.output:
        # No overwrite, including symlinks. Private results stay private on disk.
        descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
    return data


async def download(config, project, reference):
    from oraculo.cli import make_envelope, request

    sha = reference.get("sha256")
    if not isinstance(sha, str) or len(sha) != 64 or reference.get("artifact") != sha:
        raise ValueError("brain_artifact_reference_invalid")
    if (
        type(reference.get("bytes")) is not int
        or not 0 < reference["bytes"] <= MAX_ARTIFACT_BYTES
    ):
        raise ValueError("brain_artifact_size_invalid")
    parts, offset, size = [], 0, 0
    while size <= MAX_ARTIFACT_BYTES:
        response = await request(
            config,
            make_envelope(
                "knowledge.read", project, {"artifact": sha, "offset": offset}
            ),
        )
        if not response.get("ok"):
            raise ValueError("brain_artifact_read_failed")
        chunk = response["data"]
        text = chunk.get("content")
        if (
            chunk.get("project_id") != project
            or chunk.get("job_id") != "knowledge"
            or chunk.get("sha256") != sha
            or chunk.get("artifact") != sha
            or chunk.get("offset") != offset
            or not isinstance(text, str)
            or chunk.get("next_offset") != offset + len(text)
            or (not text and not chunk.get("eof"))
        ):
            raise ValueError("brain_artifact_chunk_invalid")
        parts.append(text)
        size += len(text.encode())
        offset += len(text)
        if chunk.get("eof"):
            raw = "".join(parts).encode()
            if len(raw) != reference["bytes"] or hashlib.sha256(raw).hexdigest() != sha:
                raise ValueError("brain_artifact_integrity_error")
            body = json.loads(raw)
            if body.get("project_id") != project or body.get("job_id") != "knowledge":
                raise ValueError("brain_artifact_scope_error")
            return body["context"]["result"]
    raise ValueError("brain_artifact_too_large")
