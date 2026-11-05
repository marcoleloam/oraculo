"""E8: the install bootstrap must not import modules planted in the working directory."""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_e8_install_cli_ignores_planted_cwd_json(tmp_path):
    planted = tmp_path / "json.py"
    planted.write_text("raise SystemExit('planted_json')\n")
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    result = subprocess.run(
        ["sh", str(ROOT / "scripts" / "install-cli.sh"), str(bundle)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env={**os.environ, "ORACULO_PYTHON": sys.executable},
    )
    output = result.stdout + result.stderr
    assert "planted_json" not in output
    assert result.returncode != 0
