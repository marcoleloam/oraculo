#!/usr/bin/env python3
"""Build and inspect exact-source distributions, then install in a disposable venv.

No daemon, vault, service, API or credential access. Run only on a clean candidate.
The receipt binds package metadata and artifact hashes to Git source identity.
"""

from __future__ import annotations

import argparse
import email.parser
import hashlib
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Compare the complete shipped source/resource inventory, not only sample files.
REQUIRED_MODULES = tuple(
    path.relative_to(ROOT / "src/oraculo").with_suffix("").as_posix()
    for path in sorted((ROOT / "src/oraculo").rglob("*.py"))
)
RESOURCE_TREES = (
    "prompts",
    "templates/vault",
    "templates/integration",
    "skills/oraculo",
)
REQUIRED_RESOURCES = (
    "config/oraculo.example.toml",
    *(
        path.relative_to(ROOT).as_posix()
        for tree in RESOURCE_TREES
        for path in sorted((ROOT / tree).rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts
    ),
)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def inspect_archive(path: Path, version: str):
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            files = {
                n: archive.read(n) for n in archive.namelist() if not n.endswith("/")
            }
        metadata = next(
            v for k, v in files.items() if k.endswith(".dist-info/METADATA")
        )
        expected = ["oraculo/" + m + ".py" for m in REQUIRED_MODULES]
        resources = ["oraculo/assets/" + r for r in REQUIRED_RESOURCES]
    else:
        with tarfile.open(path) as archive:
            files = {}
            for m in archive.getmembers():
                if not m.isfile():
                    if m.issym() or m.islnk():
                        raise ValueError("distribution links forbidden")
                    continue
                name = "/".join(Path(m.name).parts[1:])
                files[name] = archive.extractfile(m).read()
        metadata = files["PKG-INFO"]
        expected = ["src/oraculo/" + m + ".py" for m in REQUIRED_MODULES]
        resources = list(REQUIRED_RESOURCES)
    for name, data in files.items():
        p = Path(name)
        if (
            p.is_absolute()
            or ".." in p.parts
            or any(
                part.startswith(".agent")
                or part in {".git", ".env", "__pycache__", "managed-contexts"}
                for part in p.parts
            )
            or p.suffix in {".sqlite3", ".pyc"}
        ):
            raise ValueError("private or unsafe distribution entry")
        if (
            re.search(rb"/(?:Users|home)/[A-Za-z0-9_.-]+/", data)
            or b"Bearer sk-" in data
        ):
            raise ValueError("private distribution content")
    parsed = email.parser.BytesParser().parsebytes(metadata)
    if parsed["Name"] != "oraculo" or parsed["Version"] != version:
        raise ValueError("distribution metadata mismatch")
    for name in expected + resources:
        if name not in files:
            raise ValueError("distribution resource missing: " + name)
    for resource, archived in zip(REQUIRED_RESOURCES, resources, strict=True):
        if files[archived] != (ROOT / resource).read_bytes():
            raise ValueError("distribution resource mismatch")
    for module in REQUIRED_MODULES:
        name = (
            ("oraculo/" if path.suffix == ".whl" else "src/oraculo/") + module + ".py"
        )
        if files[name] != (ROOT / "src/oraculo" / (module + ".py")).read_bytes():
            raise ValueError("distribution source mismatch")
    return dict(
        name=path.name,
        sha256=digest(path.read_bytes()),
        files=len(files),
        version=version,
    )


def distribution_paths(directory):
    paths = []
    for path in sorted(directory.iterdir()):
        if path.name == ".gitignore" and path.read_bytes() in {b"*", b"*\n"}:
            continue  # uv's own output-directory marker, never a distribution.
        paths.append(path)
    if (
        len(paths) != 2
        or len([p for p in paths if p.suffix == ".whl"]) != 1
        or len([p for p in paths if p.name.endswith(".tar.gz")]) != 1
    ):
        raise ValueError("wheel and sdist required")
    return paths


def run(argv, cwd, logdir, records, *, label):
    environment = dict(
        os.environ,
        PYTHONDONTWRITEBYTECODE="1",
        UV_PYTHON_DOWNLOADS="never",
        PYTHONNOUSERSITE="1",
    )
    environment.pop("PYTHONPATH", None)
    environment.pop("PYTHONHOME", None)
    result = subprocess.run(
        argv,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=environment,
        timeout=300,
    )
    log = logdir / (label + ".log")
    log.write_bytes(result.stdout)
    records.append(
        dict(
            command=argv,
            exit=result.returncode,
            log=log.name,
            log_sha256=digest(result.stdout),
        )
    )
    if result.returncode:
        raise RuntimeError("distribution check failed: " + label)
    return result.stdout.decode()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expect-head", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    if output == ROOT or output.is_relative_to(ROOT):
        parser.error("output must be disposable and outside the source")
    output.mkdir(parents=True, exist_ok=False)
    records = []
    receipt = dict(
        state="failed",
        source_commit=args.expect_head,
        checks=records,
        activation="not_requested",
    )
    try:
        head = run(
            ["git", "rev-parse", "HEAD"], ROOT, output, records, label="head"
        ).strip()
        dirty = run(
            ["git", "status", "--porcelain"], ROOT, output, records, label="clean"
        )
        if head != args.expect_head or dirty:
            raise ValueError("clean exact source required")
        version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"][
            "version"
        ]
        artifacts = output / "dist"
        run(
            ["uv", "build", "--out-dir", str(artifacts)],
            ROOT,
            output,
            records,
            label="build",
        )
        paths = distribution_paths(artifacts)
        receipt["artifacts"] = [inspect_archive(p, version) for p in paths]
        requirements = output / "requirements.txt"
        run(
            [
                "uv",
                "export",
                "--frozen",
                "--no-dev",
                "--no-emit-project",
                "--format",
                "requirements-txt",
                "--output-file",
                str(requirements),
            ],
            ROOT,
            output,
            records,
            label="export",
        )
        with tempfile.TemporaryDirectory(prefix="oraculo-installed-") as temp:
            cwd = Path(temp)
            run(
                ["uv", "venv", "--python", sys.executable, str(cwd / "venv")],
                cwd,
                output,
                records,
                label="venv",
            )
            python = cwd / "venv/bin/python"
            run(
                [
                    "uv",
                    "pip",
                    "install",
                    "--python",
                    str(python),
                    "--require-hashes",
                    "-r",
                    str(requirements),
                ],
                cwd,
                output,
                records,
                label="dependencies",
            )
            wheel = next(artifacts.glob("*.whl"))
            run(
                [
                    "uv",
                    "pip",
                    "install",
                    "--python",
                    str(python),
                    "--no-deps",
                    str(wheel),
                ],
                cwd,
                output,
                records,
                label="install",
            )
            probe = (
                "import importlib.metadata,importlib.resources; from oraculo import decisions,ci_events,outcomes,rollout,orchestration_service,deliverables,project_registration,task_panel; assert importlib.metadata.version('oraculo')=="
                + repr(version)
                + "; from oraculo import __version__; assert __version__=="
                + repr(version)
                + "; assert all(importlib.resources.files('oraculo').joinpath('assets', r).is_file() for r in "
                + repr(REQUIRED_RESOURCES)
                + ")"
            )
            run(
                [str(python), "-B", "-c", probe],
                cwd,
                output,
                records,
                label="installed-imports",
            )
            run(
                [str(cwd / "venv/bin/oraculo"), "--help"],
                cwd,
                output,
                records,
                label="installed-help",
            )
        final_head = run(
            ["git", "rev-parse", "HEAD"], ROOT, output, records, label="head-after"
        ).strip()
        final_dirty = run(
            ["git", "status", "--porcelain"], ROOT, output, records, label="clean-after"
        )
        if final_head != head or final_dirty:
            raise ValueError("source changed during distribution verification")
        if receipt["artifacts"] != [inspect_archive(p, version) for p in paths]:
            raise ValueError("distribution changed during install")
        receipt.update(state="passed", package_version=version)
    finally:
        (output / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
