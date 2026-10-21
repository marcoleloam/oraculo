"""Conservative, evidence-selected installation of the single Oraculo skill.

Discovery records describe observations supplied by the operator, not attestation.
This module never starts a model, edits provider settings or contacts the daemon.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from oraculo.config import resources_root

MANIFEST = ".oraculo-install.json"
PROVIDERS = {
    "codex": (".agents/skills", ".codex/skills"),
    "claude": (".claude/skills",),
    "grok": (".grok/skills", ".claude/skills", ".agents/skills"),
}
SOURCES = {
    "codex": "https://learn.chatgpt.com/docs/build-skills",
    "claude": "https://code.claude.com/docs/en/skills",
    "grok": "https://docs.x.ai/build/features/skills-plugins-marketplaces",
}


class SkillConflict(Exception):
    def __init__(self, code: str, paths: list[str] | None = None):
        self.code = code
        self.paths = paths or []
        super().__init__(code)


def _absolute(path: Path | str) -> Path:
    return Path(os.path.abspath(Path(path).expanduser()))


def _safe_path(path: Path) -> None:
    for part in (path, *path.parents):
        if part.is_symlink():
            raise SkillConflict("skill_symlink_conflict", [str(part)])
        if part.exists() and part != path and not part.is_dir():
            raise SkillConflict("skill_path_conflict", [str(part)])


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _source_files() -> dict[str, bytes]:
    source = resources_root() / "skills" / "oraculo"
    if not (source / "SKILL.md").is_file():
        raise SkillConflict("skill_resources_missing")
    files = {}
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            raise SkillConflict("skill_source_symlink", [str(path)])
        if path.is_file():
            name = path.relative_to(source).as_posix()
            if name == MANIFEST:
                raise SkillConflict("skill_source_manifest_reserved")
            files[name] = path.read_bytes()
    return files


def _hashes(files: dict[str, bytes]) -> dict[str, str]:
    return {name: _digest(raw) for name, raw in sorted(files.items())}


def _bundle_hash(hashes: dict[str, str]) -> str:
    return _digest(json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode())


def _evidence(path: Path | None, provider: str) -> dict | None:
    if path is None:
        return None
    try:
        data = json.loads(path.read_text())
        if (
            data["schema_version"] != 1
            or data["provider"] != provider
            or data["mechanism"]
            not in {"harness_catalog", "grok_inspect", "isolated_fixture"}
            or not all(
                isinstance(data[k], str) and data[k].strip()
                for k in (
                    "harness_version",
                    "provider_instance_id",
                    "source",
                    "observed_at",
                )
            )
            or not isinstance(data["roots"], list)
            or not data["roots"]
            or not all(
                isinstance(p, str) and Path(p).is_absolute() for p in data["roots"]
            )
            or not isinstance(data.get("skill_sources", []), list)
            or not all(
                isinstance(p, str) and Path(p).is_absolute()
                for p in data.get("skill_sources", [])
            )
        ):
            raise ValueError
        datetime.fromisoformat(data["observed_at"].replace("Z", "+00:00"))
        return data
    except (OSError, ValueError, KeyError, TypeError):
        raise ValueError("invalid_skill_discovery_evidence") from None


def _roots(provider: str, home: Path, project: Path | None) -> list[Path]:
    roots = [home / name for name in PROVIDERS[provider]]
    if project is not None:
        for base in (project, *project.parents):
            roots.extend(base / name for name in PROVIDERS[provider])
            if (base / ".git").exists():
                break
    return list(dict.fromkeys(roots))


def _discover(roots: list[Path], evidence: dict | None) -> list[str]:
    # Bounded known-name checks; plugin/custom sources must come from the catalog.
    paths = set(evidence.get("skill_sources", []) if evidence else [])
    for root in roots:
        candidate = root / "oraculo"
        if candidate.exists() or candidate.is_symlink():
            paths.add(str(candidate / "SKILL.md"))
        command = root.parent / "commands" / "oraculo.md"
        if command.exists() or command.is_symlink():
            paths.add(str(command))
    return sorted(str(_absolute(p)) for p in paths)


def _manifest(target: Path) -> dict | None:
    path = target / MANIFEST
    _safe_path(path)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        files = data["files"]
        if (
            data["schema_version"] != 1
            or data["skill"] != "oraculo"
            or data["provider"] not in PROVIDERS
            or not isinstance(files, dict)
            or "SKILL.md" not in files
        ):
            raise ValueError
        for name, digest in files.items():
            rel = PurePosixPath(name)
            if (
                rel.is_absolute()
                or str(rel) != name
                or ".." in rel.parts
                or "\\" in name
                or name == MANIFEST
                or not isinstance(digest, str)
                or not re.fullmatch(r"[0-9a-f]{64}", digest)
            ):
                raise ValueError
        if data["source_sha256"] != _bundle_hash(files):
            raise ValueError
        return data
    except (OSError, ValueError, KeyError, TypeError):
        raise SkillConflict("skill_manifest_invalid", [str(path)]) from None


def _verify(target: Path, manifest: dict) -> None:
    conflicts = []
    for name, expected in manifest["files"].items():
        path = target / name
        _safe_path(path)
        if not path.is_file() or _digest(path.read_bytes()) != expected:
            conflicts.append(name)
    if conflicts:
        raise SkillConflict("skill_external_edits", conflicts)


def _extras(target: Path, files: dict) -> list[str]:
    managed_dirs = {
        str(parent)
        for name in files
        for parent in PurePosixPath(name).parents
        if str(parent) != "."
    }
    return sorted(
        p.relative_to(target).as_posix()
        for p in target.rglob("*")
        if (
            not p.is_dir()
            or p.is_symlink()
            or p.relative_to(target).as_posix() not in managed_dirs
        )
        and p.relative_to(target).as_posix() not in {*files, MANIFEST}
    )


def _remove_owned(target: Path, manifest: dict) -> list[str]:
    # Preflight the whole inventory first. A mismatch preserves the entire install.
    _verify(target, manifest)
    extras = _extras(target, manifest["files"])
    for name in manifest["files"]:
        (target / name).unlink()
    (target / MANIFEST).unlink()
    directories = sorted(
        {
            target / parent
            for name in manifest["files"]
            for parent in PurePosixPath(name).parents
            if str(parent) != "."
        },
        key=lambda p: len(p.parts),
        reverse=True,
    )
    for path in [*directories, target]:
        try:
            path.rmdir()
        except OSError:
            pass
    return extras


def _install(
    target: Path, files: dict[str, bytes], manifest: dict, previous: dict | None
) -> None:
    backup = target.parent / ".oraculo-previous"
    if backup.exists() or backup.is_symlink():
        raise SkillConflict("skill_interrupted_install", [str(backup)])
    stage = Path(tempfile.mkdtemp(prefix=".oraculo-stage-", dir=target.parent))
    try:
        for name, raw in files.items():
            path = stage / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
        (stage / MANIFEST).write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n"
        )
        if previous:
            _verify(target, previous)
            if _extras(target, previous["files"]):
                raise SkillConflict("skill_update_has_external_files")
            target.rename(backup)
        try:
            stage.rename(target)
        except BaseException:
            if previous:
                backup.rename(target)
            raise
        if previous:
            _remove_owned(backup, previous)
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def diagnose(
    *,
    provider: str = "codex",
    root: Path | None = None,
    home: Path | None = None,
    project: Path | None = None,
    evidence: Path | None = None,
) -> dict:
    return operate(
        "inspect",
        provider=provider,
        root=root,
        home=home,
        project=project,
        evidence=evidence,
    )


def operate(
    action: str,
    *,
    provider: str,
    root: Path | None = None,
    home: Path | None = None,
    project: Path | None = None,
    evidence: Path | None = None,
    dry_run: bool = False,
) -> dict:
    if provider not in PROVIDERS or action not in {"inspect", "install", "uninstall"}:
        raise ValueError("invalid_skill_operation")
    data = _evidence(evidence, provider)
    home = _absolute(home or Path.home())
    project = _absolute(project) if project else None
    roots = _roots(provider, home, project)
    observed = [_absolute(p) for p in data["roots"]] if data else []
    chosen = _absolute(root) if root else (observed[0] if len(observed) == 1 else None)
    result = {
        "schema_version": 1,
        "state": "inspected",
        "error_code": None,
        "retryable": False,
        "recovery_action": None,
        "operation_id": str(uuid.uuid4()),
        "source": "local_skill_files",
        "observed_at": datetime.now(UTC).isoformat(),
        "provider": provider,
        "destination": str(chosen / "oraculo") if chosen else None,
        "documented_roots": [str(home / PROVIDERS[provider][0])],
        "documentation": SOURCES[provider],
        "discovery_evidence": data,
        "support": "documented",
        "dry_run": dry_run,
        "loaded_skill": "not_observed",
        "called_cli": "not_observed",
        "confirmed_service": "not_observed",
    }
    lock_fd = None
    try:
        roots = list(dict.fromkeys([*roots, *observed, *([chosen] if chosen else [])]))
        found = _discover(roots, data)
        result["discovered_sources"] = found
        if found:
            result["support"] = "detected"
        if action != "inspect" and chosen is None:
            raise SkillConflict("skill_destination_required")
        if action == "install" and (data is None or chosen not in observed):
            raise SkillConflict("skill_discovery_evidence_required")
        if chosen is None:
            return result
        target = chosen / "oraculo"
        _safe_path(target)
        if target.exists() and not target.is_dir():
            raise SkillConflict("skill_path_conflict", [str(target)])
        if (chosen / ".oraculo-previous").exists() or (
            chosen / ".oraculo-previous"
        ).is_symlink():
            raise SkillConflict(
                "skill_interrupted_install", [str(chosen / ".oraculo-previous")]
            )
        if action != "inspect" and not dry_run:
            if action == "uninstall" and not target.exists():
                result["state"] = "absent"
                return result
            chosen.mkdir(parents=True, exist_ok=True)
            lock_fd = os.open(
                chosen / ".oraculo-install.lock",
                os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
                0o600,
            )
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise SkillConflict("skill_install_busy") from None
        previous = _manifest(target) if target.exists() else None
        result["manifest"] = previous
        if previous:
            _verify(target, previous)
            result["integrity"] = "verified"
        if action == "inspect":
            result["conflicts"] = [p for p in found if p != str(target / "SKILL.md")]
            return result
        if target.exists() and previous is None:
            raise SkillConflict("skill_unmanaged_destination", [str(target)])
        if previous and previous["provider"] != provider:
            raise SkillConflict("skill_owned_by_other_provider", [previous["provider"]])
        if action == "uninstall":
            result["state"] = "absent" if previous is None else "uninstalled"
            if previous:
                result["preserved"] = _extras(target, previous["files"])
                if not dry_run:
                    _remove_owned(target, previous)
            return result
        duplicates = [p for p in found if p != str(target / "SKILL.md")]
        if duplicates:
            raise SkillConflict("skill_duplicate_discovery", duplicates)
        files = _source_files()
        hashes = _hashes(files)
        result["source_sha256"] = _bundle_hash(hashes)
        result["files"] = hashes
        if previous and previous["files"] == hashes:
            result["state"] = "unchanged"
            return result
        if previous and _extras(target, previous["files"]):
            raise SkillConflict("skill_update_has_external_files")
        result["state"] = "updated" if previous else "installed"
        if not dry_run:
            manifest = {
                "schema_version": 1,
                "skill": "oraculo",
                "provider": provider,
                "source_sha256": result["source_sha256"],
                "files": hashes,
                "installed_at": result["observed_at"],
                "discovery_evidence": data,
            }
            _install(target, files, manifest, previous)
            result["manifest"] = manifest
        return result
    except SkillConflict as exc:
        result.update(
            state="blocked",
            error_code=exc.code,
            conflicts=exc.paths,
            retryable=exc.code == "skill_install_busy",
            recovery_action="Inspect the reported files and discovery evidence; preserve external edits. Retry the same destination after resolving the conflict.",
        )
        return result
    except OSError as exc:
        result.update(
            state="failed",
            error_code="skill_io_error",
            retryable=False,
            io_errno=exc.errno,
            recovery_action="Inspect destination and .oraculo-previous before retrying; do not overwrite uncertain files.",
        )
        return result
    finally:
        if lock_fd is not None:
            os.close(lock_fd)


def register_parsers(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "skill", help="Inspect/install one evidence-selected skill destination"
    )
    parser.add_argument("action", choices=["inspect", "install", "uninstall"])
    parser.add_argument("--provider", required=True, choices=list(PROVIDERS))
    parser.add_argument(
        "--root",
        type=Path,
        help="One provider skills directory (not the oraculo subdirectory)",
    )
    parser.add_argument(
        "--home",
        type=Path,
        help="Home used for discovery, including isolated acceptance",
    )
    parser.add_argument(
        "--project", type=Path, help="Project cwd whose ancestors are inspected"
    )
    parser.add_argument(
        "--evidence",
        type=Path,
        help="Private discovery observation JSON; required for installation",
    )
    parser.add_argument("--dry-run", action="store_true")


def dispatch(args: argparse.Namespace) -> dict:
    return operate(
        args.action,
        provider=args.provider,
        root=args.root,
        home=args.home,
        project=args.project,
        evidence=args.evidence,
        dry_run=args.dry_run,
    )
