"""E3: the AT-020 battery must keep collecting after a test rename."""

import importlib.util
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def cases():
    path = ROOT / "scripts" / "test_at020.py"
    spec = importlib.util.spec_from_file_location("test_at020_script", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.CASES


def test_e3_at020_selectors_collect():
    selected = cases()
    assert (
        "tests/test_service.py::test_submit_dispatch_execute_reconcile_publishes_cycle"
        in selected
    )
    assert not any("queues_memory" in item for item in selected)
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", *selected],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
