"""Verified user releases and recoverable activation, independent of the checkout."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path

DEFAULT_ROOT = Path.home() / ".local/share/oraculo"
DEFAULT_BIN = Path.home() / ".local/bin"
CURRENT_STATE_SCHEMA = 4


class RuntimeError(Exception):
    def __init__(
        self,
        code: str,
        recovery: str = "Inspect runtime status and reconcile the operation before retrying.",
    ):
        self.code, self.recovery = code, recovery
        super().__init__(code)


def receipt(state: str, *, operation_id=None, error_code=None, **data) -> dict:
    return dict(
        schema_version=1,
        state=state,
        error_code=error_code,
        retryable=False,
        recovery_action=None,
        operation_id=operation_id,
        source="local_runtime",
        observed_at=datetime.now(UTC).isoformat(),
        **data,
    )


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def atomic_write(path: Path, data: bytes, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="." + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as out:
            os.fchmod(out.fileno(), mode)
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, path)
        sync_dir(path.parent)
    finally:
        Path(name).unlink(missing_ok=True)


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_json(path, data):
    atomic_write(path, (json.dumps(data, sort_keys=True, indent=2) + "\n").encode())


@contextlib.contextmanager
def runtime_lock(root):
    if root.is_symlink():
        raise RuntimeError("runtime_root_conflict")
    root.mkdir(parents=True, exist_ok=True)
    fd = os.open(root / ".runtime.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "a") as lock:
        info = os.fstat(lock.fileno())
        if (
            info.st_uid != os.getuid()
            or not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
        ):
            raise RuntimeError("runtime_lock_conflict")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("runtime_busy") from None
        yield


def valid_id(value):
    if not isinstance(value, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value
    ):
        raise ValueError("invalid_release_id")
    return value


def inventory(root: Path, *, symlinks=False):
    files = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if relative == "manifest.json" and not path.is_symlink():
            continue
        if path.is_symlink():
            if not symlinks:
                raise RuntimeError("artifact_symlink")
            files[relative] = "symlink:" + os.readlink(path)
        elif path.is_file():
            files[relative] = digest(path)
    return files


def verify(root: Path, *, installed=False):
    if root.is_symlink() or not root.is_dir():
        raise RuntimeError("release_missing")
    try:
        manifest = json.loads((root / "manifest.json").read_text())
        valid_id(manifest["release_id"])
        if manifest["schema_version"] != 1 or not re.fullmatch(
            r"[a-f0-9]{40,64}", manifest["source_commit"]
        ):
            raise ValueError("manifest_schema")
        if manifest["python"] != "3.12":
            raise RuntimeError("python_incompatible")
        if not (
            type(manifest["schema_min"]) is int
            and type(manifest["schema_max"]) is int
            and 0 <= manifest["schema_min"] <= manifest["schema_max"]
        ):
            raise ValueError("manifest_schema")
        if manifest["files"] != inventory(root, symlinks=installed):
            raise RuntimeError("release_corrupt")
        if installed:
            if digest(root / "venv/bin/python") != manifest["interpreter_sha256"]:
                raise RuntimeError("interpreter_changed")
        elif "requirements.txt" not in manifest["files"] or not any(
            n.startswith("oraculo-") and n.endswith(".whl") for n in manifest["files"]
        ):
            raise ValueError("bundle_requires_wheel_and_requirements")
        return manifest
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("invalid_manifest") from exc


def create_manifest(bundle, release_id, source_commit, schema_min, schema_max):
    """Seal a reviewed, flat wheelhouse plus hash-locked requirements."""
    manifest = dict(
        schema_version=1,
        release_id=valid_id(release_id),
        source_commit=source_commit,
        python="3.12",
        schema_min=schema_min,
        schema_max=schema_max,
        files=inventory(bundle),
    )
    if any(
        "/" in name or not (name.endswith(".whl") or name == "requirements.txt")
        for name in manifest["files"]
    ):
        raise ValueError("bundle_contains_unexpected_files")
    write_json(bundle / "manifest.json", manifest)
    verify(bundle)
    return manifest


def launcher_bytes(root):
    return (
        "#!/bin/sh\n# oraculo managed launcher v1\n"
        "export PYTHONDONTWRITEBYTECODE=1\n"
        f'exec {shlex.quote(str(root / "current/venv/bin/python"))} -B -m oraculo "$@"\n'
    ).encode()


def check_launcher(root, bin_dir):
    launcher = bin_dir / "oraculo"
    if launcher.is_symlink() or (
        launcher.exists() and launcher.read_bytes() != launcher_bytes(root)
    ):
        raise RuntimeError(
            "launcher_conflict",
            "Preserve the existing executable; select another --bin-dir.",
        )


def current_release(root):
    current = root / "current"
    if not current.exists() and not current.is_symlink():
        return None
    if not current.is_symlink():
        raise RuntimeError("current_conflict")
    target = current.resolve()
    if target.parent != (root / "releases").resolve() or not target.is_dir():
        raise RuntimeError("current_conflict")
    verify(target, installed=True)
    return target.name


def switch(root, release):
    temporary = root / (".current-" + uuid.uuid4().hex)
    temporary.symlink_to(Path("releases") / valid_id(release))
    try:
        os.replace(temporary, root / "current")
        sync_dir(root)
    finally:
        temporary.unlink(missing_ok=True)


def run(command, *, timeout=180):
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1", UV_PYTHON_DOWNLOADS="never"),
    )
    if result.returncode:
        raise RuntimeError(
            "dependency_install_failed",
            "Check the verified wheelhouse, locked hashes and Python 3.12 availability.",
        )
    return result.stdout


def prepare(root, bin_dir, bundle, *, dry_run=False, initial=False):
    manifest = verify(bundle)
    if not manifest["schema_min"] <= CURRENT_STATE_SCHEMA <= manifest["schema_max"]:
        raise RuntimeError("schema_incompatible")
    if sys.version_info[:2] != (3, 12):
        raise RuntimeError(
            "python_incompatible",
            "Run the bootstrap with an existing Python 3.12 interpreter.",
        )
    check_launcher(root, bin_dir)
    previous = current_release(root)
    release = root / "releases" / manifest["release_id"]
    if release.exists():
        existing = verify(release, installed=True)
        if existing.get("bundle_sha256") != digest(bundle / "manifest.json"):
            raise RuntimeError("release_id_conflict")
    if dry_run:
        return receipt(
            "planned",
            release_id=manifest["release_id"],
            previous=previous,
            root=str(root),
            launcher=str(bin_dir / "oraculo"),
            starts_service=False,
        )
    if not release.exists():
        uv = shutil.which("uv")
        if not uv:
            raise RuntimeError(
                "uv_missing",
                "Install uv explicitly before bootstrap; no Python is downloaded automatically.",
            )
        release.parent.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix=".prepare-", dir=release.parent))
        try:
            run([uv, "venv", "--python", sys.executable, str(stage / "venv")])
            run(
                [
                    uv,
                    "--offline",
                    "pip",
                    "install",
                    "--python",
                    str(stage / "venv/bin/python"),
                    "--link-mode",
                    "copy",
                    "--no-index",
                    "--find-links",
                    str(bundle),
                    "--require-hashes",
                    "--only-binary",
                    ":all:",
                    "-r",
                    str(bundle / "requirements.txt"),
                ]
            )
            run(
                [
                    str(stage / "venv/bin/python"),
                    "-B",
                    "-c",
                    "import oraculo.cli; from oraculo.config import resources_root; "
                    'assert all((resources_root()/p).is_file() for p in ["config/oraculo.example.toml", "prompts/managed-origin.md", "skills/oraculo/SKILL.md", "templates/integration/AGENTS.md"])',
                ]
            )
            installed = dict(
                manifest,
                bundle_sha256=digest(bundle / "manifest.json"),
                interpreter_sha256=digest(stage / "venv/bin/python"),
                files=inventory(stage, symlinks=True),
            )
            write_json(stage / "manifest.json", installed)
            os.replace(stage, release)
            sync_dir(release.parent)
            for path in release.rglob("*"):
                if not path.is_symlink():
                    path.chmod(path.stat().st_mode & ~0o222)
            release.chmod(0o555)
        finally:
            if stage.exists():
                shutil.rmtree(stage)
    atomic_write(bin_dir / "oraculo", launcher_bytes(root), 0o755)
    return receipt(
        "prepared",
        release_id=manifest["release_id"],
        previous=previous,
        starts_service=False,
        activation_required=True,
    )


def compatible(manifest, guard):
    if not (
        guard.get("quiescent") is True
        and guard.get("admission_closed") is True
        and isinstance(guard.get("state_schema"), int)
        and guard.get("config_schema") == 1
    ):
        raise RuntimeError("maintenance_unproven")
    if not manifest["schema_min"] <= guard["state_schema"] <= manifest["schema_max"]:
        raise RuntimeError(
            "schema_incompatible",
            "Keep the current release; use code compatible with the current state schema. Never restore an old database.",
        )


def config_hash(config_path):
    return digest(config_path) if config_path.is_file() else None


def load_config_verified(config_path, expected_hash):
    from oraculo.config import Config

    config = Config.load(config_path)
    if (
        config._loaded_sha256 != expected_hash
        or config_hash(config_path) != expected_hash
    ):
        raise RuntimeError("config_changed")
    return config


def match_guard(journal, guard):
    """Never replace the durable token while recovering an operation."""
    if guard.get("operation_id") != journal["operation_id"]:
        raise RuntimeError("maintenance_correlation")
    if not guard.get("token") or (
        journal.get("token") is not None and journal["token"] != guard["token"]
    ):
        raise RuntimeError("maintenance_token_mismatch")


def recovery_stop(root, config_path, controller, *, journal_name="operation.json"):
    from oraculo.maintenance import maintenance_guard

    if journal_name not in {"operation.json", "service-operation.json"}:
        raise ValueError("invalid_recovery_journal")
    path = root / journal_name
    if not path.is_file() or path.is_symlink():
        raise RuntimeError("recovery_journal_missing")
    journal = json.loads(path.read_text())
    config = load_config_verified(config_path, journal.get("config_sha256"))
    result = controller.recovery_stop(journal, journal_name=journal_name)
    with maintenance_guard(config, journal["operation_id"]) as guard:
        if config_hash(config_path) != config._loaded_sha256:
            raise RuntimeError("config_changed")
        match_guard(journal, guard)
        compatible(
            verify(
                root / "releases" / journal["replacement"]["release_id"], installed=True
            ),
            guard,
        )
        journal["state"] = "recovery_stopped"
        write_json(path, journal)
    return dict(result, operation_id=journal["operation_id"], recovery_required=True)


def require_migration_complete(root):
    path = root / "legacy-migration.json"
    if path.exists() and json.loads(path.read_text()).get("state") != "complete":
        raise RuntimeError(
            "legacy_migration_pending",
            "Repeat runtime migrate-legacy with the recorded inputs; do not restore a database or start legacy code.",
        )


def activate(
    root,
    bin_dir,
    config_path,
    controller,
    release_id=None,
    *,
    rollback=False,
    dry_run=False,
):
    require_migration_complete(root)
    journal_path = root / "operation.json"
    journal = json.loads(journal_path.read_text()) if journal_path.exists() else None
    pending = journal and journal["state"] != "complete"
    if pending and not rollback and release_id != journal.get("target"):
        raise RuntimeError(
            "recovery_required",
            "Repeat runtime activate for the recorded target or use runtime rollback; inspect operation.json first.",
        )
    previous = current_release(root)
    if rollback:
        history = journal or {}
        release_id = history.get("previous")
        if not release_id:
            raise RuntimeError("no_previous_release")
    valid_id(release_id)
    manifest = verify(root / "releases" / release_id, installed=True)
    check_launcher(root, bin_dir)
    expected_hash = config_hash(config_path)
    if pending and expected_hash != journal["config_sha256"]:
        raise RuntimeError(
            "config_changed",
            "Restore or reconcile the recorded configuration before recovery.",
        )
    observed = controller.status()
    if (
        pending
        and not dry_run
        and observed["state"] in {"starting", "loaded", "failed"}
    ):
        recovery_stop(root, config_path, controller)
        journal = json.loads(journal_path.read_text())
        observed = controller.status()
    if (
        pending
        and not dry_run
        and observed["state"] == "unloaded"
        and journal.get("recovery_pids")
    ):
        # A prior recovery may have advanced the pointer before a later file fault.
        # There is no loaded job to identify/stop again; retain the PID/socket proof.
        controller.wait(
            lambda: (
                controller.inspect_launchd()["loaded"] is False
                and not any(controller.alive(pid) for pid in journal["recovery_pids"])
                and not controller.socket_present()
            ),
            "stop_timeout",
        )
    if observed["state"] in {"unknown", "failed", "stopping", "starting", "loaded"}:
        raise RuntimeError("service_state_unproven")
    running = observed["state"] == "online"
    if dry_run:
        return receipt(
            "planned",
            release_id=release_id,
            previous=previous,
            service=observed,
            maintenance_required=True,
        )
    service_path = root / "service-operation.json"
    service_operation = (
        json.loads(service_path.read_text()) if service_path.exists() else None
    )
    if service_operation and service_operation.get("state") == "complete":
        service_operation = None
    if (
        service_operation
        and service_operation.get("config_sha256", expected_hash) != expected_hash
    ):
        raise RuntimeError("config_changed")
    if (
        pending
        and service_operation
        and service_operation["operation_id"] != journal["operation_id"]
    ):
        raise RuntimeError("maintenance_correlation")
    operation = (
        journal["operation_id"]
        if pending
        else (
            service_operation["operation_id"]
            if service_operation
            else str(uuid.uuid4())
        )
    )
    config = load_config_verified(config_path, expected_hash) if expected_hash else None
    fresh = (
        (previous is None or (pending and journal.get("previous") is None))
        and config is None
        and observed["state"] in {"unloaded", "unsupported"}
    )
    if not fresh and config is None:
        raise RuntimeError("config_missing")
    if not pending:
        journal = dict(
            schema_version=1,
            operation_id=operation,
            state="acquiring_maintenance",
            previous=previous,
            target=release_id,
            config_sha256=expected_hash,
            token=service_operation.get("token") if service_operation else None,
            was_running=running,
            capacity=None,
            previous_pid=observed.get("pid"),
            old_plist=controller.plist_bytes().decode()
            if controller.plist_bytes()
            else None,
        )
    else:
        journal.update(state="recovering", recovery_target=release_id)
    write_json(journal_path, journal)

    def step(state):
        journal["state"] = state
        write_json(journal_path, journal)

    with contextlib.ExitStack() as stack:
        if fresh:
            guard = dict(
                quiescent=True,
                admission_closed=True,
                state_schema=CURRENT_STATE_SCHEMA,
                config_schema=1,
            )
        elif running:
            guard = controller.request("maintenance.begin", {"operation_id": operation})
        else:
            try:
                from oraculo.maintenance import maintenance_guard
            except ImportError:
                raise RuntimeError("offline_maintenance_unavailable") from None
            guard = stack.enter_context(maintenance_guard(config, operation))
        if not fresh:
            match_guard(journal, guard)
        journal["token"] = guard.get("token")
        journal["maintenance_end_attempted"] = False
        if not pending or journal.get("capacity") is None:
            # A first attempt that failed before maintenance never observed
            # capacity; the recovered guard is the baseline the restart must keep.
            journal["capacity"] = guard.get("capacity")
        step("maintenance")
        compatible(manifest, guard)
        if guard.get("operation_id", operation) != operation:
            raise RuntimeError("maintenance_correlation")
        if not fresh and not guard.get("token"):
            raise RuntimeError("maintenance_unproven")
        try:
            if running:
                step("stopping")
                controller.stop()
            step("stopped")
            if config_hash(config_path) != expected_hash:
                raise RuntimeError("config_changed")
            step("switching")
            switch(root, release_id)
            step("pointer_switched")
            atomic_write(bin_dir / "oraculo", launcher_bytes(root), 0o755)
            if controller.supported:
                controller.install(release_id)
                journal["replacement"] = controller.recovery_identity(release_id)
                journal["replacement_maintenance"] = dict(
                    operation_id=operation, token=journal["token"]
                )
            step("plist_switched")
            if config_hash(config_path) != expected_hash:
                raise RuntimeError("config_changed")
            # Offline guards hold the singleton lock until all filesystem changes are durable.
        except Exception:
            step("recovery_required")
            raise
    try:
        if journal["was_running"]:
            step("starting")
            controller.start(release_id=release_id, previous_pid=observed.get("pid"))
            after = controller.request("maintenance.status", {})
            compatible(manifest, after)
            if journal["capacity"] != after.get("capacity"):
                raise RuntimeError("capacity_changed")
            if config_hash(config_path) != expected_hash:
                raise RuntimeError("config_changed")
            journal["maintenance_end_attempted"] = True
            write_json(journal_path, journal)
            controller.request(
                "maintenance.end",
                {"operation_id": operation, "token": journal["token"]},
            )
        elif not fresh:
            from oraculo.maintenance import end_maintenance

            journal["maintenance_end_attempted"] = True
            write_json(journal_path, journal)
            end_maintenance(config, operation, journal["token"])
        step("complete")
        if service_operation:
            service_operation["state"] = "complete"
            write_json(service_path, service_operation)
    except Exception:
        step("recovery_required")
        raise
    return receipt(
        "active",
        operation_id=operation,
        release_id=release_id,
        previous=previous,
        service_started=journal["was_running"],
    )


def diagnose(root=DEFAULT_ROOT, bin_dir=DEFAULT_BIN, config=None):
    root, bin_dir = (
        Path(root).expanduser().absolute(),
        Path(bin_dir).expanduser().absolute(),
    )
    result = receipt(
        "uninstalled",
        root=str(root),
        launcher=str(bin_dir / "oraculo"),
        on_path=str(bin_dir) in os.environ.get("PATH", "").split(os.pathsep),
        python=sys.version.split()[0],
        config="missing" if config is None or not Path(config).is_file() else "present",
    )
    try:
        check_launcher(root, bin_dir)
        result["release_id"] = current_release(root)
        result["state"] = "installed" if result["release_id"] else "uninstalled"
        if (root / "operation.json").exists():
            result["operation"] = json.loads((root / "operation.json").read_text())
            result["operation"].pop("token", None)
            result["operation"].pop("replacement_maintenance", None)
            if result["operation"]["state"] != "complete":
                result["state"] = "recovery_required"
    except (RuntimeError, ValueError, OSError) as exc:
        result.update(
            state="failed", error_code=getattr(exc, "code", "invalid_runtime")
        )
    migration_path = root / "legacy-migration.json"
    if migration_path.is_file() and not migration_path.is_symlink():
        try:
            result["migration"] = json.loads(migration_path.read_text())
            result["migration"].pop("token", None)
            if result["migration"].get("state") != "complete":
                result["state"] = "recovery_required"
        except (OSError, ValueError):
            result.update(state="failed", error_code="invalid_migration_journal")
    if not result["on_path"]:
        result["path_action"] = (
            f"Add {bin_dir} to PATH in your shell; no profile is edited automatically."
        )
    return result


def common_options(parser):
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--bin-dir", type=Path, default=DEFAULT_BIN)
    parser.add_argument("--label", default="local.oraculo")
    parser.add_argument(
        "--plist-dir", type=Path, default=Path.home() / "Library/LaunchAgents"
    )
    parser.add_argument(
        "--log-dir", type=Path, default=Path.home() / "Library/Logs/Oraculo"
    )
    parser.add_argument("--timeout", type=float, default=20)
    parser.add_argument("--dry-run", action="store_true")


def register_parsers(subparsers):
    parser = subparsers.add_parser("runtime")
    actions = parser.add_subparsers(dest="runtime_action", required=True)
    for action in (
        "install",
        "upgrade",
        "activate",
        "rollback",
        "status",
        "migrate-legacy",
    ):
        child = actions.add_parser(action)
        common_options(child)
        if action in {"install", "upgrade"}:
            child.add_argument("--bundle", type=Path, required=True)
        if action == "activate":
            child.add_argument("release_id")
        if action == "migrate-legacy":
            child.add_argument("--candidate-root", type=Path, required=True)
            child.add_argument("--release-id", required=True)
            child.add_argument("--expected-legacy-pointer", required=True)
            child.add_argument("--expected-plist-sha256", required=True)
            child.add_argument("--previous-pid", type=int, required=True)


def dispatch(args):
    root, bin_dir = (
        args.root.expanduser().absolute(),
        args.bin_dir.expanduser().absolute(),
    )
    if args.runtime_action == "status":
        return diagnose(root, bin_dir, args.config)
    try:
        manager = contextlib.nullcontext() if args.dry_run else runtime_lock(root)
        with manager:
            if args.runtime_action in {"install", "upgrade"}:
                return prepare(
                    root,
                    bin_dir,
                    args.bundle.expanduser().resolve(),
                    dry_run=args.dry_run,
                    initial=args.runtime_action == "install",
                )
            from oraculo.service_control import ServiceController

            if args.runtime_action == "migrate-legacy":
                from oraculo.runtime_migration import migrate

                return migrate(
                    root,
                    bin_dir,
                    args.config.expanduser().absolute(),
                    ServiceController.from_args(args),
                    args.candidate_root.expanduser(),
                    args.release_id,
                    args.expected_legacy_pointer,
                    args.expected_plist_sha256,
                    args.previous_pid,
                    dry_run=args.dry_run,
                )
            return activate(
                root,
                bin_dir,
                args.config.expanduser().absolute(),
                ServiceController.from_args(args),
                getattr(args, "release_id", None),
                rollback=args.runtime_action == "rollback",
                dry_run=args.dry_run,
            )
    except RuntimeError as exc:
        result = receipt("blocked", error_code=exc.code)
        result["recovery_action"] = exc.recovery
        return result
    except ValueError as exc:
        if type(exc).__name__ != "StateError":
            raise
        result = receipt("blocked", error_code=str(exc))
        result["recovery_action"] = (
            "Reconcile maintenance and active or uncertain work through the official interface."
        )
        return result
    except (OSError, subprocess.SubprocessError) as exc:
        return receipt(
            "failed", error_code="runtime_io_error", detail=type(exc).__name__
        )


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config", type=Path, default=Path.home() / ".config/oraculo/config.toml"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    register_parsers(commands)
    seal = commands.add_parser("manifest")
    seal.add_argument("bundle", type=Path)
    seal.add_argument("--release-id", required=True)
    seal.add_argument("--source-commit", required=True)
    seal.add_argument("--schema-min", type=int, required=True)
    seal.add_argument("--schema-max", type=int, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "manifest":
            result = create_manifest(
                args.bundle,
                args.release_id,
                args.source_commit,
                args.schema_min,
                args.schema_max,
            )
        else:
            result = dispatch(args)
        print(json.dumps(result, ensure_ascii=False))
        return int(result.get("state") in {"failed", "blocked", "recovery_required"})
    except ValueError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
