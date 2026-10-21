"""Explicit offline conversion of a legacy layout; never certifies legacy code."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import plistlib
import shutil
import tempfile
import uuid
from pathlib import Path

from oraculo import runtime as rt


def _unchanged(path, expected):
    if path.is_symlink() or not path.is_file() or rt.digest(path) != expected:
        raise rt.RuntimeError("legacy_artifact_changed")


def _stopped(controller, previous_pid):
    # A successful bootout command or elapsed time is never evidence here.
    if (
        controller.inspect_launchd()["loaded"] is not False
        or controller.alive(previous_pid)
        or controller.socket_present()
    ):
        raise rt.RuntimeError("legacy_stop_unconfirmed")


def _legacy_plist(raw, controller, legacy):
    try:
        value = plistlib.loads(raw)
        arguments = value["ProgramArguments"]
        interpreter = Path(arguments[0])
        tail = arguments[1:]
        if tail[:1] == ["-B"]:
            tail = tail[1:]

        def legacy_directory(path):
            # The legacy plist may use current; on retry that link has already moved.
            return legacy if path == controller.root / "current" else path.resolve()

        if (
            value.get("Label") != controller.label
            or "OraculoManagedRoot" in value
            or interpreter.parent.parent.name != ".venv"
            or legacy_directory(interpreter.parent.parent.parent) != legacy
            or interpreter.name not in {"python", "python3", "python3.12"}
            or tail != ["-m", "oraculo", "--config", str(controller.config), "serve"]
            or legacy_directory(Path(value["WorkingDirectory"])) != legacy
        ):
            raise ValueError("legacy identity")
    except (ValueError, TypeError, KeyError, IndexError, plistlib.InvalidFileException):
        raise rt.RuntimeError("legacy_plist_identity_mismatch") from None


def migrate(
    root,
    bin_dir,
    config_path,
    controller,
    candidate_root,
    release_id,
    expected_legacy_pointer,
    expected_plist_sha256,
    previous_pid,
    *,
    dry_run=False,
):
    """Caller holds destination runtime lock; source lock excludes preparation.

    Retries roll forward using the same durable identity and maintenance token.
    The offline guard may advance the state schema even if later file work fails;
    consequently the archived legacy pointer/plist are evidence, not rollback.
    """
    from oraculo.config import Config
    from oraculo.maintenance import maintenance_guard
    from oraculo.state import State

    controller.require_supported()
    rt.valid_id(release_id)
    if type(previous_pid) is not int or previous_pid <= 1:
        raise ValueError("positive_previous_pid_required")
    if len(expected_plist_sha256) != 64 or any(
        c not in "0123456789abcdef" for c in expected_plist_sha256
    ):
        raise ValueError("expected_plist_sha256_required")
    candidate_root = candidate_root.resolve()
    if (root / "releases").is_symlink() or (candidate_root / "releases").is_symlink():
        raise rt.RuntimeError("release_directory_conflict")
    if (
        candidate_root == root.resolve()
        or candidate_root.is_relative_to(root.resolve())
        or root.resolve().is_relative_to(candidate_root)
    ):
        raise rt.RuntimeError("candidate_requires_alternate_root")
    rt.check_launcher(root, bin_dir)
    config = Config.load(config_path)
    expected_config = config._loaded_sha256
    if expected_config != rt.config_hash(config_path):
        raise rt.RuntimeError("config_changed")
    path = root / "legacy-migration.json"
    if path.is_symlink():
        raise rt.RuntimeError("migration_journal_conflict")
    journal = json.loads(path.read_text()) if path.exists() else None
    legacy = (root / expected_legacy_pointer).resolve()
    if (
        legacy.parent != (root / "releases").resolve()
        or not legacy.is_dir()
        or legacy.name == release_id
    ):
        raise rt.RuntimeError("legacy_pointer_conflict")
    if (legacy / "manifest.json").exists() or not (legacy / ".venv").is_dir():
        raise rt.RuntimeError("legacy_layout_unrecognized")
    intent = dict(
        root=str(root),
        bin_dir=str(bin_dir),
        target=release_id,
        candidate_root=str(candidate_root),
        legacy_pointer=expected_legacy_pointer,
        legacy_path=str(legacy),
        legacy_plist_sha256=expected_plist_sha256,
        config_path=str(controller.config),
        config_sha256=expected_config,
        plist=str(controller.plist),
        launchd_target=controller.target,
        previous_pid=previous_pid,
    )
    if journal and journal.get("intent") != intent:
        raise rt.RuntimeError("migration_intent_mismatch")
    runtime_journal = root / "operation.json"
    if runtime_journal.exists():
        raise rt.RuntimeError("runtime_journal_conflict")
    service_journal = root / "service-operation.json"
    if service_journal.exists():
        service_operation = json.loads(service_journal.read_text())
        if service_operation.get("state") != "complete" and (
            not journal
            or service_operation.get("operation_id") != journal["operation_id"]
            or service_operation.get("token") != journal.get("token")
        ):
            raise rt.RuntimeError("service_operation_pending")
    _stopped(controller, previous_pid)
    source = candidate_root / "releases" / release_id
    source_manager = (
        contextlib.nullcontext() if dry_run else rt.runtime_lock(candidate_root)
    )
    with source_manager:
        manifest = rt.verify(source, installed=True)
        if manifest["release_id"] != release_id:
            raise rt.RuntimeError("release_id_conflict")
        manifest_hash = rt.digest(source / "manifest.json")
        if (
            not manifest["schema_min"]
            <= rt.CURRENT_STATE_SCHEMA
            <= manifest["schema_max"]
        ):
            raise rt.RuntimeError("schema_incompatible")
        if journal and journal["candidate_manifest_sha256"] != manifest_hash:
            raise rt.RuntimeError("candidate_changed")
        pointer = root / "current"
        if not pointer.is_symlink():
            raise rt.RuntimeError("legacy_pointer_conflict")
        selected = os.readlink(pointer)
        allowed = {expected_legacy_pointer}
        if journal:
            allowed.add(str(Path("releases") / release_id))
        if selected not in allowed:
            raise rt.RuntimeError("legacy_pointer_conflict")
        raw = controller.plist_bytes()
        managed_raw = plistlib.dumps(controller.plist_value(release_id))
        legacy_raw = (
            raw is not None and hashlib.sha256(raw).hexdigest() == expected_plist_sha256
        )
        if legacy_raw:
            _legacy_plist(raw, controller, legacy)
        elif not journal or (raw is not None and raw != managed_raw):
            raise rt.RuntimeError("legacy_plist_conflict")
        if journal and journal["state"] == "complete":
            if selected != str(Path("releases") / release_id) or raw != managed_raw:
                raise rt.RuntimeError("migration_result_changed")
            rt.verify(root / "releases" / release_id, installed=True)
            return rt.receipt(
                "migrated",
                operation_id=journal["operation_id"],
                release_id=release_id,
                service_started=False,
                legacy_rollback_supported=False,
            )
        if dry_run:
            return rt.receipt(
                "planned",
                release_id=release_id,
                maintenance_required=True,
                service_started=False,
                legacy_rollback_supported=False,
            )
        if not journal:
            journal = dict(
                schema_version=1,
                operation_id=str(uuid.uuid4()),
                state="acquiring_maintenance",
                intent=intent,
                candidate_manifest_sha256=manifest_hash,
                token=None,
                config_sha256=expected_config,
                legacy_rollback_supported=False,
            )
            rt.write_json(path, journal)
        backup = root / "legacy-backups" / journal["operation_id"]

        def step(state):
            journal["state"] = state
            rt.write_json(path, journal)

        def stable():
            _stopped(controller, previous_pid)
            if rt.config_hash(config_path) != expected_config:
                raise rt.RuntimeError("config_changed")

        with maintenance_guard(config, journal["operation_id"]) as guard:
            rt.match_guard(journal, guard)
            rt.compatible(manifest, guard)
            journal["token"] = guard["token"]
            if "capacity" in journal and journal["capacity"] != guard.get("capacity"):
                raise rt.RuntimeError("capacity_changed")
            journal["capacity"] = guard.get("capacity")
            step("maintenance")
            stable()
            if backup.is_symlink() or backup.parent.is_symlink():
                raise rt.RuntimeError("migration_backup_conflict")
            backup.parent.mkdir(mode=0o700, exist_ok=True)
            backup.mkdir(mode=0o700, exist_ok=True)
            saved_plist = backup / "legacy.plist"
            saved_pointer = backup / "current.link"
            if not saved_plist.exists():
                if not legacy_raw:
                    raise rt.RuntimeError("migration_backup_missing")
                rt.atomic_write(saved_plist, raw)
            _unchanged(saved_plist, expected_plist_sha256)
            pointer_bytes = expected_legacy_pointer.encode()
            if not saved_pointer.exists():
                rt.atomic_write(saved_pointer, pointer_bytes)
            _unchanged(saved_pointer, hashlib.sha256(pointer_bytes).hexdigest())
            if "state_backup" not in journal:
                # This snapshot is made AFTER the official guard opens schema 4.
                # Keep even interrupted snapshots; never restore them automatically.
                snapshot = backup / ("state-schema4-" + uuid.uuid4().hex + ".sqlite3")
                fd = os.open(
                    snapshot, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600
                )
                os.close(fd)
                state = State(config.db_path, max_executors=config.max_executors)
                try:
                    state.backup(snapshot)
                finally:
                    state.close()
                with snapshot.open("rb") as stream:
                    os.fsync(stream.fileno())
                rt.sync_dir(backup)
                journal["state_backup"] = dict(
                    path=str(snapshot),
                    sha256=rt.digest(snapshot),
                    state_schema=guard["state_schema"],
                    source="official_State.backup_under_offline_guard",
                )
                rt.write_json(path, journal)
            saved_state = journal["state_backup"]
            snapshot = Path(saved_state["path"])
            if snapshot.parent != backup:
                raise rt.RuntimeError("migration_backup_conflict")
            _unchanged(snapshot, saved_state["sha256"])
            step("backed_up")
            target = root / "releases" / release_id
            if not target.exists() and not target.is_symlink():
                stage = Path(tempfile.mkdtemp(prefix=".migrate-", dir=target.parent))
                # A partial copy is never selected. Leave interrupted staging for inspection.
                shutil.copytree(source, stage, dirs_exist_ok=True, symlinks=True)
                rt.verify(stage, installed=True)
                for item in stage.rglob("*"):
                    if not item.is_symlink():
                        if item.is_file():
                            with item.open("rb") as stream:
                                os.fsync(stream.fileno())
                        else:
                            rt.sync_dir(item)
                        item.chmod(item.stat().st_mode & ~0o222)
                rt.sync_dir(stage)
                stage.chmod(0o555)
                os.replace(stage, target)
                rt.sync_dir(target.parent)
            rt.verify(target, installed=True)
            if rt.digest(target / "manifest.json") != manifest_hash:
                raise rt.RuntimeError("release_id_conflict")
            step("candidate_copied")
            stable()
            if os.readlink(pointer) not in allowed:
                raise rt.RuntimeError("legacy_pointer_conflict")
            step("switching_pointer")
            rt.switch(root, release_id)
            step("pointer_switched")
            # Only archive the exact explicitly authorized legacy plist. Never adopt it.
            current_raw = controller.plist_bytes()
            if current_raw == saved_plist.read_bytes():
                step("archiving_plist")
                controller.plist.unlink()
                rt.sync_dir(controller.plist.parent)
            elif current_raw is not None and current_raw != managed_raw:
                raise rt.RuntimeError("legacy_plist_conflict")
            stable()
            controller.install(release_id)
            rt.atomic_write(bin_dir / "oraculo", rt.launcher_bytes(root), 0o755)
            step("installed")
            stable()
            # Keep admission closed until the explicit, identity-checked service start.
            rt.write_json(
                service_journal,
                dict(
                    operation_id=journal["operation_id"],
                    token=journal["token"],
                    state="stopped",
                    config_sha256=expected_config,
                ),
            )
            step("complete")
    return rt.receipt(
        "migrated",
        operation_id=journal["operation_id"],
        release_id=release_id,
        backup=str(backup),
        service_started=False,
        legacy_rollback_supported=False,
    )
