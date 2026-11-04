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
    MAX_RESPONSE_BYTES,
    canonical_json,
    parse_envelope,
    validate_memory_submission,
)
from oraculo.stdio import print_json

DEFAULT_CONFIG = Path.home() / ".config/oraculo/config.toml"


async def request(config: Config, envelope: dict) -> dict:
    raw = canonical_json(envelope).encode()
    parse_envelope(raw)
    timeout = (
        125
        if envelope["kind"]
        in {"rollout.verify", "operation.report", "outcome.verify", "operation.prepare"}
        else 5
    )
    async with asyncio.timeout(timeout):
        reader, writer = await asyncio.open_unix_connection(
            str(config.socket_path), limit=MAX_RESPONSE_BYTES + 1
        )
        try:
            writer.write(raw + b"\n")
            await writer.drain()
            try:
                result = await reader.readline()
            except ValueError:
                raise ValueError("invalid_response") from None
            if not result or len(result) > MAX_RESPONSE_BYTES:
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


def positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a positive integer") from error
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="oraculo")
    root.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    commands = root.add_subparsers(dest="command", required=True)
    from oraculo.knowledge_cli import add_parser as add_brain_parser

    add_brain_parser(commands)
    task = commands.add_parser("task")
    task.add_argument(
        "action",
        choices=[
            "start",
            "resume",
            "cancel",
            "status",
            "list",
            "history",
            "continue",
            "progress",
            "finish",
            "recover-report",
            "diagnose-review",
            "adjudicate-review",
            "diagnose-delivery",
            "recover-delivery",
            "complete-origin",
            "supersede",
            "recover-report-bootstrap",
            "recover-continuation-bootstrap",
            "approve",
            "publish",
            "pr-check",
            "merge",
            "settle",
            "unwatch",
            "deployment",
            "reconcile-legacy-failure",
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
    from oraculo.host_ack import register_parsers as bridge_parsers

    bridge_parsers(commands)
    obsidian = commands.add_parser("obsidian")
    labels = obsidian.add_subparsers(dest="action", required=True).add_parser(
        "project-labels"
    )
    labels.add_argument("--file", type=Path)
    labels.add_argument("--project", action="append", required=True)
    experience = commands.add_parser("experience")
    views = experience.add_subparsers(dest="action", required=True)
    export = views.add_parser("export")
    export.add_argument("--file", type=Path, required=True)
    export.add_argument("--project", required=True)
    export.add_argument("--output", type=Path, required=True)
    snapshot = views.add_parser("snapshot")
    snapshot.add_argument("--project", required=True)
    panel = views.add_parser("serve")
    panel_scope = panel.add_mutually_exclusive_group(required=True)
    panel_scope.add_argument("--project")
    panel_scope.add_argument("--all-projects", action="store_true")
    panel.add_argument("--port", type=int, default=8765)
    portfolio = views.add_parser("portfolio")
    portfolio.add_argument("--file", type=Path, required=True)
    portfolio.add_argument("--output", type=Path, required=True)
    sync = views.add_parser("sync")
    sync.add_argument("--output", type=Path, required=True)
    sync.add_argument("--project", action="append")
    readable = views.add_parser("status")
    readable.add_argument("--file", type=Path, required=True)
    readable.add_argument("--json", action="store_true")
    project = commands.add_parser(
        "project",
        help="Autorizar um projeto T3 sem editar config.toml nem reiniciar",
    )
    project_actions = project.add_subparsers(dest="project_action", required=True)
    register = project_actions.add_parser(
        "register",
        help="Cadastrar um projeto observado no catálogo T3. Somente adição.",
        description=(
            "Somente adição. Não remove projetos. Observa o catálogo T3 atual e "
            "publica uma autorização estreita em "
            "state_dir/project-authorization.json. Não altera config.toml, o hash "
            "preso do processo, reservas, provedores ou rotinas, e não reinicia o "
            "serviço. Sem o arquivo, allowed_projects permanece como está. plan só "
            "emite a confirmação; apply exige essa confirmação exata e observa o "
            "catálogo de novo."
        ),
    )
    phases = register.add_subparsers(dest="phase", required=True)
    plan = phases.add_parser(
        "plan",
        help="Emite a confirmação. Não publica e não reinicia.",
        description="Emite a confirmação. Não publica e não reinicia.",
    )
    apply = phases.add_parser(
        "apply",
        help="Publica uma autorização confirmada. Não edita config.toml.",
        description="Publica uma autorização confirmada. Não edita config.toml.",
    )
    for phase in (plan, apply):
        phase.add_argument("--id", required=True, help="ID do projeto no catálogo T3")
        phase.add_argument(
            "--path",
            required=True,
            help="Diretório absoluto do projeto, estritamente dentro de projects_root",
        )
    apply.add_argument(
        "--confirmation",
        required=True,
        help="SHA-256 exato retornado por plan para o mesmo ID, caminho e catálogo",
    )
    decisions = commands.add_parser(
        "decisions",
        help="Prontidão local e recuperação financeira auditada; sem API paga",
    )
    decisions.add_argument("action", choices=["status", "prepare", "acknowledge"])
    decisions.add_argument("--project", required=True)
    decisions.add_argument(
        "--file", type=Path, help="JSON do acknowledgement; nunca uma credencial"
    )
    commands.add_parser("serve")
    diagnostics = commands.add_parser("doctor")
    diagnostics.add_argument(
        "--provider", choices=["codex", "claude", "grok"], default="codex"
    )
    diagnostics.add_argument("--skill-root", type=Path)
    diagnostics.add_argument("--home", type=Path)
    diagnostics.add_argument("--root", type=Path)
    diagnostics.add_argument("--bin-dir", type=Path)
    diagnostics.add_argument("--label", default="local.oraculo")
    diagnostics.add_argument("--plist-dir", type=Path)
    from oraculo import runtime, service_control, skill_install

    skill_install.register_parsers(commands)
    runtime.register_parsers(commands)
    service_control.register_parsers(commands)
    commands.add_parser("init")
    auth = commands.add_parser("auth")
    auth.add_argument("action", choices=["login"])
    status = commands.add_parser("status")
    status.add_argument("--project", default="*")
    capacity = commands.add_parser("capacity")
    capacity.add_argument("--set", dest="max_executors", type=positive_int)
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
    configure.add_argument("--max-parallel", type=positive_int, required=True)
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


def doctor_report(args) -> dict:
    from oraculo import runtime, service_control, skill_install

    report = {
        "schema_version": 1,
        "source": "local_diagnostics",
        "python": sys.version.split()[0],
        "launcher": shutil.which("oraculo"),
        "config": {"path": str(args.config), "exists": args.config.is_file()},
        "bridge": {
            "acp_environment_present": bool(os.environ.get("T3_ACP_MCP_NODE")),
            "caller_bound": "not_observed",
            "stdio": "cooperative",
        },
        "skill": skill_install.diagnose(
            provider=args.provider, root=args.skill_root, home=args.home
        ),
        "service": {"state": "not_observed"},
    }
    try:
        config = Config.load(args.config)
        report["local"] = doctor(config)
        try:
            report["service"] = asyncio.run(
                request(config, make_envelope("status.get", "*", {}))
            )
        except (OSError, TimeoutError, ValueError) as exc:
            report["service"] = {
                "state": "unavailable",
                "error_code": type(exc).__name__,
            }
        report["ok"] = report["local"]["ok"]
    except (OSError, ValueError) as exc:
        report["config"]["error_code"] = type(exc).__name__
        report["ok"] = False
    report["runtime"] = runtime.diagnose(
        root=args.root or runtime.DEFAULT_ROOT,
        bin_dir=args.bin_dir or runtime.DEFAULT_BIN,
        config=args.config,
    )
    report["launchd"] = service_control.diagnose(
        args.config, root=args.root, label=args.label, plist_dir=args.plist_dir
    )
    report["ok"] = bool(
        report.get("ok")
        and report["runtime"]["state"] == "installed"
        and report["service"].get("ok")
        and report["service"].get("data", {}).get("health") == "online"
        and report["launchd"]["state"]
        not in {"unknown", "failed", "starting", "stopping"}
    )
    report["note"] = (
        "Each probe identifies its own target. ok covers local/runtime/service health; "
        "skill loading and caller-bound bridge require separate observed evidence."
    )
    report["state"] = "diagnosed"
    return report


def make_envelope(kind: str, project: str, payload: dict) -> dict:
    return {
        "schema_version": 1,
        "request_id": str(uuid.uuid4()),
        "kind": kind,
        "project_id": project,
        "origin": {},
        "payload": payload,
    }


async def experience_snapshot(config: Config, project: str) -> dict:
    """Download a scoped, hash-checked snapshot without opening live storage."""
    from oraculo.memory.transport import MAX_ARTIFACT_BYTES

    result = await request(config, make_envelope("experience.snapshot", project, {}))
    if not result.get("ok"):
        raise ValueError(
            "experience_snapshot_service_failed:"
            + str((result.get("error") or {}).get("message"))
        )
    data = result["data"]
    reference = data.get("artifact")
    if reference is None:
        return data
    if reference.get("bytes", MAX_ARTIFACT_BYTES + 1) > MAX_ARTIFACT_BYTES:
        raise ValueError("experience_snapshot_too_large")
    chunks, offset = [], 0
    for _ in range(MAX_ARTIFACT_BYTES // 4096 + 1):
        result = await request(
            config,
            make_envelope(
                "experience.snapshot",
                project,
                {"artifact": reference["artifact"], "offset": offset},
            ),
        )
        if not result.get("ok"):
            raise ValueError("experience_snapshot_download_failed")
        page = result["data"]
        if (
            page.get("project_id") != project
            or page.get("job_id") != "experience"
            or page.get("artifact") != reference["artifact"]
            or page.get("sha256") != reference["sha256"]
            or page.get("offset") != offset
            or page.get("next_offset") != offset + len(page["content"])
        ):
            raise ValueError("experience_snapshot_chunk_mismatch")
        chunks.append(page["content"])
        offset = page["next_offset"]
        if page["eof"]:
            raw = "".join(chunks).encode()
            if (
                len(raw) != reference["bytes"]
                or hashlib.sha256(raw).hexdigest() != reference["sha256"]
            ):
                raise ValueError("experience_snapshot_integrity")
            wrapper = json.loads(raw)
            if (
                wrapper.get("project_id") != project
                or wrapper.get("job_id") != "experience"
            ):
                raise ValueError("experience_snapshot_scope")
            return wrapper["context"]["bundle"]
        if not page["content"]:
            break
    raise ValueError("experience_snapshot_incomplete")


_PORTFOLIO_KEYS = {"schema_version", "snapshots", "project_titles"}
_CATALOG_DENIAL = (
    "experience_snapshot_service_failed:project_not_registered_or_outside_scope"
)


def validate_sync_projects(projects: list[str] | None) -> list[str]:
    selected = list(projects or [])
    if any(type(item) is not str or item == "" for item in selected):
        raise ValueError("experience_sync_invalid_project")
    if len(selected) != len(set(selected)):
        raise ValueError("experience_sync_duplicate_project")
    return selected


def snapshot_project_id(snapshot: dict) -> str:
    """Identity of one service snapshot. Rejects a multi-project blob."""
    if not isinstance(snapshot, dict) or snapshot.get("schema_version") != 1:
        raise ValueError("experience_portfolio_invalid_artifact")
    projects = snapshot.get("projects")
    if (
        not isinstance(projects, list)
        or len(projects) != 1
        or not isinstance(projects[0], dict)
    ):
        raise ValueError("experience_portfolio_invalid_artifact")
    project_id = projects[0].get("id")
    declared = snapshot.get("project_id")
    if (
        not isinstance(project_id, str)
        or project_id == ""
        or (declared is not None and declared != project_id)
    ):
        raise ValueError("experience_portfolio_invalid_artifact")
    return project_id


def reject_unsafe_export_paths(files: dict) -> None:
    from pathlib import PurePosixPath

    for path in files:
        pure = PurePosixPath(path) if isinstance(path, str) else None
        if (
            pure is None
            or path == ""
            or pure.is_absolute()
            or ".." in pure.parts
            or "\\" in path
            or str(pure) != path
        ):
            raise ValueError("experience_portfolio_unsafe_path")


def load_portfolio_bundle(path: Path) -> tuple[list[dict], dict[str, str] | None]:
    try:
        bundle = json.loads(path.read_text())
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("experience_portfolio_invalid_artifact") from exc
    if (
        not isinstance(bundle, dict)
        or bundle.get("schema_version") != 1
        or not set(bundle).issubset(_PORTFOLIO_KEYS)
        or "snapshots" not in bundle
    ):
        raise ValueError("experience_portfolio_invalid_artifact")
    snapshots = bundle["snapshots"]
    if not isinstance(snapshots, list):
        raise ValueError("experience_portfolio_invalid_artifact")
    if not snapshots:
        raise ValueError("experience_portfolio_no_projects")
    identities = [snapshot_project_id(snapshot) for snapshot in snapshots]
    if len(identities) != len(set(identities)):
        raise ValueError("experience_portfolio_duplicate_project")
    titles = bundle["project_titles"] if "project_titles" in bundle else None
    if titles is not None:
        if not isinstance(titles, dict) or any(
            not isinstance(key, str) or not isinstance(value, str) or value == ""
            for key, value in titles.items()
        ):
            raise ValueError("experience_portfolio_invalid_artifact")
        if not set(titles).issubset(identities):
            raise ValueError("experience_portfolio_unknown_title")
    return snapshots, titles


def catalog_rows(status: dict) -> tuple[list[dict], dict]:
    """Registered catalog from status.get. Paths stay out of the reading receipt."""
    if not isinstance(status, dict) or status.get("ok") is not True:
        message = ""
        error = status.get("error") if isinstance(status, dict) else None
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            message = error["message"]
        raise ValueError("experience_sync_service_failed:" + message)
    data = status.get("data")
    projects = data.get("projects") if isinstance(data, dict) else None
    if not isinstance(projects, list):
        raise ValueError("experience_sync_invalid_catalog")
    rows = []
    seen: set[str] = set()
    for row in projects:
        if not isinstance(row, dict):
            raise ValueError("experience_sync_invalid_catalog")
        project_id = row.get("id")
        state = row.get("status")
        if (
            not isinstance(project_id, str)
            or project_id == ""
            or not isinstance(state, str)
            or project_id in seen
        ):
            raise ValueError("experience_sync_invalid_catalog")
        seen.add(project_id)
        rows.append(row)
    return rows, data


def catalog_titles(rows: list[dict]) -> dict[str, str] | None:
    """Human titles only when the catalog itself carries a non-empty string."""
    titles = {}
    for row in rows:
        title = row.get("title")
        if isinstance(title, str) and title != "":
            titles[row["id"]] = title
    return titles or None


def portfolio_freshness(snapshots: list[dict], source: str) -> list[dict]:
    items = []
    for snapshot in snapshots:
        observed = snapshot.get("observed_at")
        items.append(
            {
                "project_id": snapshot_project_id(snapshot),
                "observed_at": observed if isinstance(observed, str) else None,
                "source": source,
            }
        )
    return items


def write_portfolio(
    destination: Path,
    snapshots: list[dict],
    titles: dict[str, str] | None,
    *,
    mode: str,
) -> dict:
    try:
        from oraculo.experience_portfolio import (
            portfolio_export_files,
            portfolio_model,
        )
    except ImportError as exc:
        raise ValueError("experience_portfolio_unavailable") from exc
    from oraculo.experience_portfolio_refresh import refresh_export

    model = portfolio_model(snapshots, titles)
    if (
        not isinstance(model, dict)
        or model.get("schema_version") != 1
        or model.get("kind") != "portfolio"
    ):
        raise ValueError("experience_portfolio_invalid_model")
    files = portfolio_export_files(model)
    if not isinstance(files, dict) or any(
        not isinstance(content, str) for content in files.values()
    ):
        raise ValueError("experience_portfolio_invalid_files")
    reject_unsafe_export_paths(files)
    result = refresh_export(destination, files)
    observed = model.get("observed_at")
    result["mode"] = mode
    result["observed_at"] = observed if isinstance(observed, str) else None
    source = "file" if mode == "snapshot" else "service"
    result["freshness"] = portfolio_freshness(snapshots, source)
    result["projects"] = [item["project_id"] for item in result["freshness"]]
    return result


async def experience_sync(
    config: Config, destination: Path, projects: list[str]
) -> dict:
    """Refresh every selected authorized project, or write nothing."""
    status = await request(config, make_envelope("status.get", "*", {}))
    rows, data = catalog_rows(status)
    by_id = {row["id"]: row for row in rows}
    explicit = bool(projects)
    if explicit:
        missing = [item for item in projects if item not in by_id]
        if missing:
            raise ValueError("experience_sync_unknown_project:" + ",".join(missing))
        unregistered = [
            item for item in projects if by_id[item]["status"] != "registered"
        ]
        if unregistered:
            raise ValueError(
                "experience_sync_project_not_registered:" + ",".join(unregistered)
            )
        selected = [by_id[item] for item in sorted(projects)]
    else:
        selected = sorted(
            (row for row in rows if row["status"] == "registered"),
            key=lambda row: row["id"],
        )
    snapshots: list[dict] = []
    included: list[dict] = []
    excluded: list[str] = []
    failures: list[str] = []
    for row in selected:
        project_id = row["id"]
        try:
            snapshot = await experience_snapshot(config, project_id)
        except ValueError as exc:
            message = str(exc)
            if message == _CATALOG_DENIAL:
                if explicit:
                    failures.append(project_id + ":not_authorized")
                else:
                    excluded.append(project_id)
                continue
            failures.append(project_id + ":" + message)
            continue
        try:
            identity = snapshot_project_id(snapshot)
        except ValueError:
            failures.append(project_id + ":experience_sync_scope")
            continue
        if identity != project_id:
            failures.append(project_id + ":experience_sync_scope")
            continue
        snapshots.append(snapshot)
        included.append(row)
    if failures:
        raise ValueError("experience_sync_incomplete:" + ",".join(failures))
    if not snapshots:
        suffix = ":" + ",".join(excluded) if excluded else ""
        raise ValueError("experience_sync_no_authorized_projects" + suffix)
    result = write_portfolio(
        destination, snapshots, catalog_titles(included), mode="refresh"
    )
    result["excluded_unauthorized"] = excluded
    runtime = data.get("runtime") if isinstance(data.get("runtime"), dict) else {}
    protocol = runtime.get("protocol_version")
    health = data.get("health")
    result["service"] = {
        "health": health if isinstance(health, str) else None,
        "protocol_version": protocol if type(protocol) is int else None,
    }
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "brain":
            from oraculo.knowledge_cli import command as brain_command

            print_json(asyncio.run(brain_command(Config.load(args.config), args)))
            return 0
        if args.command == "obsidian":
            from oraculo.obsidian import project_labels_css

            print(
                project_labels_css(
                    json.loads(
                        args.file.read_text() if args.file else sys.stdin.read()
                    ),
                    args.project,
                ),
                end="",
            )
            return 0
        if args.command == "experience":
            if args.action == "portfolio":
                snapshots, titles = load_portfolio_bundle(args.file)
                print_json(
                    write_portfolio(args.output, snapshots, titles, mode="snapshot")
                )
                return 0
            if args.action == "sync":
                selected = validate_sync_projects(args.project)
                print_json(
                    asyncio.run(
                        experience_sync(Config.load(args.config), args.output, selected)
                    )
                )
                return 0
            from oraculo.experience import export_files, project_model, write_export
            from oraculo.presentation import summary_markdown, task_view

            if args.action == "serve":
                from oraculo.task_panel import serve

                if args.all_projects:
                    serve(Config.load(args.config), None, args.port, all_projects=True)
                else:
                    serve(Config.load(args.config), args.project, args.port)
            elif args.action == "snapshot":
                print_json(
                    asyncio.run(
                        experience_snapshot(Config.load(args.config), args.project)
                    )
                )
            elif args.action == "export":
                model = project_model(json.loads(args.file.read_text()), args.project)
                print_json(write_export(args.output, export_files(model)))
            else:
                status = json.loads(args.file.read_text())
                if "ok" in status:
                    if status["ok"] is not True:
                        raise ValueError("experience_failed_status")
                    status = status["data"]
                if args.json:
                    print_json(status)
                else:
                    print(summary_markdown(task_view(status)))
            return 0
        if args.command == "bridge":
            from oraculo.host_ack import run_ack

            run_ack(args)
            return 0
        if args.command in {"runtime", "service"}:
            from oraculo import runtime, service_control

            result = (
                runtime if args.command == "runtime" else service_control
            ).dispatch(args)
            print_json(result)
            return (
                1
                if result.get("state")
                in {"blocked", "failed", "unknown", "recovery_required", "unsupported"}
                else 0
            )
        if args.command == "skill":
            from oraculo import skill_install

            result = skill_install.dispatch(args)
            print_json(result)
            return 1 if result.get("state") in {"blocked", "failed"} else 0
        if args.command == "doctor":
            result = doctor_report(args)
            print_json(result)
            return 0 if result["ok"] else 1
        if args.command == "init":
            import fcntl
            import os

            from oraculo.memory.publish import Publisher
            from oraculo.service import Singleton
            from oraculo.state import State

            if not args.config.exists():
                example = resources_root() / "config/oraculo.example.toml"
                args.config.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                with args.config.open("x") as stream:
                    stream.write(example.read_text())
                args.config.chmod(0o600)
            config = Config.load(args.config)
            config.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            # Initialization must never open/migrate an existing database. Take
            # both writer locks before checking, including for the first start.
            with Singleton(config.state_dir):
                fd = os.open(
                    config.state_dir / "managed-tasks.lock",
                    os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                    0o600,
                )
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    databases = (
                        config.db_path,
                        config.state_dir / "managed-tasks.sqlite3",
                    )
                    existing = [config.socket_path] + [
                        Path(str(database) + suffix)
                        for database in databases
                        for suffix in ("", "-wal", "-shm", "-journal")
                    ]
                    if any(os.path.lexists(path) for path in existing):
                        raise ValueError(
                            "init_requires_new_state_use_runtime_migration"
                        )
                    state = State(config.db_path, max_executors=config.max_executors)
                    state.close()
                    Publisher(config.vault_root, config.state_dir).initialize()
                finally:
                    os.close(fd)
            result = {
                "config": str(args.config),
                "state": "initialized",
                "providers_enabled": any(
                    p.billing_verified and p.models for p in config.providers.values()
                ),
            }
        else:
            config = Config.load(args.config)
            if args.command == "project":
                from oraculo.project_registration import cli_command

                print_json(asyncio.run(cli_command(config, args)))
                return 0
            if args.command == "task":
                from oraculo.managed_service import command as managed_command
                from oraculo.state import StateError
                from oraculo.task_store import TaskStore
                from oraculo.task_transport import CommandTransport, StdioHostTransport

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
                if args.action in {"list", "status", "history"}:
                    try:
                        result = asyncio.run(
                            managed_command(config, args.action, payload, None)
                        )
                    except (OSError, TimeoutError):
                        store = TaskStore(
                            config.state_dir / "managed-tasks.sqlite3", read_only=True
                        )
                        try:
                            result = (
                                {
                                    "tasks": [
                                        store.describe(row[0])
                                        for row in store.db.execute(
                                            "SELECT id FROM managed_tasks ORDER BY rowid DESC"
                                        )
                                    ]
                                }
                                if args.action == "list"
                                else store.describe(payload["task_id"])
                            )
                            if args.action == "history":
                                result = store.history(
                                    payload["task_id"], payload.get("offset", 0)
                                )
                            result["source"] = "local_ledger"
                        finally:
                            store.close()
                    print_json(result)
                    return 0
                if args.action in {
                    "reconcile-legacy-failure",
                    "recover-report-bootstrap",
                    "recover-continuation-bootstrap",
                }:
                    from oraculo.legacy_failure import reconcile

                    transport = (
                        StdioHostTransport()
                        if args.stdio_host
                        else CommandTransport(
                            json.loads(args.transport_command.read_text())
                            if args.transport_command
                            else None
                        )
                    )
                    try:
                        if args.action in {
                            "recover-report-bootstrap",
                            "recover-continuation-bootstrap",
                        }:
                            if args.action == "recover-report-bootstrap":
                                from oraculo.report_bootstrap import recover
                            else:
                                from oraculo.continuation_bootstrap import recover

                            result = asyncio.run(
                                recover(config, args.config, payload, transport)
                            )
                        else:
                            result = asyncio.run(reconcile(config, payload, transport))
                        print_json(result)
                        # The reconciliation succeeded; the reviewed work failed.
                        return (
                            0
                            if args.action != "reconcile-legacy-failure"
                            and result.get("work_status") == "succeeded"
                            else 1
                        )
                    finally:
                        if args.stdio_host:
                            transport.close()
                # Never mutate the ledger locally after negotiating service authority.
                try:
                    capabilities = asyncio.run(
                        request(config, make_envelope("managed.capabilities", "*", {}))
                    )
                except (OSError, TimeoutError):
                    if args.action != "cancel":
                        raise
                    # No daemon answers. Cancelling an unlaunched reservation is the
                    # one mutation allowed offline, under the daemon and ledger locks.
                    from oraculo.managed_offline import cancel as offline_cancel

                    transport = (
                        StdioHostTransport()
                        if args.stdio_host
                        else CommandTransport(
                            json.loads(args.transport_command.read_text())
                            if args.transport_command
                            else None
                        )
                    )
                    try:
                        result = asyncio.run(offline_cancel(config, payload, transport))
                        print_json(result)
                        return 1 if result.get("phase") == "blocked" else 0
                    finally:
                        if args.stdio_host:
                            transport.close()
                if (
                    not capabilities.get("ok")
                    or capabilities.get("data", {}).get("protocol_version") != 1
                ):
                    raise StateError("managed_service_upgrade_required")
                if args.action == "recover-report" and not capabilities.get(
                    "data", {}
                ).get("invalid_report_recovery"):
                    raise StateError("report_recovery_service_upgrade_required")
                if (
                    args.action in {"diagnose-delivery", "recover-delivery"}
                    and capabilities.get("data", {}).get("legacy_delivery_recovery")
                    is not True
                ):
                    raise StateError("legacy_delivery_service_upgrade_required")
                if (
                    args.action in {"diagnose-review", "adjudicate-review"}
                    and capabilities.get("data", {}).get("terminal_review_adjudication")
                    is not True
                ):
                    raise StateError("review_adjudication_service_upgrade_required")
                if (
                    args.action == "start"
                    and payload.get("task_role")
                    in {"quality_review", "functional_tests"}
                    and capabilities.get("data", {}).get("delivery_review_separation")
                    is not True
                ):
                    raise StateError("delivery_review_service_upgrade_required")
                if (
                    args.action in {"pr-check", "merge", "settle"}
                    and capabilities.get("data", {}).get("ci_round_memory") is not True
                ):
                    raise StateError("ci_round_memory_service_upgrade_required")
                if (
                    args.action in {"complete-origin", "supersede"}
                    and capabilities.get("data", {}).get("origin_terminal_recovery")
                    is not True
                ):
                    raise StateError("origin_recovery_service_upgrade_required")
                command = (
                    json.loads(args.transport_command.read_text())
                    if args.transport_command
                    else None
                )
                transport = (
                    StdioHostTransport()
                    if args.stdio_host
                    else CommandTransport(command)
                )
                try:
                    result = asyncio.run(
                        managed_command(config, args.action, payload, transport)
                    )
                    print_json(result)
                    return 1 if result.get("phase") == "blocked" else 0
                finally:
                    if args.stdio_host:
                        transport.close()
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
                elif args.command == "decisions":
                    if args.action != "acknowledge" and args.file:
                        raise ValueError("decisions_payload_not_allowed")
                    if args.action == "acknowledge" and args.file is None:
                        raise ValueError("decision_acknowledgement_file_required")
                    payload = json.loads(args.file.read_text()) if args.file else {}
                    envelope = make_envelope(
                        "decisions." + args.action, args.project, payload
                    )
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
        print_json(result)
        return 0 if result.get("ok", True) else 1
    except (OSError, ValueError, TimeoutError) as exc:
        print(
            json.dumps(
                {"ok": False, "error": type(exc).__name__, "message": str(exc)},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return (
            2
            if args.command in {"skill", "runtime", "service"}
            and isinstance(exc, ValueError)
            else 1
        )
