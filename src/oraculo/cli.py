from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import sys
import uuid
from pathlib import Path

from oraculo.config import Config, resources_root
from oraculo.protocol import (
    MAX_ENVELOPE_BYTES,
    canonical_json,
    parse_envelope,
    validate_memory_submission,
)

DEFAULT_CONFIG = Path.home() / ".config/oraculo/config.toml"


async def request(config: Config, envelope: dict) -> dict:
    raw = canonical_json(envelope).encode()
    parse_envelope(raw)
    async with asyncio.timeout(5):
        reader, writer = await asyncio.open_unix_connection(
            str(config.socket_path), limit=MAX_ENVELOPE_BYTES + 1
        )
        try:
            writer.write(raw + b"\n")
            await writer.drain()
            result = await reader.readline()
            if not result or len(result) > MAX_ENVELOPE_BYTES:
                raise ValueError("invalid_response")
            return json.loads(result)
        finally:
            writer.close()
            await writer.wait_closed()


async def download_memory(config: Config, envelope: dict, path: Path) -> dict:
    """Fetch bounded authenticated pages, writing only a complete verified snapshot."""
    from oraculo.memory.transport import MAX_ARTIFACT_BYTES

    hasher = hashlib.sha256()
    offset = 0
    written = 0
    # Exclusive creation preserves any previous user artifact.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            while True:
                page_request = dict(
                    envelope,
                    request_id=str(uuid.uuid4()),
                    payload=dict(envelope["payload"], offset=offset),
                )
                response = await request(config, page_request)
                if not response.get("ok"):
                    raise ValueError(
                        response.get("error", {}).get("message", "memory_read_failed")
                    )
                page = response["data"]
                content = page["content"]
                if (
                    page["sha256"] != envelope["payload"]["artifact"]
                    or page["offset"] != offset
                    or page["next_offset"] != offset + len(content)
                    or (not content and not page["eof"])
                ):
                    raise ValueError("invalid_memory_read_page")
                raw = content.encode("utf-8")
                written += len(raw)
                if written > MAX_ARTIFACT_BYTES:
                    raise ValueError("memory_artifact_too_large")
                stream.write(raw)
                hasher.update(raw)
                offset = page["next_offset"]
                if page["eof"]:
                    if (
                        offset != page["total_characters"]
                        or hasher.hexdigest() != envelope["payload"]["artifact"]
                    ):
                        raise ValueError("memory_artifact_integrity_error")
                    stream.flush()
                    os.fsync(stream.fileno())
                    return {
                        "ok": True,
                        "file": str(path),
                        "sha256": hasher.hexdigest(),
                        "bytes": written,
                        "job_id": page["job_id"],
                        "project_id": page["project_id"],
                    }
    except BaseException:
        path.unlink()
        raise


def stage_proposal(
    config: Config, path: Path, shared_source: Path | None = None
) -> dict:
    raw = path.read_bytes()
    if shared_source:
        raw = canonical_json(
            {
                "scope": "shared",
                "sanitized_source": shared_source.read_text(),
                "proposal": json.loads(raw),
            }
        ).encode()
    if len(raw) > config.memory.max_proposal_bytes:
        raise ValueError("proposal_too_large")
    json.loads(raw)
    directory = config.state_dir / "staging"
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    if directory.is_symlink():
        raise ValueError("staging_symlink")
    name = f"{uuid.uuid4().hex}.json"
    descriptor = os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    return {"artifact": name, "sha256": hashlib.sha256(raw).hexdigest()}


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="oraculo")
    root.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    commands = root.add_subparsers(dest="command", required=True)
    task = commands.add_parser("task")
    task.add_argument(
        "action",
        choices=[
            "start",
            "resume",
            "status",
            "list",
            "continue",
            "progress",
            "finish",
            "publish",
            "pr-check",
            "merge",
            "settle",
            "unwatch",
        ],
    )
    task.add_argument("--file", type=Path, help="JSON payload; stdin when omitted")
    task.add_argument(
        "--stdio-host",
        action="store_true",
        help="Bridge native T3 calls through interactive JSON request/ack",
    )
    task.add_argument(
        "--transport-command",
        type=Path,
        help="JSON argv for an explicit caller-bound host bridge",
    )
    commands.add_parser("serve")
    commands.add_parser("doctor")
    commands.add_parser("init")
    auth = commands.add_parser("auth")
    auth.add_argument("action", choices=["login"])
    status = commands.add_parser("status")
    status.add_argument("--project", default="*")
    capacity = commands.add_parser("capacity")
    capacity.add_argument("--set", dest="max_executors", type=int, choices=range(1, 5))
    submit = commands.add_parser("submit")
    submit.add_argument("project")
    submit.add_argument("text")
    submit.add_argument("--kind", default="implementation")
    plan = commands.add_parser("plan")
    plan.add_argument("project")
    plan.add_argument("--file", type=Path, required=True)
    plan.add_argument("--key", required=True)
    configure = commands.add_parser("plan-configure")
    configure.add_argument("project")
    configure.add_argument("objective_id")
    configure.add_argument(
        "--max-parallel", type=int, choices=range(1, 5), required=True
    )
    retry = commands.add_parser("retry")
    retry.add_argument("project")
    retry.add_argument("objective_id")
    retry.add_argument("task_id")
    cancel = commands.add_parser("cancel")
    cancel.add_argument("project")
    cancel.add_argument("objective_id")
    raw = commands.add_parser("request")
    raw.add_argument("--file", type=Path)
    settlement = commands.add_parser("settlement")
    settlement.add_argument("action", choices=["bind", "status"])
    settlement.add_argument("project")
    settlement.add_argument("--file", type=Path)
    settlement.add_argument("--binding-id")
    review = commands.add_parser("review")
    review.add_argument("action", choices=["open", "close"])
    review.add_argument("project")
    review.add_argument("--session")
    cycle = commands.add_parser("memory-cycle")
    cycle.add_argument("action", choices=["begin", "read", "finish", "status", "retry"])
    cycle.add_argument("project")
    cycle.add_argument("--thread-id", required=True)
    cycle.add_argument("--run-id", required=True)
    cycle.add_argument("--key")
    cycle.add_argument("--query", default="")
    cycle.add_argument("--cycle-id")
    cycle.add_argument("--status", choices=["succeeded", "failed", "stopped"])
    cycle.add_argument("--file", type=Path)
    cycle.add_argument("--offset", type=int, default=0)
    cycle.add_argument("--artifact")
    cycle.add_argument("--phase", choices=["read", "write"], default="write")
    repair = commands.add_parser("memory-retry")
    repair.add_argument("project")
    repair.add_argument("--cycle-id", required=True)
    repair.add_argument("--file", type=Path)
    memory = commands.add_parser("memory")
    memory.add_argument(
        "action", choices=["ingest", "query", "lint", "propose", "promote", "read"]
    )
    memory.add_argument("project")
    memory.add_argument("--source", type=Path)
    memory.add_argument("--query", default="")
    memory.add_argument("--file", type=Path)
    memory.add_argument("--attempt-id")
    memory.add_argument("--generation", type=int)
    memory.add_argument("--thread-id")
    memory.add_argument("--run-id")
    memory.add_argument("--shared-source", type=Path)
    memory.add_argument("--artifact")
    memory.add_argument("--offset", type=int, default=0)
    return root


def doctor(config: Config) -> dict:
    return {
        "ok": bool(shutil.which("git")) and config.projects_root.is_dir(),
        "python": sys.version.split()[0],
        "git": shutil.which("git"),
        "t3": shutil.which("t3"),
        "socket_exists": config.socket_path.exists(),
        "vault_exists": config.vault_root.exists(),
        "projects_root_exists": config.projects_root.is_dir(),
        "providers": {
            name: {
                "instance_id": provider.instance_id,
                "billing_verified": provider.billing_verified,
                "models": provider.models,
                "eligible_configuration": provider.billing_verified
                and bool(provider.models),
            }
            for name, provider in config.providers.items()
        },
        "additional_spend_allowed": False,
        "note": "Verificação local; não comprova login, cota ou conectividade MCP.",
    }


def make_envelope(kind: str, project: str, payload: dict) -> dict:
    return {
        "schema_version": 1,
        "request_id": str(uuid.uuid4()),
        "kind": kind,
        "project_id": project,
        "origin": {},
        "payload": payload,
    }


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "init":
            from oraculo.memory.publish import Publisher
            from oraculo.state import State

            if not args.config.exists():
                example = resources_root() / "config/oraculo.example.toml"
                args.config.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                with args.config.open("x") as stream:
                    stream.write(example.read_text())
                args.config.chmod(0o600)
            config = Config.load(args.config)
            config.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            state = State(config.db_path)
            state.close()
            Publisher(config.vault_root, config.state_dir).initialize()
            result = {
                "config": str(args.config),
                "state": "initialized",
                "providers_enabled": any(
                    p.billing_verified and p.models for p in config.providers.values()
                ),
            }
        else:
            config = Config.load(args.config)
            if args.command == "task":
                from oraculo.managed_tasks import ManagedTasks
                from oraculo.pr_lifecycle import GitHubPRLifecycle
                from oraculo.publication import GitHubPublisher
                from oraculo.task_store import TaskStore
                from oraculo.task_transport import (
                    CommandTransport,
                    SocketMemory,
                    StdioHostTransport,
                )

                if args.stdio_host and (not args.file or args.transport_command):
                    raise ValueError(
                        "--stdio-host requires --file and excludes --transport-command"
                    )
                payload = (
                    {}
                    if args.action == "list"
                    else json.loads(
                        args.file.read_text() if args.file else sys.stdin.read()
                    )
                )
                command = (
                    json.loads(args.transport_command.read_text())
                    if args.transport_command
                    else None
                )
                store = TaskStore(config.state_dir / "managed-tasks.sqlite3")
                try:
                    if args.action in {"list", "status"}:
                        result = (
                            {
                                "tasks": [
                                    store.describe(row[0])
                                    for row in store.db.execute(
                                        "SELECT id FROM managed_tasks ORDER BY rowid DESC"
                                    )
                                ],
                                "source": "local_ledger",
                            }
                            if args.action == "list"
                            else {
                                **store.describe(payload["task_id"]),
                                "source": "local_ledger",
                            }
                        )
                        print(json.dumps(result, ensure_ascii=False, indent=2))
                        return 0
                    transport = (
                        StdioHostTransport()
                        if args.stdio_host
                        else CommandTransport(command)
                    )
                    adapter = ManagedTasks(
                        config,
                        store,
                        transport,
                        SocketMemory(config),
                        GitHubPublisher(),
                        GitHubPRLifecycle(),
                    )
                    adapter.config_path = args.config.resolve()
                    result = asyncio.run(adapter.command(args.action, payload))
                    print(json.dumps(result, ensure_ascii=False, indent=2))
                    return 1 if result.get("phase") == "blocked" else 0
                finally:
                    store.close()
            if args.command == "serve":
                from oraculo.service import serve

                asyncio.run(serve(config, args.config.resolve()))
                return 0
            if args.command == "auth":
                from oraculo.service import Singleton
                from oraculo.t3.auth import login

                with Singleton(config.state_dir):
                    asyncio.run(login(config.t3_mcp_url))
                result = {"state": "authenticated"}
            elif args.command == "doctor":
                result = doctor(config)
            else:
                if args.command == "request":
                    raw = args.file.read_text() if args.file else sys.stdin.read()
                    envelope = json.loads(raw)
                elif args.command == "status":
                    envelope = make_envelope("status.get", args.project, {})
                elif args.command == "capacity":
                    envelope = make_envelope(
                        "capacity.get"
                        if args.max_executors is None
                        else "capacity.set",
                        "*",
                        (
                            {}
                            if args.max_executors is None
                            else {"max_executors": args.max_executors}
                        ),
                    )
                elif args.command == "settlement":
                    if args.action == "bind":
                        if not args.file:
                            raise ValueError("settlement bind exige --file")
                        payload = json.loads(args.file.read_text())
                    else:
                        payload = (
                            {"binding_id": args.binding_id} if args.binding_id else {}
                        )
                    envelope = make_envelope(
                        f"settlement.{args.action}", args.project, payload
                    )
                elif args.command == "memory-cycle":
                    if args.action == "begin":
                        if not args.key:
                            raise ValueError("memory-cycle begin exige --key")
                        payload = {"key": args.key, "query": args.query}
                    else:
                        if not args.cycle_id:
                            raise ValueError("memory-cycle exige --cycle-id")
                        payload = {"cycle_id": args.cycle_id}
                        if args.action == "read":
                            payload["offset"] = args.offset
                            if args.artifact:
                                payload["artifact"] = args.artifact
                        elif args.action == "finish":
                            if not args.file or not args.status:
                                raise ValueError(
                                    "memory-cycle finish exige --status e --file"
                                )
                            payload.update(
                                status=args.status,
                                evidence=json.loads(args.file.read_text()),
                            )
                        elif args.action == "retry":
                            payload["phase"] = args.phase
                    envelope = make_envelope(
                        f"memory.cycle.{args.action}", args.project, payload
                    )
                    envelope["origin"] = {
                        "thread_id": args.thread_id,
                        "run_id": args.run_id,
                    }
                elif args.command == "memory-retry":
                    payload = {"cycle_id": args.cycle_id}
                    if args.file:
                        payload["evidence"] = json.loads(args.file.read_text())
                    envelope = make_envelope(
                        "memory.cycle.repair", args.project, payload
                    )
                elif args.command == "submit":
                    envelope = make_envelope(
                        "objective.submit",
                        args.project,
                        {"text": args.text, "kind": args.kind},
                    )
                elif args.command == "plan":
                    envelope = make_envelope(
                        "plan.submit",
                        args.project,
                        {
                            "plan": json.loads(args.file.read_text()),
                            "dedupe_key": args.key,
                        },
                    )
                elif args.command == "retry":
                    envelope = make_envelope(
                        "task.retry",
                        args.project,
                        {"objective_id": args.objective_id, "task_id": args.task_id},
                    )
                elif args.command == "plan-configure":
                    envelope = make_envelope(
                        "plan.configure",
                        args.project,
                        {
                            "objective_id": args.objective_id,
                            "max_parallel": args.max_parallel,
                        },
                    )
                elif args.command == "cancel":
                    envelope = make_envelope(
                        "objective.cancel",
                        args.project,
                        {"objective_id": args.objective_id},
                    )
                elif args.command == "review":
                    if args.action == "close" and not args.session:
                        raise ValueError("review close exige --session")
                    envelope = make_envelope(
                        f"review.{args.action}",
                        args.project,
                        {"session_id": args.session},
                    )
                elif args.action == "read":
                    if (
                        not args.artifact
                        or not args.attempt_id
                        or args.generation is None
                        or not args.thread_id
                        or not args.run_id
                    ):
                        raise ValueError(
                            "memory read exige --artifact, --attempt-id, --generation, --thread-id e --run-id"
                        )
                    envelope = make_envelope(
                        "memory.read",
                        args.project,
                        {
                            "artifact": args.artifact,
                            "attempt_id": args.attempt_id,
                            "generation": args.generation,
                            "offset": args.offset,
                        },
                    )
                    envelope["origin"] = {
                        "thread_id": args.thread_id,
                        "run_id": args.run_id,
                    }
                elif args.action == "propose":
                    if (
                        not args.file
                        or not args.attempt_id
                        or args.generation is None
                        or not args.thread_id
                        or not args.run_id
                    ):
                        raise ValueError(
                            "memory propose exige --file, --attempt-id, --generation, --thread-id e --run-id"
                        )
                    payload = stage_proposal(config, args.file, args.shared_source)
                    payload.update(
                        attempt_id=args.attempt_id, generation=args.generation
                    )
                    envelope = make_envelope("memory.propose", args.project, payload)
                    envelope["origin"] = {
                        "thread_id": args.thread_id,
                        "run_id": args.run_id,
                    }
                else:
                    if args.action in {"ingest", "promote"} and not args.source:
                        raise ValueError("memory ingest exige --source")
                    envelope = make_envelope(
                        "objective.submit",
                        args.project,
                        {
                            "text": args.query or f"Memória: {args.action}",
                            "kind": "memory",
                            "memory_operation": "ingest"
                            if args.action == "promote"
                            else args.action,
                            "memory_scope": "shared"
                            if args.action == "promote"
                            else args.project,
                            "source_path": str(args.source.resolve())
                            if args.source
                            else None,
                            "query": args.query,
                        },
                    )
                if (
                    isinstance(envelope, dict)
                    and envelope.get("kind") == "objective.submit"
                ):
                    validate_memory_submission(envelope.get("payload"))
                if args.command == "memory" and args.action == "read" and args.file:
                    if args.offset:
                        raise ValueError("--file exige leitura integral, sem --offset")
                    result = asyncio.run(download_memory(config, envelope, args.file))
                else:
                    result = asyncio.run(request(config, envelope))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok", True) else 1
    except (OSError, ValueError, TimeoutError) as exc:
        print(
            json.dumps(
                {"ok": False, "error": type(exc).__name__, "message": str(exc)},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1
