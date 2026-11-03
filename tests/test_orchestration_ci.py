import importlib.util
import json
import subprocess
import sys
import types
import zipfile
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "scripts" / (name + ".py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "failure", [None, "junit_skip", "brain", "final_head", "final_dirty"]
)
def test_ci_receipt_requires_subgates_and_final_exact_clean_source(
    tmp_path, monkeypatch, failure
):
    module = load("orchestration_ci")
    root = tmp_path / "source"
    root.mkdir()
    module.ROOT = root
    output = tmp_path / "evidence"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "ci",
            "--expect-head",
            "a" * 40,
            "--output",
            str(output),
            "--run-id",
            "synthetic",
            "--run-attempt",
            "1",
        ],
    )
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        stdout = b""
        if argv[:3] == ["git", "rev-parse", "HEAD"]:
            stdout = (
                ("b" if failure == "final_head" and len(calls) > 5 else "a") * 40 + "\n"
            ).encode()
        elif argv[:3] == ["git", "status", "--porcelain"]:
            stdout = (
                b" M source.py\n"
                if failure == "final_dirty" and len(calls) > 5
                else b""
            )
        elif "pytest" in argv:
            assert "tests/test_project_registration.py" in argv
            assert "tests/test_dispatch_health.py" in argv
            assert "tests/test_task_panel.py" in argv
            junit = next(
                a.split("=", 1)[1] for a in argv if a.startswith("--junitxml=")
            )
            Path(junit).write_text(
                '<testsuite tests="5" failures="0" errors="0" skipped="'
                + ("1" if failure == "junit_skip" else "0")
                + '"/>'
            )
        elif "scripts/second_brain_release_gate.py" in argv:
            Path(argv[argv.index("--output") + 1]).write_text(
                json.dumps(
                    dict(
                        synthetic_release_gate="failed"
                        if failure == "brain"
                        else "passed",
                        technically_eligible_for_next_phase=failure != "brain",
                        plan_only=False,
                        head="a" * 40,
                        head_after="a" * 40,
                        head_stable=True,
                    )
                )
            )
        elif "scripts/verify_distribution.py" in argv:
            dest = Path(argv[argv.index("--output") + 1])
            dest.mkdir()
            (dest / "receipt.json").write_text(
                json.dumps(
                    dict(state="passed", source_commit="a" * 40, artifacts=[{}, {}])
                )
            )
        return types.SimpleNamespace(returncode=0, stdout=stdout)

    monkeypatch.setattr(subprocess, "run", run)
    if failure:
        with pytest.raises((ValueError, RuntimeError)):
            module.main()
    else:
        assert module.main() == 0
    receipt = json.loads((output / "receipt.json").read_text())
    assert receipt["state"] == ("failed" if failure else "passed")
    assert receipt["run_id"] == "synthetic" and receipt["run_attempt"] == 1
    assert receipt["checks"]
    if failure in {None, "final_head", "final_dirty"}:
        assert any("scripts/verify_distribution.py" in argv for argv in calls)
        assert calls[-1][:2] == ["git", "status"] or failure == "final_head"


def valid_wheel(module, path, changes=None):
    files = {"oraculo-0.1.0.dist-info/METADATA": b"Name: oraculo\nVersion: 0.1.0\n\n"}
    files.update(
        {
            "oraculo/" + m + ".py": (ROOT / "src/oraculo" / (m + ".py")).read_bytes()
            for m in module.REQUIRED_MODULES
        }
    )
    files.update(
        {
            "oraculo/assets/" + r: (ROOT / r).read_bytes()
            for r in module.REQUIRED_RESOURCES
        }
    )
    files.update(changes or {})
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)


@pytest.mark.parametrize(
    "mutation", ["version", "path", "private_path", "source", "resource"]
)
def test_distribution_rejects_mismatch_missing_resource_and_generic_private_paths(
    tmp_path, mutation
):
    module = load("verify_distribution")
    wheel = tmp_path / "oraculo-0.1.0-py3-none-any.whl"
    valid_wheel(module, wheel)
    assert module.inspect_archive(wheel, "0.1.0")["version"] == "0.1.0"
    changes = {
        "version": {
            "oraculo-0.1.0.dist-info/METADATA": b"Name: oraculo\nVersion: 0.2.0\n"
        },
        "path": {"../private": b"data"},
        "private_path": {
            "private-note": b"/Users/synthetic-person/private/receipt.json"
        },
        "source": {"oraculo/decisions.py": b"different source"},
        "resource": {},
    }[mutation]
    valid_wheel(module, wheel, changes)
    if mutation == "resource":
        with zipfile.ZipFile(wheel) as archive:
            files = {
                n: archive.read(n)
                for n in archive.namelist()
                if n != "oraculo/assets/" + module.REQUIRED_RESOURCES[0]
            }
        with zipfile.ZipFile(wheel, "w") as archive:
            for name, data in files.items():
                archive.writestr(name, data)
    with pytest.raises(ValueError):
        module.inspect_archive(wheel, "0.1.0")


def test_inactive_ci_template_pins_actions_and_preserves_always_evidence():
    path = ROOT / "templates/ci/oraculo-ci.yml.template"
    spec = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
    steps = spec["jobs"]["offline"]["steps"]
    assert spec["permissions"] == {"contents": "read"}
    for step in steps:
        if "uses" in step:
            import re

            assert re.fullmatch(r"[^@]+@[a-f0-9]{40}", step["uses"])
    upload = steps[-1]
    assert upload["if"] == "always()" and upload["with"]["if-no-files-found"] == "error"
    assert upload["with"]["retention-days"] == "7"
    assert "steps.proof.outputs.path" in upload["with"]["path"]
    assert spec["jobs"]["offline"]["name"] == "Orchestration offline gate"
    assert spec["jobs"]["offline"]["timeout-minutes"] == "30"
    full = yaml.load(
        (ROOT / "templates/ci/full-ci.yml.template").read_text(), Loader=yaml.BaseLoader
    )
    job = full["jobs"]["tests"]
    assert job["name"] == "Tests (${{ matrix.os }})"
    assert job["strategy"]["matrix"]["os"] == ["ubuntu-latest", "macos-latest"]
    assert job["timeout-minutes"] == "30"
    assert any(
        "--durations=25" in step.get("run", "") and "--junitxml=" in step.get("run", "")
        for step in job["steps"]
    )


def test_uv_output_marker_is_metadata_and_unexpected_extra_files_still_fail(tmp_path):
    module = load("verify_distribution")
    (tmp_path / "oraculo-0.1.0.whl").write_bytes(b"wheel")
    (tmp_path / "oraculo-0.1.0.tar.gz").write_bytes(b"sdist")
    (tmp_path / ".gitignore").write_bytes(b"*")
    assert len(module.distribution_paths(tmp_path)) == 2
    (tmp_path / "private-note").write_bytes(b"unexpected")
    with pytest.raises(ValueError):
        module.distribution_paths(tmp_path)
    (tmp_path / "private-note").unlink()
    (tmp_path / ".gitignore").write_bytes(b"unexpected private content")
    with pytest.raises(ValueError):
        module.distribution_paths(tmp_path)
