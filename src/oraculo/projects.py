"""Resolve task ownership from allowed paths and Git metadata, never T3 labels."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

from .config import Config


class ProjectResolutionError(ValueError):
    def __init__(self, reason: str, candidates: list[dict[str, Any]] | None = None):
        self.reason = reason
        self.candidates = candidates or []
        super().__init__(reason)


def _path(value: str | Path) -> Path:
    try:
        return Path(value).expanduser().resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ProjectResolutionError("path_unavailable") from exc


def _git(path: Path, *args: str) -> str | None:
    # Ambient Git overrides must not turn a path check into another repository.
    env = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, LC_ALL="C")
    try:
        result = subprocess.run(
            ["git", "-C", str(path), *args],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProjectResolutionError("git_metadata_unavailable") from exc
    if result.returncode:
        # A plain directory is valid project metadata. Other failures (permissions,
        # unsafe ownership, corrupt repositories) must not masquerade as non-Git.
        if "not a git repository" in result.stderr:
            return None
        raise ProjectResolutionError("git_metadata_unavailable")
    return result.stdout


def _repository(path: Path) -> tuple[Path, Path | None]:
    common = _git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if common is None:
        return path, None
    common_path = _path(common.strip())
    listing = _git(path, "worktree", "list", "--porcelain", "-z")
    if not listing or not listing.startswith("worktree "):
        raise ProjectResolutionError("git_workspace_unavailable")
    # Git lists the main checkout first; -z retains whitespace in filesystem paths.
    main = _path(listing.split("\0", 1)[0][len("worktree ") :])
    if not main.is_dir() or not (main / ".git").exists():
        raise ProjectResolutionError("git_workspace_unavailable")
    return main, common_path


def resolve_project(
    config: Config,
    *,
    cwd: str | Path,
    projects: list[dict[str, Any]],
    explicit: str | None = None,
) -> dict[str, Any]:
    """Resolve a registered, allowed project or report a launch-blocking reason.

    ``projects`` is the T3 project-list snapshot (id/workspaceRoot). ``repoPath``
    is accepted for adapters retaining the older path spelling. Explicit intent
    accepts a project id, an unambiguous directory name, or an existing path;
    titles and remote repository names are not ownership evidence. No project
    files or directory listings are read, and no network requests are made.
    """
    source = _path(cwd)
    if not source.is_dir():
        raise ProjectResolutionError("cwd_not_directory")
    records: list[dict[str, Any]] = []
    failures = False
    seen: set[tuple[str, str]] = set()
    for project in projects:
        if project.get("deletedAt"):
            continue
        project_id = project.get("id")
        raw_path = project.get("workspaceRoot") or project.get("repoPath")
        if (
            not isinstance(project_id, str)
            or not project_id
            or not isinstance(raw_path, str)
        ):
            continue
        try:
            registered = _path(raw_path)
            if not registered.is_dir():
                continue
            canonical, common = _repository(registered)
        except ProjectResolutionError:
            failures = True
            continue
        if not config.project_in_scope(canonical):
            continue
        key = (project_id, str(canonical))
        if key in seen:
            for record in records:
                if (record["project_id"], record["project_path"]) == key:
                    record["aliases"].update({Path(raw_path).name, registered.name})
            continue
        seen.add(key)
        records.append(
            {
                "project_id": project_id,
                "project_path": str(canonical),
                "repository_common_dir": str(common) if common else None,
                "registered_path": str(registered),
                "aliases": {Path(raw_path).name, registered.name, canonical.name},
            }
        )

    def summary(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {"project_id": item["project_id"], "project_path": item["project_path"]}
            for item in items
        ]

    selector = explicit.strip() if explicit is not None else None
    if explicit is not None and not selector:
        raise ProjectResolutionError("empty_project_intent", summary(records))
    if selector is not None:
        selected = [
            item
            for item in records
            if selector == item["project_id"] or selector in item["aliases"]
        ]
        # Paths are relative to the supplied source, never the daemon's cwd.
        requested = Path(selector).expanduser()
        paths = [requested] if requested.is_absolute() else [source / requested]
        if not requested.is_absolute():
            paths.append(config.projects_root / requested)
        for requested_path in paths:
            if not requested_path.exists():
                continue
            target = _path(requested_path)
            if not target.is_dir():
                raise ProjectResolutionError("project_not_directory")
            target_root, target_common = _repository(target)
            selected += [
                item
                for item in records
                if (
                    target == Path(item["registered_path"])
                    or target_root == Path(item["project_path"])
                    or (
                        target_common is not None
                        and str(target_common) == item["repository_common_dir"]
                    )
                )
                and item not in selected
            ]
        reason = "explicit_project_unavailable_or_out_of_scope"
        method = "explicit_intent"
    else:
        source_root, source_common = _repository(source)
        selected = [
            item
            for item in records
            if (
                (
                    source_common is not None
                    and str(source_common) == item["repository_common_dir"]
                )
                or (
                    source_common is None
                    and (
                        source_root == Path(item["project_path"])
                        or source_root.is_relative_to(Path(item["project_path"]))
                    )
                )
            )
        ]
        reason = "cwd_unregistered_or_out_of_scope"
        method = "cwd_git_identity" if source_common else "cwd_path"
    if not selected:
        raise ProjectResolutionError(
            "project_metadata_unavailable" if failures and not records else reason,
            summary(records),
        )
    if len(selected) != 1:
        raise ProjectResolutionError("ambiguous_project", summary(selected))
    result = selected[0]
    return {
        key: result[key]
        for key in ("project_id", "project_path", "repository_common_dir")
    } | {
        "source_cwd": str(source),
        "evidence": {
            "method": method,
            "registered_path": result["registered_path"],
            "projects_root": str(config.projects_root),
            "scope_checked": True,
            "explicit_intent": selector,
        },
    }
