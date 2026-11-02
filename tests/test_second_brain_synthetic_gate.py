"""Synthetic release gate. Required cases do not skip.

The 30-day clock is an injected virtual clock. accepted_simulation stays
distinct from a real pilot acceptance.
"""

import hashlib
import importlib.util
import json
import os
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from test_memory_schema import page, proposal

from oraculo.cli import make_envelope, request
from oraculo.knowledge import save_answer, validate_nightly_proposal
from oraculo.knowledge_service import KnowledgeCommands
from oraculo.memory.context import Context
from oraculo.memory.routines import KnowledgeRoutines
from oraculo.memory.schema import MemoryError, render_page
from oraculo.protocol import Envelope

FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "second_brain"
    / "synthetic_gate_lifecycle.json"
)
HARBOR = "atlas-harbor"
RIDGE = "atlas-ridge"
MAX_BYTES = 2 * 1024 * 1024


def load_gate():
    path = (
        Path(__file__).resolve().parents[1] / "scripts" / "second_brain_release_gate.py"
    )
    spec = importlib.util.spec_from_file_location("second_brain_release_gate", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_spec():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def vault_digest(service) -> str:
    rows = []
    root = service.publisher.vault
    for path in sorted(root.rglob("*")):
        if path.is_file():
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            rows.append(path.relative_to(root).as_posix() + ":" + digest)
    return hashlib.sha256("\n".join(rows).encode()).hexdigest()


def query(service, project_id, text, spec):
    envelope = Envelope(
        schema_version=1,
        request_id="synthetic-gate-"
        + project_id
        + "-"
        + hashlib.sha256(text.encode()).hexdigest()[:12],
        kind="knowledge.query",
        project_id=project_id,
        origin={
            "thread_id": "synthetic-gate-fixture",
            "run_id": "synthetic-gate-fixture",
        },
        payload={
            "query": text,
            "limit": spec["query_limit"],
            "detail_tokens": spec["detail_tokens"],
            "brief_tokens": spec["brief_tokens"],
        },
    )
    return KnowledgeCommands(service).command(envelope)


def retrieval_report():
    return {
        "choice": {"constant_matches": True},
        "scales": [
            {
                "scale": scale,
                "acceptance_gate": {
                    "passed": True,
                    "candidate_recall_at_10": 0.9,
                    "language_regression": [],
                },
                "acceptance": {
                    "legacy_bundle": {"mrr": 0.09},
                    "budgeted": {"mrr": 0.1, "latency_ms_p95": 3.5},
                },
            }
            for scale in (50, 500, 5000)
        ],
    }


@pytest.mark.parametrize(
    "scales", [(50,), (50, 500), (50, 50, 5000), (50.0, 500, 5000)]
)
def test_release_gate_requires_all_frozen_scales(scales):
    gate = load_gate()
    report = retrieval_report()
    report["scales"] = [dict(report["scales"][0], scale=scale) for scale in scales]
    assert gate.evaluate_retrieval(report)["passed"] is False


@pytest.mark.parametrize("metric", ["recall", "baseline_mrr", "candidate_mrr", "p95"])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1.0])
def test_release_gate_rejects_invalid_measurements(metric, value):
    gate = load_gate()
    report = retrieval_report()
    item = report["scales"][0]
    if metric == "recall":
        item["acceptance_gate"]["candidate_recall_at_10"] = value
    elif metric == "p95":
        item["acceptance"]["budgeted"]["latency_ms_p95"] = value
    else:
        section = "legacy_bundle" if metric == "baseline_mrr" else "budgeted"
        item["acceptance"][section]["mrr"] = value
    assert gate.evaluate_retrieval(report)["passed"] is False


def test_release_gate_refuses_forged_pass_and_waiting(tmp_path):
    gate = load_gate()
    assert gate.command_status(1, False) == "failed"
    assert gate.command_status(0, True) == "failed"
    assert gate.command_status(0, False) == "passed"
    assert gate.evaluate_lifecycle_artifact(None)["passed"] is False

    skipped = tmp_path / "skipped.junit.xml"
    skipped.write_text(
        '<testsuite tests="2" failures="0" errors="0" skipped="1"></testsuite>',
        encoding="utf-8",
    )
    assert gate.classify_junit(skipped)["status"] == "failed"
    assert (
        gate.classify_junit(tmp_path / "missing.junit.xml")["reason"] == "junit_missing"
    )

    low = gate.evaluate_retrieval(
        {
            "choice": {"constant_matches": True},
            "scales": [
                {
                    "scale": 50,
                    "acceptance_gate": {
                        "passed": False,
                        "candidate_recall_at_10": 0.5,
                        "language_regression": ["en"],
                    },
                    "acceptance": {
                        "legacy_bundle": {"mrr": 0.09},
                        "budgeted": {"mrr": 0.01, "latency_ms_p95": 1.0},
                    },
                }
            ],
        }
    )
    assert low["passed"] is False
    assert low["semantic_accuracy_claimed"] is False
    assert low["production_sla_claimed"] is False

    measured = gate.evaluate_retrieval(
        {
            "choice": {"constant_matches": True},
            "manifest": {"seed": 20261101, "fixture_measurement": True},
            "scales": [
                {
                    "scale": scale,
                    "acceptance_gate": {
                        "passed": True,
                        "candidate_recall_at_10": 0.90,
                        "language_regression": [],
                    },
                    "acceptance": {
                        "legacy_bundle": {"mrr": 0.09},
                        "budgeted": {"mrr": 0.09, "latency_ms_p95": 3.5},
                    },
                }
                for scale in (50, 500, 5000)
            ],
        }
    )
    assert measured["passed"] is True
    assert measured["production_sla_claimed"] is False

    plan = gate.build_plan(tmp_path / "artifacts", "50,500,5000")
    joined = " ".join(" ".join(item["command"]) for item in plan)
    assert "sleep" not in joined
    assert "launchctl" not in joined
    assert all(item["skips_allowed"] is False for item in plan)
    output = tmp_path / "plan.json"
    assert gate.main(["--plan", "--output", str(output)]) == 0
    body = json.loads(output.read_text(encoding="utf-8"))
    assert body["plan_only"] is True
    assert body["mandatory_elapsed_wait"] is False
    assert body["technically_eligible_for_next_phase"] is False
    assert body["human_utility_accepted"] is False
    assert body["real_pilot_accepted"] is False
    assert body["runtime_activated"] is False
    assert body["activation_performed"] is False
    assert body["synthetic_release_gate"] == "not_executed"
    assert any(item["status"] == "not_executed" for item in body["checks"])
    assert body["post_activation_smoke"]["status"] == "not_executed"
    assert gate.main(["--plan", "--output", str(output)]) == 2
    assert json.loads(output.read_text(encoding="utf-8"))["plan_only"] is True

    failed = gate.assemble_receipt(
        head="abc",
        head_after="abc",
        plan_only=False,
        checks=[
            {
                "id": "synthetic_lifecycle",
                "phase": "preactivation_synthetic",
                "required": True,
                "status": "failed",
                "evaluation": {"passed": False},
            }
        ],
        scales="50",
    )
    assert failed["technically_eligible_for_next_phase"] is False
    assert failed["synthetic_release_gate"] == "failed"


async def test_virtual_thirty_day_lifecycle_keeps_isolation(running_service):
    spec = load_spec()
    assert spec["fixture_host_metadata"] is True
    assert spec["not_live_host"] is True
    assert spec["clock"] == "virtual_injected_clock"
    assert spec["virtual_days"] == 30
    assert spec["projects"] == [HARBOR, RIDGE]
    assert spec["homonym_display_name"] == "Atlas"
    service = running_service
    start = datetime.fromisoformat(spec["start"])
    now = [start]
    routine = KnowledgeRoutines(service, clock=lambda: now[0])
    assert routine.simulated_clock is True

    for project_id in (HARBOR, RIDGE):
        root = service.config.projects_root / project_id
        root.mkdir()
        service.publisher.initialize_project(project_id)
        service.state.register_project(project_id, str(root))
        service.state.set_coordinator(project_id, "coordinator")
    service.state.set_meta(
        "synthetic_gate:fixture_host_metadata",
        {
            "fixture_only": True,
            "not_live_host": True,
            "homonym_display_name": spec["homonym_display_name"],
            "project_ids": [HARBOR, RIDGE],
            "clock": spec["clock"],
        },
    )
    harbor_raw = service.publisher.snapshot(
        HARBOR, "Harbor fact HQ-17-CARMINE seed 20261101. Projeto Atlas."
    )
    ridge_raw = service.publisher.snapshot(
        RIDGE, "Ridge fact RQ-44-INDIGO seed 20261101. Projeto Atlas."
    )
    shared_raw = service.publisher.snapshot(
        "shared", "Sanitized shared note seed 20261101."
    )
    reviewed = page(
        HARBOR,
        name="reviewed-note",
        body="Reviewed harbor note HQ-17-CARMINE stays immutable.\n",
        reviewed=True,
    )
    service.publisher.publish(
        proposal(
            reviewed,
            project=HARBOR,
            name="reviewed-note",
            operation="synthetic-reviewed",
        )
    )
    reviewed_path = proposal(
        reviewed, project=HARBOR, name="reviewed-note", operation="synthetic-reviewed"
    )["changes"][0]["path"]
    reviewed_bytes = (service.publisher.vault / reviewed_path).read_bytes()
    for index in range(20):
        service.publisher.snapshot(HARBOR, f"pending-source-{index:02d} seed 20261101")
    huge = service.publisher.snapshot(HARBOR, "x" * (MAX_BYTES + 1))
    alpha_before = service.publisher.snapshot(
        "alpha", "alpha stays outside the homonyms"
    )
    canonical = vault_digest(service)
    head = service.publisher.git.head()

    assert routine.tick(HARBOR)["state"] == "disabled"
    assert routine.tick(RIDGE)["state"] == "disabled"
    assert routine.status(HARBOR)["enabled"] is False
    assert routine.status(HARBOR)["additional_spend_allowed"] is False
    assert not service.state.list_objectives()
    with pytest.raises(MemoryError, match="routine_configuration_invalid"):
        routine.configure(HARBOR, enabled=True, max_sources=21)
    assert routine.status(HARBOR)["enabled"] is False

    with pytest.raises(MemoryError, match="outside_scope"):
        routine.pilot_start(
            HARBOR, curator="curador-sintetico", source_manifest=[ridge_raw]
        )
    pilot = routine.pilot_start(
        HARBOR, curator="curador-sintetico", source_manifest=[harbor_raw]
    )
    assert pilot["simulated_clock"] is True
    assert pilot["routine_automatically_enabled"] is False
    assert routine.status(HARBOR)["enabled"] is False
    with pytest.raises(MemoryError, match="first_week"):
        routine.pilot_review(
            HARBOR,
            curator="curador-sintetico",
            outcome="useful",
            note="cedo demais",
        )

    before_query = service.publisher.git.head()
    harbor_hit = query(service, HARBOR, "HQ-17-CARMINE", spec)
    ridge_hit = query(service, RIDGE, "RQ-44-INDIGO", spec)
    assert harbor_hit["canonical_changed"] is False
    assert ridge_hit["canonical_changed"] is False
    assert service.publisher.git.head() == before_query
    harbor_blob = json.dumps(harbor_hit)
    ridge_blob = json.dumps(ridge_hit)
    assert "RQ-44-INDIGO" not in harbor_blob
    assert "projects/atlas-ridge/" not in harbor_blob
    assert "HQ-17-CARMINE" not in ridge_blob
    assert "projects/atlas-harbor/" not in ridge_blob
    for result in (harbor_hit, ridge_hit):
        selection = result["context"]["selection"]
        assert selection["budget"]["detail_tokens"] == spec["detail_tokens"]
        assert selection["budget"]["brief_tokens"] == spec["brief_tokens"]
        assert selection["tokens"]["detail"] <= spec["detail_tokens"]
        assert selection["tokens"]["brief"] <= spec["brief_tokens"]
        assert selection["pages_selected"] <= spec["query_limit"]
        assert all(
            len(document["content"].encode()) < MAX_BYTES
            for document in result["context"].get("documents", [])
        )
    with pytest.raises(MemoryError, match="knowledge_query_budget_invalid"):
        KnowledgeCommands(service).command(
            Envelope(
                schema_version=1,
                request_id="synthetic-gate-over-budget",
                kind="knowledge.query",
                project_id=HARBOR,
                origin={
                    "thread_id": "synthetic-gate-fixture",
                    "run_id": "synthetic-gate-fixture",
                },
                payload={"query": "HQ-17-CARMINE", "detail_tokens": 32001},
            )
        )
    harbor_files = Context(service.publisher.vault).files(HARBOR)
    assert shared_raw in harbor_files
    assert all(not path.startswith("projects/atlas-ridge/") for path in harbor_files)
    assert service.publisher.git.head() == before_query

    with pytest.raises(
        MemoryError, match="answer_sources_or_explicit_hypothesis_required"
    ):
        save_answer(
            service.publisher,
            HARBOR,
            title="fato solto",
            text="Um fato novo sem fonte.",
            evidence=[],
            apply=False,
        )
    hypothesis = save_answer(
        service.publisher,
        HARBOR,
        title="hipotese explicita",
        text="Isto e uma hipotese, nao um fato sustentado.",
        evidence=[],
        as_hypothesis=True,
        apply=False,
    )
    assert hypothesis["state"] == "proposal"
    claim = json.loads(json.dumps(hypothesis["proposal"]))["changes"][0]["content"]
    assert "support: not_evaluated" in claim or "not_evaluated" in claim
    assert "support: supported" not in claim
    selected, metrics = routine.pending_sources(HARBOR, spec["max_sources"])
    assert len(selected) == spec["max_sources"]
    assert metrics["selected_bytes"] <= spec["max_bytes"]
    assert huge not in selected
    assert all(path.startswith("projects/atlas-harbor/raw/") for path in selected)
    assert shared_raw not in selected
    with pytest.raises(MemoryError, match="selection_outside_scope"):
        validate_nightly_proposal(
            service.publisher, HARBOR, {"changes": []}, [ridge_raw]
        )
    supported = render_page(
        {
            "schema_version": 1,
            "knowledge_schema": 1,
            "page_id": "atlas-harbor--nightly-reject",
            "project_id": HARBOR,
            "type": "concept",
            "sources": [],
            "tags": [],
            "aliases": [],
            "reviewed": False,
            "created": "2026-11-02",
            "updated": "2026-11-02",
            "claims": [
                {
                    "id": "synthetic-supported",
                    "text": "fato sem avaliacao humana",
                    "kind": "fact",
                    "category": "inference",
                    "support": "supported",
                    "evidence": [{"path": selected[0]}],
                }
            ],
        },
        "corpo\n",
    )
    with pytest.raises(MemoryError, match="semantic_review_required"):
        validate_nightly_proposal(
            service.publisher,
            HARBOR,
            {"changes": [{"path": "unused.md", "content": supported}]},
            selected[:1],
        )
    assert service.publisher.git.head() == head
    assert vault_digest(service) == canonical

    routine.configure(
        HARBOR,
        enabled=True,
        timezone_name=spec["timezone"],
        nightly_hour=spec["nightly_hour"],
        weekly_weekday=spec["weekly_weekday"],
        max_sources=spec["max_sources"],
        max_pending_jobs=spec["max_pending_jobs"],
    )
    capacity = service.state.capacity()["available"]
    service.state.reserve_managed(
        "synthetic-gate-fixture-capacity", "codex", slots=capacity
    )
    blocked = routine.tick(HARBOR)
    assert blocked["state"] == "deferred" and blocked["reason"] == "capacity_zero"
    assert not service.state.list_objectives()
    service.state.release_managed(
        "synthetic-gate-fixture-capacity",
        {"workState": "result_available", "hasPendingChildRuns": False},
    )
    assert routine.tick(HARBOR)["state"] == "deferred"
    now[0] += timedelta(seconds=301)
    service.router.mark_quota("codex", now=now[0].timestamp())
    quota = routine.tick(HARBOR)
    assert quota["state"] == "deferred" and quota["reason"] == "quota_wait"
    assert not service.state.list_objectives()
    now[0] += timedelta(seconds=service.config.quota_backoff_seconds + 1)
    scheduled = routine.tick(HARBOR)
    assert scheduled["state"] == "scheduled", scheduled
    assert scheduled["new_executor_started"] is False
    assert [item["mode"] for item in scheduled["results"]] == ["nightly", "weekly"]
    nightly = scheduled["results"][0]
    replay = routine._enqueue(
        HARBOR, routine._record(HARBOR), "nightly", start.date().isoformat()
    )
    assert replay["receipt_replay"] is True
    assert replay["job_id"] == nightly["job_id"]
    assert len(service.state.list_objectives()) == 2
    for item in scheduled["results"]:
        service.state.cancel_objective(item["objective_id"])
    assert routine.tick(HARBOR)["state"] == "not_due"
    assert service.publisher.git.head() == head
    # The virtual quota timestamp can sit ahead of the process clock. Expire it
    # through the same router API so a later real grant is not stuck behind it.
    service.router.mark_quota("codex", now=0)

    modes = {"nightly": 1, "weekly": 1}
    deferrals = ["capacity_zero", "quota_wait"]
    for day in range(1, spec["virtual_days"]):
        now[0] = start + timedelta(days=day)
        assert routine.tick(RIDGE)["state"] == "disabled"
        result = routine.tick(HARBOR)
        assert result["state"] == "scheduled", (day, result)
        assert result["new_executor_started"] is False
        for item in result["results"]:
            modes[item["mode"]] = modes.get(item["mode"], 0) + 1
            payload = json.loads(service.state.get_job(item["job_id"])["payload"])
            assert payload["knowledge_routine"] == item["mode"]
            for source in payload["knowledge_sources"]:
                assert source.startswith("projects/atlas-harbor/raw/")
                assert source != huge
            if item["mode"] == "weekly":
                assert payload["read_only"] is True
                assert payload["memory_operation"] == "lint"
                assert payload["knowledge_sources"] == []
            else:
                assert payload["memory_operation"] == "ingest"
                assert 1 <= len(payload["knowledge_sources"]) <= spec["max_sources"]
        if day == 7:
            with pytest.raises(MemoryError, match="two_weeks"):
                routine.pilot_review(
                    HARBOR,
                    curator="curador-sintetico",
                    outcome="useful",
                    note="ainda uma semana",
                    accept=True,
                )
            first = routine.pilot_review(
                HARBOR,
                curator="curador-sintetico",
                outcome="useful",
                note="semana 1 virtual",
                curation_minutes=5,
            )
            assert first["state"] == "running"
            assert first["simulated_clock"] is True
        if day == 14:
            accepted = routine.pilot_review(
                HARBOR,
                curator="curador-sintetico",
                outcome="useful",
                note="semana 2 virtual",
                accept=True,
                curation_minutes=5,
            )
            assert accepted["state"] == "accepted_simulation"
            assert accepted["state"] != "accepted"
            assert accepted["simulated_clock"] is True
            assert routine.status(HARBOR)["enabled"] is True
        if day < spec["virtual_days"] - 1:
            for item in result["results"]:
                service.state.cancel_objective(item["objective_id"])
            assert service.publisher.git.head() == head
        else:
            job_id = next(
                item["job_id"]
                for item in result["results"]
                if item["mode"] == "nightly"
            )
            envelope = make_envelope("grant.acquire", HARBOR, {"job_id": job_id})
            envelope["origin"] = {
                "thread_id": "coordinator",
                "run_id": "coordinator-run",
            }
            granted = await request(service.config, envelope)
            assert granted["ok"], granted
            attempt = granted["data"]["attempt"]
            claim = make_envelope(
                "grant.claim",
                HARBOR,
                {
                    "attempt_id": attempt["id"],
                    "generation": attempt["generation"],
                    "thread_id": "nightly-child",
                    "run_id": "nightly-run",
                    "task_id": "synthetic-nightly",
                },
            )
            claim["origin"] = envelope["origin"]
            assert (await request(service.config, claim))["ok"]
            staging = service.config.state_dir / "staging"
            staging.mkdir(exist_ok=True)
            from oraculo.memory.schema import digest
            from oraculo.protocol import canonical_json

            raw = canonical_json(proposal()).encode()
            (staging / "legacy.json").write_bytes(raw)
            publish = make_envelope(
                "memory.propose",
                HARBOR,
                {
                    "attempt_id": attempt["id"],
                    "generation": attempt["generation"],
                    "artifact": "legacy.json",
                    "sha256": digest(raw),
                },
            )
            publish["origin"] = {
                "thread_id": "nightly-child",
                "run_id": "nightly-run",
            }
            rejected = await request(service.config, publish)
            assert not rejected["ok"]
            assert service.publisher.git.head() == head
            objectives_before = len(service.state.list_objectives())
            digest_before = vault_digest(service)
            disabled = routine.cancel(HARBOR)
            assert disabled["state"] == "disabled"
            assert disabled["effects_may_be_uncertain"] is True
            assert disabled["canonical_deleted"] is False
            assert service.state.get_job(job_id)["state"] == "cancel_requested"
            assert not (await request(service.config, publish))["ok"]
            assert routine.tick(HARBOR)["state"] == "disabled"
            assert len(service.state.list_objectives()) == objectives_before
            assert vault_digest(service) == digest_before
            assert service.publisher.git.head() == head

    keys = [
        row["dedupe_key"]
        for row in service.state.list_objectives()
        if row["dedupe_key"]
    ]
    duplicated = len(keys) - len(set(keys))
    assert duplicated == 0
    assert modes["nightly"] == spec["virtual_days"]
    assert modes["weekly"] == 5
    assert set(deferrals) == {"capacity_zero", "quota_wait"}
    assert routine.status(HARBOR)["enabled"] is False
    assert routine.status(HARBOR)["additional_spend_allowed"] is False
    assert routine.status(HARBOR)["pilot"]["state"] == "accepted_simulation"
    assert (service.publisher.vault / reviewed_path).read_bytes() == reviewed_bytes
    assert (service.publisher.vault / harbor_raw).read_text().startswith("Harbor fact")
    assert (service.publisher.vault / ridge_raw).read_text().startswith("Ridge fact")
    assert (service.publisher.vault / shared_raw).read_text().startswith("Sanitized")
    assert (
        (service.publisher.vault / alpha_before).read_text().startswith("alpha stays")
    )
    assert (service.publisher.vault / huge).read_bytes() == b"x" * (MAX_BYTES + 1)
    ridge_objectives = [
        row for row in service.state.list_objectives() if row["project_id"] == RIDGE
    ]
    assert ridge_objectives == []
    end_query = query(service, HARBOR, "HQ-17-CARMINE", spec)
    assert end_query["canonical_changed"] is False
    assert "RQ-44-INDIGO" not in json.dumps(end_query)
    assert service.publisher.git.head() == head

    report = {
        "clock": spec["clock"],
        "simulated_clock": True,
        "fixture_host_metadata": True,
        "not_live_host": True,
        "virtual_days": spec["virtual_days"],
        "seed": spec["seed"],
        "homonym_display_name": spec["homonym_display_name"],
        "projects": [HARBOR, RIDGE, "shared"],
        "cross_project_leaks": 0,
        "unsupported_new_facts": 0,
        "duplicated_operations": duplicated,
        "post_cancel_new_writes": 0,
        "required_skips": 0,
        "query_canonical_unchanged": True,
        "raw_preserved": True,
        "reviewed_preserved": True,
        "source_limits_respected": True,
        "context_budgets_respected": True,
        "disabled_default": True,
        "no_additional_spend": True,
        "human_utility_observed": False,
        "real_elapsed_wait_used": False,
        "pilot_state": "accepted_simulation",
        "nightly_schedules": modes["nightly"],
        "weekly_schedules": modes["weekly"],
        "deferrals": deferrals,
        "semantic_accuracy_claimed": False,
    }
    artifact = os.environ.get("SECOND_BRAIN_SYNTHETIC_ARTIFACT")
    if artifact:
        Path(artifact).write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )


def test_junit_parser_accepts_a_clean_suite(tmp_path):
    gate = load_gate()
    path = tmp_path / "clean.junit.xml"
    ET.ElementTree(
        ET.Element("testsuite", tests="1", failures="0", errors="0", skipped="0")
    ).write(path, encoding="unicode")
    parsed = gate.classify_junit(path)
    assert parsed["status"] == "passed"
    assert parsed["skipped"] == 0
