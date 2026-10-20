from __future__ import annotations

import subprocess
from pathlib import Path

from .schema import MemoryError, safe_path


class VaultGit:
    def __init__(self, vault: Path) -> None:
        self.vault = Path(vault)
        self.timeout: float | None = None

    def run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(self.vault), *args],
            capture_output=True,
            text=True,
            check=check,
            timeout=self.timeout,
        )

    def initialize(self) -> None:
        self.vault.mkdir(parents=True, exist_ok=True)
        if not (self.vault / ".git").exists():
            self.run("init", "--quiet")
        if self.run("rev-parse", "--show-toplevel").stdout.strip() != str(
            self.vault.resolve()
        ):
            raise MemoryError("vault_must_be_git_root")

    def head(self) -> str | None:
        result = self.run("rev-parse", "--verify", "HEAD", check=False)
        return result.stdout.strip() if result.returncode == 0 else None

    def commit(self, paths: list[str], operation_id: str) -> str:
        if not paths:
            return self.head() or ""
        for path in paths:
            safe_path(self.vault, path)
        self.run("add", "--", *paths)
        self.run(
            "-c",
            "user.name=Oraculo",
            "-c",
            "user.email=oraculo@localhost",
            "commit",
            "--quiet",
            "--only",
            "--allow-empty",
            "-m",
            f"Oraculo memory\n\nOperation: {operation_id}",
            "--",
            *paths,
        )
        return self.head() or ""

    def operation_commit(self, operation_id: str) -> str | None:
        result = self.run("log", "--format=%H%x00%B%x00END", check=False)
        for entry in result.stdout.split("\x00END"):
            pieces = entry.strip().split("\x00", 1)
            if (
                len(pieces) == 2
                and f"Operation: {operation_id}" in pieces[1].splitlines()
            ):
                return pieces[0]
        return None

    def restore_copy(self, commit: str, destination: Path) -> None:
        if destination.exists():
            raise MemoryError("restore_destination_exists")
        resolved = self.run(
            "rev-parse", "--verify", f"{commit}^{{commit}}"
        ).stdout.strip()
        files = self.run("ls-tree", "-r", "--name-only", resolved).stdout.splitlines()
        destination.mkdir(parents=True)
        for relative in files:
            target = safe_path(destination, relative)
            data = subprocess.run(
                ["git", "-C", str(self.vault), "show", f"{resolved}:{relative}"],
                capture_output=True,
                check=True,
            ).stdout
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
