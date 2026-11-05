"""Replace an owned reading export while retaining all local-edit safeguards."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

from oraculo.experience import write_export
from oraculo.memory.schema import digest, safe_path
from oraculo.protocol import canonical_json


def _generated_canvas_unchanged(path: str, content: bytes, expected: str) -> bool:
    """Allow Obsidian's JSON formatting only when the original hash is recovered."""
    if path != "Mapa dos projetos.canvas":
        return False
    try:
        document = json.loads(content)
    except (ValueError, UnicodeDecodeError):
        return False
    # The exporter writes this exact serialization. Any changed node, edge,
    # geometry, list order or added property must still fail the ownership guard.
    return digest(canonical_json(document) + "\n") == expected


def _local_configuration(path: str) -> bool:
    parts = Path(path).parts
    return len(parts) == 2 and parts[0] == ".obsidian" and path.endswith(".json")


def _unchanged(destination: Path, incoming: dict[str, str]) -> dict[str, str]:
    """Return local JSON that must survive this refresh.

    A generated graph or workspace that still matches the previous manifest is
    replaced, so project colors can follow the new set. Edited bytes, and any
    JSON the package does not generate, stay byte for byte.
    """
    marker = destination / ".oraculo-export.json"
    if destination.is_symlink():
        raise ValueError("experience_symlink_destination")
    paths = list(destination.rglob("*"))
    if any(path.is_symlink() for path in paths):
        raise ValueError("experience_symlink_destination")
    if not marker.is_file():
        raise ValueError("experience_requires_empty_or_owned_export")
    previous = json.loads(marker.read_text())
    if not isinstance(previous, dict):
        raise ValueError("experience_invalid_export_manifest")
    current = {
        path.relative_to(destination).as_posix()
        for path in paths
        if path.is_file() and path != marker
    }
    if {p for p in current if not _local_configuration(p)} != {
        p for p in previous if not _local_configuration(p)
    }:
        raise ValueError("experience_export_locally_modified")
    for path, expected in previous.items():
        if _local_configuration(path):
            continue
        content = safe_path(destination, path).read_bytes()
        if digest(content) != expected and not _generated_canvas_unchanged(
            path, content, expected
        ):
            raise ValueError("experience_export_locally_modified")
    preserved = {}
    for path in sorted(current):
        if not _local_configuration(path):
            continue
        text = safe_path(destination, path).read_text(encoding="utf-8")
        prior = previous.get(path)
        if path not in incoming or prior is None or digest(text) != prior:
            preserved[path] = text
    return preserved


def _pin_generated_configuration(
    stage: Path, files: dict[str, str], receipt: dict
) -> None:
    """Remember the generated digest, not the preserved user bytes.

    The next refresh compares disk bytes with that generated digest. An edit
    therefore stays preserved on every later refresh.
    """

    marker = stage / ".oraculo-export.json"
    manifest = json.loads(marker.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError("experience_invalid_export_manifest")
    for path, content in files.items():
        if _local_configuration(path) and path in manifest:
            manifest[path] = digest(content)
    marker.write_text(canonical_json(manifest) + "\n", encoding="utf-8")
    marker.chmod(0o600)
    receipt["manifest_sha256"] = digest(canonical_json(manifest))


def refresh_export(destination: Path, files: dict[str, str]) -> dict:
    """Stage a complete replacement; refuse unknown files or modified originals.

    An interrupted replacement leaves its private sibling backup available for
    recovery. This function never reads or writes the canonical Oraculo vault.
    """
    destination = destination.absolute()
    if destination.is_symlink():
        raise ValueError("experience_symlink_destination")
    destination = destination.parent.resolve() / destination.name
    if not destination.exists() or not any(destination.iterdir()):
        return write_export(destination, files)
    configuration = _unchanged(destination, files)
    stage = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}.staging-", dir=destination.parent)
    )
    backup = None
    installed = False
    try:
        receipt = write_export(stage, files | configuration)
        _pin_generated_configuration(stage, files, receipt)
        if _unchanged(destination, files) != configuration:
            raise ValueError("experience_export_locally_modified")
        backup = Path(
            tempfile.mkdtemp(
                prefix=f".{destination.name}.previous-", dir=destination.parent
            )
        )
        backup.rmdir()
        os.replace(destination, backup)
        try:
            os.replace(stage, destination)
        except BaseException:
            os.replace(backup, destination)
            backup = None
            raise
        receipt["path"] = str(destination)
        receipt["state"] = "refreshed"
        installed = True
        return receipt
    finally:
        if stage.exists():
            shutil.rmtree(stage)
        if installed and backup is not None and backup.exists():
            shutil.rmtree(backup)
