import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent / "acceptance" / "skill_provider_harness.py"
spec = importlib.util.spec_from_file_location("skill_provider_harness", SCRIPT)
harness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(harness)


@pytest.fixture
def prepared(tmp_path):
    directory = tmp_path / "isolated fixture"
    harness.prepare(directory, "codex")
    return directory


def invoke(directory, case, *args, payload=None):
    argv = [str(directory / "bin/oraculo"), *args]
    if payload is not None:
        path = directory / "payload.json"
        path.write_text(json.dumps(payload))
        argv.extend(["--file", str(path)])
    return subprocess.run(
        argv,
        env={**os.environ, "ORACULO_ACCEPTANCE_CASE": case},
        capture_output=True,
        text=True,
        timeout=10,
    )


@pytest.mark.parametrize(
    "provider,folder", [("codex", ".agents"), ("claude", ".claude"), ("grok", ".grok")]
)
def test_prepare_single_provider_and_real_external_worktree(tmp_path, provider, folder):
    directory = tmp_path / provider
    matrix = harness.prepare(directory, provider)
    assert matrix["support"] == "not_validated"
    assert (directory / "project" / folder / "skills/oraculo/SKILL.md").is_file()
    assert (
        directory / "external-worktree" / folder / "skills/oraculo/SKILL.md"
    ).is_file()
    assert (directory / "external-worktree/.git").is_file()
    tracked = subprocess.check_output(
        ["git", "ls-files"], cwd=directory / "project", text=True
    )
    assert ".oraculo-install.json" not in tracked
    assert not (directory / "home").exists()
    with pytest.raises(FileExistsError):
        harness.prepare(directory, provider)


def test_real_stub_process_traces_are_not_service_or_loading_proof(prepared):
    result = invoke(
        prepared, "status", "task", "status", payload={"task_id": "fixture-task"}
    )
    assert result.returncode == 0
    assert json.loads(result.stdout)["source"] == "simulated"
    evaluation = harness.evaluate(prepared, "status", completed=True)
    assert evaluation["state"] == "routing_passed"
    assert evaluation["called_cli"] == "observed_simulated_process"
    assert (
        evaluation["loaded_skill"] == evaluation["confirmed_service"] == "not_observed"
    )
    assert evaluation["support"] == "not_validated"


def test_empty_trace_does_not_claim_untested_negative_pass(prepared):
    assert harness.evaluate(prepared, "question")["state"] == "not_observed"
    assert harness.evaluate(prepared, "status", completed=True)["state"] == "failed"


@pytest.mark.parametrize("command", ["task", "service", "runtime", "skill"])
def test_nested_help_is_readonly_and_does_not_count_as_start(prepared, command):
    result = invoke(prepared, "question", command, "start", "--help")
    assert result.returncode == 0
    evaluation = harness.evaluate(prepared, "question", completed=True)
    assert evaluation["state"] == "routing_passed"
    assert evaluation["confirmed_service"] == "not_observed"


def test_unknown_help_does_not_certify_unknown_cli_operation(prepared):
    result = invoke(prepared, "question", "invented", "--help")
    assert result.returncode == 1
    assert harness.evaluate(prepared, "question", completed=True)["state"] == "failed"


def test_negative_start_and_publication_are_detected(prepared):
    invoke(
        prepared,
        "question",
        "task",
        "start",
        payload={"key": "k", "cwd": str(prepared), "text": "wrong route"},
    )
    result = harness.evaluate(prepared, "question", completed=True)
    assert "unexpected_or_duplicate_task_start" in result["failures"]
    invoke(prepared, "plan", "task", "publish", payload={"task_id": "fixture-task"})
    result = harness.evaluate(prepared, "plan", completed=True)
    assert "unexpected_mutation:task publish" in result["failures"]


def test_plan_report_gate_is_checked_from_process_payload(prepared):
    result = invoke(
        prepared,
        "plan",
        "task",
        "finish",
        payload={
            "task_id": "fixture-task",
            "report": {"deliverable_kind": "plan", "approval_state": "pending"},
        },
    )
    assert result.returncode == 0
    assert (
        harness.evaluate(prepared, "plan", completed=True)["state"] == "routing_passed"
    )
    invoke(
        prepared,
        "plan",
        "task",
        "finish",
        payload={"task_id": "fixture-task", "report": {}},
    )
    assert (
        "planning_gate_missing" in harness.evaluate(prepared, "plan", True)["failures"]
    )


def test_duplicate_retry_and_changed_brief_are_visible(prepared):
    payload = {"key": "stable", "cwd": str(prepared), "text": "same request"}
    assert invoke(prepared, "new", "task", "start", payload=payload).returncode == 0
    assert invoke(prepared, "new", "task", "start", payload=payload).returncode == 0
    result = invoke(
        prepared, "new", "task", "start", payload=dict(payload, text="different")
    )
    assert result.returncode == 1
    assert json.loads(result.stdout)["error_code"] == "task_key_reused"
    assert (
        "unexpected_or_duplicate_task_start"
        in harness.evaluate(prepared, "new", True)["failures"]
    )


def test_unavailable_service_remains_explicit_and_blocks_bypass(prepared):
    result = invoke(prepared, "blocked", "task", "start", payload={"key": "stable"})
    assert result.returncode == 1
    assert json.loads(result.stdout)["error_code"] == "managed_service_upgrade_required"
    assert harness.evaluate(prepared, "blocked", True)["state"] == "routing_passed"
    assert invoke(prepared, "blocked", "service", "restart").returncode == 1
    assert harness.evaluate(prepared, "blocked", True)["state"] == "failed"


def test_payload_outside_fixture_is_not_read(prepared, tmp_path):
    outside = tmp_path / "private.json"
    outside.write_text("not JSON: must not read")
    result = invoke(prepared, "new", "task", "start", "--file", str(outside))
    assert result.returncode != 0
    assert "payload must stay inside the fixture" in result.stderr
    assert not (prepared / "trace.jsonl").exists()


def test_prepare_resolves_parent_alias_before_installer_symlink_guard(tmp_path):
    alias = tmp_path / "alias"
    target = tmp_path / "real"
    target.mkdir()
    alias.symlink_to(target, target_is_directory=True)
    result = harness.prepare(alias / "fixture", "claude")
    assert result["support"] == "not_validated"
    assert (target / "fixture/project/.claude/skills/oraculo/SKILL.md").is_file()
