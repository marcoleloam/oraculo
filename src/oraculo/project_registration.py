"""Addition-only project authorization outside the pinned config file.

The running daemon pins the bytes of ``config.toml``. Editing ``allowed_projects``
changes that hash and is not a supported reload. This module publishes one
narrow policy file under the operational state directory. Scope checks read it
on use, so a live process and a later restart see the same additions without a
new config hash.

Absent policy preserves static ``allowed_projects``. An invalid document grants
no dynamic path and does not rewrite itself. There is no removal operation.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from oraculo.protocol import canonical_json, content_hash

POLICY_NAME = "project-authorization.json"
LOCK_NAME = "project-authorization.lock"
SCHEMA = "oraculo-project-registration-v1"
MAX_POLICY_BYTES = 262144
MAX_PROJECTS = 256
LOCK_WAIT_SECONDS = 2.0
ENTRY_KEYS = {
    "base_policy_sha256",
    "catalog_sha256",
    "config_sha256",
    "confirmation",
    "path",
    "project_id",
}
EMPTY_BODY = {"schema_version": 1, "projects": []}


class ProjectRegistrationError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def empty_policy_sha256() -> str:
    return content_hash(EMPTY_BODY)


def plan_registration(
    config: Any,
    catalog: list[dict[str, Any]],
    project_id: str,
    path: str,
    *,
    config_path: Path | None = None,
) -> dict[str, Any]:
    """Observe one catalog identity and return a confirmation. Writes nothing."""
    config_sha256 = pinned_config_sha256(config, config_path)
    current = load_policy(config)
    identity = observe_identity(config, catalog, project_id, path)
    existing = _same_identity(current["projects"], identity)
    if existing is not None:
        if existing["config_sha256"] != config_sha256:
            raise ProjectRegistrationError("registration_config_changed")
        return _plan_result(
            identity,
            existing["base_policy_sha256"],
            config_sha256,
            existing["confirmation"],
            already_authorized=True,
        )
    base = current["document_sha256"]
    confirmation = confirmation_for(identity, base, config_sha256)
    return _plan_result(identity, base, config_sha256, confirmation)


def apply_registration(
    config: Any,
    catalog: list[dict[str, Any]],
    project_id: str,
    path: str,
    confirmation: str,
    *,
    config_path: Path | None = None,
) -> dict[str, Any]:
    """Publish one confirmed project. Idempotent replay does not rewrite."""
    if not _hex64(confirmation):
        raise ProjectRegistrationError("registration_confirmation_invalid")
    config_sha256 = pinned_config_sha256(config, config_path)
    with _exclusive(config):
        identity = observe_identity(config, catalog, project_id, path)
        current = load_policy(config)
        existing = _same_identity(current["projects"], identity)
        if existing is not None:
            if (
                existing["confirmation"] != confirmation
                or existing["config_sha256"] != config_sha256
            ):
                raise ProjectRegistrationError("registration_duplicate")
            return _apply_result(
                current, changed=False, replayed=True, config_sha256=config_sha256
            )
        if any(item["path"] == identity["path"] for item in current["projects"]):
            raise ProjectRegistrationError("registration_duplicate")
        expected = confirmation_for(identity, current["document_sha256"], config_sha256)
        if confirmation != expected:
            raise ProjectRegistrationError("registration_confirmation_mismatch")
        if len(current["projects"]) >= MAX_PROJECTS:
            raise ProjectRegistrationError("registration_policy_invalid")
        entry = {
            "project_id": identity["project_id"],
            "path": identity["path"],
            "catalog_sha256": identity["catalog_sha256"],
            "base_policy_sha256": current["document_sha256"],
            "config_sha256": config_sha256,
            "confirmation": confirmation,
        }
        projects = sorted(
            [*current["projects"], entry], key=lambda item: item["project_id"]
        )
        document = _document(projects)
        _publish(config.state_dir, document)
        reloaded = load_policy(config)
        if reloaded["document_sha256"] != document["document_sha256"]:
            raise ProjectRegistrationError("registration_policy_invalid")
        return _apply_result(
            reloaded, changed=True, replayed=False, config_sha256=config_sha256
        )


def dynamic_scope_contains(config: Any, candidate: Path) -> bool:
    """Return whether a resolved directory is an explicit dynamic addition."""
    try:
        for entry in load_policy(config)["projects"]:
            if not _entry_live(config.projects_root, entry["path"]):
                continue
            if Path(entry["path"]).resolve() == candidate:
                return True
    except (OSError, RuntimeError, ProjectRegistrationError):
        return False
    return False


def admit_project(state: Any, config: Any, project_id: str) -> dict | None:
    """Insert one published project when the local row is absent.

    A catalog tick may later mark that row unregistered, or store another
    path. Status, project lookup, and direct memory do not override either
    case; only a later tick that observes the project again may re-register it.
    """
    if not isinstance(project_id, str):
        return None
    try:
        entries = load_policy(config)["projects"]
    except ProjectRegistrationError:
        return None
    entry = next((item for item in entries if item["project_id"] == project_id), None)
    if entry is None or not _entry_live(config.projects_root, entry["path"]):
        return None
    if not config.project_in_scope(entry["path"]):
        return None
    current = state.get_project(project_id)
    if current is not None:
        if current.get("status") != "registered":
            return None
        try:
            same = (
                Path(current["path"]).expanduser().resolve()
                == Path(entry["path"]).resolve()
            )
        except (OSError, RuntimeError):
            return None
        if not same:
            return None
        return current
    return state.register_project(project_id, entry["path"], "registered")


def admit_authorized_projects(state: Any, config: Any) -> None:
    try:
        entries = load_policy(config)["projects"]
    except ProjectRegistrationError:
        return
    for entry in entries:
        admit_project(state, config, entry["project_id"])


async def load_t3_catalog(config: Any) -> list[dict[str, Any]]:
    """Fresh project list from the configured T3 endpoint. Not a path payload."""
    from oraculo.t3.auth import build_auth
    from oraculo.t3.client import T3Client

    async with T3Client(config.t3_mcp_url, build_auth(config.t3_mcp_url)) as client:
        projects = await client.list_projects()
    if not isinstance(projects, list):
        raise ProjectRegistrationError("registration_catalog_invalid")
    return projects


async def cli_command(config: Any, args: Any) -> dict[str, Any]:
    if getattr(args, "project_action", None) != "register" or args.phase not in {
        "plan",
        "apply",
    }:
        raise ProjectRegistrationError("registration_command_invalid")
    catalog = await load_t3_catalog(config)
    if args.phase == "plan":
        return plan_registration(
            config, catalog, args.id, args.path, config_path=args.config
        )
    return apply_registration(
        config,
        catalog,
        args.id,
        args.path,
        args.confirmation,
        config_path=args.config,
    )


def pinned_config_sha256(config: Any, config_path: Path | None) -> str:
    loaded = getattr(config, "_loaded_sha256", None)
    if not isinstance(loaded, str) or len(loaded) != 64:
        raise ProjectRegistrationError("registration_config_unpinned")
    try:
        int(loaded, 16)
    except ValueError:
        raise ProjectRegistrationError("registration_config_unpinned") from None
    if config_path is not None:
        try:
            current = hashlib.sha256(Path(config_path).read_bytes()).hexdigest()
        except OSError:
            raise ProjectRegistrationError("registration_config_changed") from None
        if current != loaded:
            raise ProjectRegistrationError("registration_config_changed")
    return loaded


def observe_identity(
    config: Any, catalog: list[dict[str, Any]], project_id: str, path: str
) -> dict[str, str]:
    if not isinstance(catalog, list):
        raise ProjectRegistrationError("registration_catalog_invalid")
    _validate_project_id(project_id)
    matches = []
    for item in catalog:
        if not isinstance(item, dict) or not _catalog_identity(item):
            raise ProjectRegistrationError("registration_catalog_invalid")
        if item.get("id") == project_id:
            matches.append(item)
    if not matches:
        raise ProjectRegistrationError("registration_project_unknown")
    if len(matches) != 1:
        raise ProjectRegistrationError("registration_project_ambiguous")
    item = matches[0]
    if item.get("deletedAt"):
        raise ProjectRegistrationError("registration_project_deleted")
    requested = canonical_project_directory(config.projects_root, path)
    observed = canonical_project_directory(config.projects_root, item["workspaceRoot"])
    if requested != observed:
        raise ProjectRegistrationError("registration_identity_mismatch")
    canonical = str(requested)
    return {
        "project_id": project_id,
        "path": canonical,
        "catalog_sha256": content_hash(
            {"deleted": False, "id": project_id, "path": canonical}
        ),
    }


def canonical_project_directory(projects_root: Path, raw: str) -> Path:
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        raise ProjectRegistrationError("registration_path_invalid")
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute() or ".." in candidate.parts:
        raise ProjectRegistrationError("registration_path_invalid")
    root = Path(projects_root).resolve()
    if not root.is_dir() or root.is_symlink():
        raise ProjectRegistrationError("registration_path_invalid")
    _reject_project_symlink(root, candidate)
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        raise ProjectRegistrationError("registration_path_invalid") from None
    if resolved == root:
        raise ProjectRegistrationError("registration_path_is_root")
    try:
        relative = resolved.relative_to(root)
    except ValueError:
        raise ProjectRegistrationError("registration_path_outside_root") from None
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise ProjectRegistrationError("registration_path_invalid")
    current = root
    for part in relative.parts:
        current = current / part
        try:
            if current.is_symlink():
                raise ProjectRegistrationError("registration_path_symlink")
        except ProjectRegistrationError:
            raise
        except (OSError, RuntimeError):
            raise ProjectRegistrationError("registration_path_invalid") from None
    if not resolved.is_dir():
        raise ProjectRegistrationError("registration_path_not_directory")
    return resolved


def confirmation_for(
    identity: dict[str, str], base_policy_sha256: str, config_sha256: str
) -> str:
    return content_hash(
        {
            "schema": SCHEMA,
            "project_id": identity["project_id"],
            "path": identity["path"],
            "catalog_sha256": identity["catalog_sha256"],
            "base_policy_sha256": base_policy_sha256,
            "config_sha256": config_sha256,
        }
    )


def load_policy(config: Any) -> dict[str, Any]:
    raw = _read_bytes(_policy_file(config))
    if raw is None:
        digest = empty_policy_sha256()
        return {
            "schema_version": 1,
            "projects": [],
            "document_sha256": digest,
            "present": False,
        }
    try:
        parsed = json.loads(raw, object_pairs_hook=_strict_object)
    except (UnicodeError, json.JSONDecodeError, ProjectRegistrationError):
        raise ProjectRegistrationError("registration_policy_invalid") from None
    if not isinstance(parsed, dict) or set(parsed) != {
        "schema_version",
        "projects",
        "document_sha256",
    }:
        raise ProjectRegistrationError("registration_policy_invalid")
    if type(parsed["schema_version"]) is not int or parsed["schema_version"] != 1:
        raise ProjectRegistrationError("registration_policy_invalid")
    projects = parsed["projects"]
    if (
        not isinstance(projects, list)
        or len(projects) > MAX_PROJECTS
        or not _hex64(parsed["document_sha256"])
    ):
        raise ProjectRegistrationError("registration_policy_invalid")
    body = {"schema_version": 1, "projects": projects}
    if content_hash(body) != parsed["document_sha256"]:
        raise ProjectRegistrationError("registration_policy_invalid")
    normalized = []
    seen_ids: set[str] = set()
    seen_paths: set[str] = set()
    for entry in projects:
        normalized.append(_validated_entry(config, entry))
        project_id = entry["project_id"]
        path = entry["path"]
        if project_id in seen_ids or path in seen_paths:
            raise ProjectRegistrationError("registration_policy_invalid")
        seen_ids.add(project_id)
        seen_paths.add(path)
    parsed["projects"] = normalized
    parsed["present"] = True
    return parsed


def _validated_entry(config: Any, entry: Any) -> dict[str, str]:
    if not isinstance(entry, dict) or set(entry) != ENTRY_KEYS:
        raise ProjectRegistrationError("registration_policy_invalid")
    project_id = entry["project_id"]
    path = entry["path"]
    try:
        _validate_project_id(project_id)
    except ProjectRegistrationError:
        raise ProjectRegistrationError("registration_policy_invalid") from None
    if not all(
        _hex64(entry[key])
        for key in (
            "catalog_sha256",
            "base_policy_sha256",
            "config_sha256",
            "confirmation",
        )
    ):
        raise ProjectRegistrationError("registration_policy_invalid")
    _lexical_project_path(config.projects_root, path)
    expected = confirmation_for(
        {
            "project_id": project_id,
            "path": path,
            "catalog_sha256": entry["catalog_sha256"],
        },
        entry["base_policy_sha256"],
        entry["config_sha256"],
    )
    if expected != entry["confirmation"]:
        raise ProjectRegistrationError("registration_policy_invalid")
    return {key: entry[key] for key in sorted(ENTRY_KEYS)}


def _reject_project_symlink(root: Path, candidate: Path) -> None:
    """Refuse a symlink in the project suffix. Prefixes such as /tmp may be links."""
    current = candidate
    while current != current.parent:
        try:
            linked = current.is_symlink()
        except (OSError, RuntimeError):
            raise ProjectRegistrationError("registration_path_invalid") from None
        if linked:
            try:
                resolved = current.resolve()
            except (OSError, RuntimeError):
                raise ProjectRegistrationError("registration_path_symlink") from None
            if resolved == root or root.is_relative_to(resolved):
                return
            raise ProjectRegistrationError("registration_path_symlink")
        if current == root:
            return
        current = current.parent


def _lexical_project_path(projects_root: Path, stored: str) -> None:
    """Reject stored paths that are not strictly inside the root, without I/O."""
    if not isinstance(stored, str) or not stored or "\x00" in stored:
        raise ProjectRegistrationError("registration_policy_invalid")
    path = Path(stored)
    root = Path(projects_root).resolve()
    if not path.is_absolute() or not root.is_absolute() or ".." in path.parts:
        raise ProjectRegistrationError("registration_policy_invalid")
    try:
        relative = path.relative_to(root)
    except ValueError:
        raise ProjectRegistrationError("registration_policy_invalid") from None
    if not relative.parts or path == root:
        raise ProjectRegistrationError("registration_policy_invalid")


def _entry_live(projects_root: Path, stored: str) -> bool:
    path = Path(stored)
    root = Path(projects_root).resolve()
    try:
        if path.is_symlink() or not path.is_absolute():
            return False
        resolved = path.resolve(strict=False)
        relative = resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return False
    if not relative.parts or resolved == root or str(resolved) != stored:
        return False
    current = root
    for part in relative.parts:
        current = current / part
        try:
            if current.is_symlink():
                return False
        except (OSError, RuntimeError):
            return False
    return True


def _same_identity(
    projects: list[dict[str, str]], identity: dict[str, str]
) -> dict[str, str] | None:
    for item in projects:
        if item["project_id"] == identity["project_id"]:
            if (
                item["path"] == identity["path"]
                and item["catalog_sha256"] == identity["catalog_sha256"]
            ):
                return item
            raise ProjectRegistrationError("registration_duplicate")
    return None


def _document(projects: list[dict[str, str]]) -> dict[str, Any]:
    body = {"schema_version": 1, "projects": projects}
    return body | {"document_sha256": content_hash(body)}


def _plan_result(
    identity: dict[str, str],
    base: str,
    config_sha256: str,
    confirmation: str,
    *,
    already_authorized: bool = False,
) -> dict[str, Any]:
    return {
        "state": "already_authorized"
        if already_authorized
        else "confirmation_required",
        "project_id": identity["project_id"],
        "path": identity["path"],
        "catalog_sha256": identity["catalog_sha256"],
        "base_policy_sha256": base,
        "config_sha256": config_sha256,
        "confirmation": confirmation,
        "publishes": False,
    }


def _apply_result(
    document: dict[str, Any],
    *,
    changed: bool,
    replayed: bool,
    config_sha256: str,
) -> dict:
    return {
        "state": "replayed" if replayed else "published",
        "changed": changed,
        "policy_sha256": document["document_sha256"],
        "config_sha256": config_sha256,
        "projects": [item["project_id"] for item in document["projects"]],
        "publishes_config": False,
        "restarts_service": False,
    }


def _policy_file(config: Any) -> Path:
    directory = Path(config.state_dir)
    if directory.is_symlink() or not directory.is_dir():
        raise ProjectRegistrationError("registration_state_dir_invalid")
    try:
        info = directory.stat()
    except OSError:
        raise ProjectRegistrationError("registration_state_dir_invalid") from None
    if info.st_uid != os.getuid():
        raise ProjectRegistrationError("registration_state_dir_invalid")
    return directory / POLICY_NAME


def _read_bytes(path: Path) -> bytes | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        raise ProjectRegistrationError("registration_policy_invalid") from None
    if stat.S_ISLNK(info.st_mode):
        raise ProjectRegistrationError("registration_policy_symlink")
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_POLICY_BYTES:
        raise ProjectRegistrationError("registration_policy_invalid")
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        raise ProjectRegistrationError("registration_policy_invalid") from None
    with os.fdopen(descriptor, "rb") as stream:
        return stream.read(MAX_POLICY_BYTES + 1)


@contextmanager
def _exclusive(config: Any) -> Iterator[None]:
    directory = _policy_file(config).parent
    lock_path = directory / LOCK_NAME
    if lock_path.is_symlink():
        raise ProjectRegistrationError("registration_policy_symlink")
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    acquired = False
    try:
        deadline = time.monotonic() + LOCK_WAIT_SECONDS
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ProjectRegistrationError(
                        "registration_lock_timeout"
                    ) from None
                time.sleep(0.01)
        yield
    finally:
        if acquired:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _publish(directory: Path, document: dict[str, Any]) -> None:
    path = directory / POLICY_NAME
    if path.is_symlink():
        raise ProjectRegistrationError("registration_policy_symlink")
    payload = (canonical_json(document) + "\n").encode("utf-8")
    if len(payload) > MAX_POLICY_BYTES:
        raise ProjectRegistrationError("registration_policy_invalid")
    temporary = directory / f".{POLICY_NAME}.{uuid.uuid4().hex}.tmp"
    descriptor = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    directory_fd = os.open(directory, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    path.chmod(0o600)


def _catalog_identity(item: dict[str, Any]) -> bool:
    return isinstance(item.get("id"), str) and isinstance(
        item.get("workspaceRoot"), str
    )


def _validate_project_id(project_id: str) -> None:
    if (
        not isinstance(project_id, str)
        or not project_id
        or project_id != project_id.strip()
        or len(project_id) > 200
        or any(ord(char) < 32 or ord(char) == 127 for char in project_id)
        or any(char in project_id for char in "/\\")
        or ".." in project_id
    ):
        raise ProjectRegistrationError("registration_project_id_invalid")


def _hex64(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProjectRegistrationError("registration_policy_invalid")
        result[key] = value
    return result
