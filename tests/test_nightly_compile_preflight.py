"""Nightly jobs carry a service-built compile preview, not a caller blessing."""

import json
from datetime import datetime, timezone

import pytest

from oraculo.cli import make_envelope, request
from oraculo.memory.compiler import compile_knowledge
from oraculo.memory.maintenance import (
    DEFAULT_NIGHTLY_CONCEPT,
    PREPARED_COMPILATION_SCHEMA,
)
from oraculo.memory.routines import KnowledgeRoutines
from oraculo.memory.schema import (
    MemoryError,
    corpus,
    digest,
    parse_page,
    render_page,
)
from oraculo.memory.transport import MemoryArtifacts, memory_message
from oraculo.protocol import canonical_json, content_hash


def _routines(service):
    now = datetime(2026, 11, 2, 3, tzinfo=timezone.utc)
    return KnowledgeRoutines(service, clock=lambda: now)


def _publish_concept(service, name, body, alias):
    page_id = f"alpha--{name}"
    path = f"{corpus('alpha')}/wiki/concepts/{page_id}.md"
    content = render_page(
        {
            "type": "concept",
            "created": "2026-11-02",
            "updated": "2026-11-02",
            "sources": [],
            "tags": [],
            "aliases": [alias],
            "reviewed": False,
            "page_id": page_id,
            "project_id": "alpha",
            "schema_version": 1,
        },
        body,
    )
    service.publisher.publish(
        {
            "schema_version": 1,
            "batch_id": "preflight" + name,
            "operation_id": "preflight" + name,
            "project_id": "alpha",
            "base_commit": service.publisher.git.head(),
            "source_hashes": {},
            "changes": [
                {
                    "page_id": page_id,
                    "path": path,
                    "base_sha256": None,
                    "content": content,
                }
            ],
            "citations": [],
            "findings": [],
        }
    )
    return path


def _nightly_payload(service):
    routine = _routines(service)
    routine.configure("alpha", enabled=True, max_pending_jobs=2)
    results = routine.tick("alpha")["results"]
    nightly = next(item for item in results if item["mode"] == "nightly")
    weekly = next(item for item in results if item["mode"] == "weekly")
    payloads = {
        item["mode"]: json.loads(service.state.get_job(item["job_id"])["payload"])
        for item in results
    }
    return nightly, weekly, payloads


def _load(service, project_id, job_id, payload, context):
    store = MemoryArtifacts(service.config.state_dir)
    _message, reference = memory_message(
        {
            "project_id": project_id,
            "job_id": job_id,
            "payload": payload,
            "context": context,
        },
        store,
    )
    text = store.load(reference["sha256"], project_id, job_id)
    assert digest(text.encode()) == reference["sha256"]
    loaded = json.loads(text)
    assert loaded["payload"]["knowledge_routine"] == payload["knowledge_routine"]
    return text, loaded


async def _propose(service, job_id, proposal):
    envelope = make_envelope("grant.acquire", "alpha", {"job_id": job_id})
    envelope["origin"] = {"thread_id": "coordinator", "run_id": "coordinator-run"}
    granted = await request(service.config, envelope)
    assert granted["ok"], granted
    attempt = granted["data"]["attempt"]
    claim = make_envelope(
        "grant.claim",
        "alpha",
        {
            "attempt_id": attempt["id"],
            "generation": attempt["generation"],
            "thread_id": "nightly-child",
            "run_id": "nightly-run",
            "task_id": "nightly-task",
        },
    )
    claim["origin"] = envelope["origin"]
    assert (await request(service.config, claim))["ok"]
    staging = service.config.state_dir / "staging"
    staging.mkdir(exist_ok=True)
    body = canonical_json(proposal).encode()
    (staging / "nightly-preview.json").write_bytes(body)
    publish = make_envelope(
        "memory.propose",
        "alpha",
        {
            "attempt_id": attempt["id"],
            "generation": attempt["generation"],
            "artifact": "nightly-preview.json",
            "sha256": digest(body),
        },
    )
    publish["origin"] = {"thread_id": "nightly-child", "run_id": "nightly-run"}
    return await request(service.config, publish)


async def test_ready_receipt_matches_compile_and_waits_for_publisher(running_service):
    service = running_service
    marker = "UNRELATED-CONCEPT-BODY"
    unrelated = []
    for index in range(7):
        unrelated.append(
            _publish_concept(
                service,
                f"solto{index}",
                f"{marker}-{index}\n" + ("corpo omitido " * 400),
                alias=f"conceito solto {index}",
            )
        )
    summary = "Lote noturno sintetico preserva o alvo unico. " + ("alfa " * 3000)
    raw = service.publisher.snapshot(
        "alpha",
        json.dumps(
            {"summary": summary, "decisions": ["Manter CAS de pagina."]},
            ensure_ascii=False,
        ),
    )
    nightly, weekly, payloads = _nightly_payload(service)
    assert payloads["nightly"]["knowledge_routine"] == "nightly"
    assert payloads["nightly"]["knowledge_sources"] == [raw]
    assert "concept" not in payloads["nightly"]
    head = service.publisher.git.head()
    concepts_before = sorted(
        (service.publisher.vault / "projects/alpha/wiki/concepts").glob("*.md")
    )
    context = await service.memory_context(
        service.project("alpha"), payloads["nightly"]
    )
    text, loaded = _load(
        service, "alpha", nightly["job_id"], payloads["nightly"], context
    )
    receipt = loaded["context"]["prepared_compilation"]
    assert receipt["schema"] == PREPARED_COMPILATION_SCHEMA
    assert receipt["status"] == "ready"
    assert receipt["concept"] == DEFAULT_NIGHTLY_CONCEPT
    assert receipt["target_exists"] is False
    assert receipt["base_sha256"] is None
    assert receipt["reviewed"] is False
    assert receipt["proposal_base_commit"] is None
    assert receipt["registry_base_commit"] == head
    assert receipt["registry_base_commit"] == loaded["context"]["bundle"]["base_commit"]
    assert receipt["change_count"] == 1
    assert receipt["proposal_sha256"] == receipt["preview_sha256"]
    assert receipt["receipt_publishes"] is False
    assert receipt["autoapproval"] is False
    assert receipt["semantic_support"] is False
    assert receipt["shared_promotion"] is False
    assert receipt["unrelated_canon_required"] is False
    assert receipt["coverage"]["corpus_acceptance"] is False
    assert receipt["alias_conflicts"] == []
    assert receipt["alias_pages_checked"] >= 7
    assert receipt["next_action"] == "lookup_then_grant_bound_memory_propose"
    assert receipt["lookup"]["apply"] is False
    assert receipt["lookup"]["payload"] == {
        "concept": DEFAULT_NIGHTLY_CONCEPT,
        "sources": [raw],
    }
    assert receipt["frozen_sources"] == [
        {"path": raw, "sha256": digest((service.publisher.vault / raw).read_bytes())}
    ]
    assert receipt["retained_claim_ids"] == []
    assert receipt["new_claim_count"] >= 1
    assert receipt["proposal_bytes"] > len(canonical_json(receipt).encode())
    preview = compile_knowledge(
        service.publisher,
        "alpha",
        [raw],
        concept=DEFAULT_NIGHTLY_CONCEPT,
        apply=False,
    )
    assert preview["state"] == "proposed"
    assert preview["proposal"]["base_commit"] is None
    assert len(preview["proposal"]["changes"]) == 1
    assert content_hash(preview["proposal"]) == receipt["proposal_sha256"]
    assert preview["operation_id"] == receipt["operation_id"]
    assert preview["path"] == receipt["target_path"]
    assert preview["page_id"] == receipt["page_id"]
    page = preview["proposal"]["changes"][0]["content"]
    assert page not in text
    assert "knowledge_schema:" not in text
    assert marker not in text
    assert all(
        path not in {item["path"] for item in loaded["context"]["bundle"]["documents"]}
        for path in unrelated
    )
    assert '"changes"' not in canonical_json(receipt)
    assert service.publisher.git.head() == head
    assert (
        sorted((service.publisher.vault / "projects/alpha/wiki/concepts").glob("*.md"))
        == concepts_before
    )
    published = await _propose(service, nightly["job_id"], preview["proposal"])
    assert published["ok"], published
    assert published["data"]["state"] == "confirmed"
    cycles = service.memory_cycles.for_objective(
        service.state.get_job(nightly["job_id"])["objective_id"]
    )
    published_cycles = [c for c in cycles if c["write_state"] == "saved"]
    assert len(published_cycles) == 1
    assert published_cycles[0]["identity"]["job_id"] == nightly["job_id"]
    assert published_cycles[0]["identity"]["attempt_id"]
    assert (
        published_cycles[0]["write_receipt"]["operation_id"] == preview["operation_id"]
    )

    assert service.publisher.git.head() != head
    opened = (service.publisher.vault / preview["path"]).read_text(encoding="utf-8")
    opened_meta, _body = parse_page(opened)
    first_ids = [claim["id"] for claim in opened_meta["claims"]]
    assert first_ids
    assert f"[[{raw}]]" in opened

    second_summary = "Segundo lote acrescenta uma decisao nova."
    second = service.publisher.snapshot(
        "alpha",
        json.dumps({"summary": second_summary}, ensure_ascii=False),
    )
    follow = dict(payloads["nightly"], knowledge_sources=[second])
    held = service.publisher.git.head()
    again = await service.memory_context(service.project("alpha"), follow)
    again_text, again_loaded = _load(service, "alpha", nightly["job_id"], follow, again)
    follow_receipt = again_loaded["context"]["prepared_compilation"]
    assert follow_receipt["status"] == "ready"
    assert follow_receipt["target_exists"] is True
    assert follow_receipt["base_sha256"] == digest(opened)
    assert follow_receipt["proposal_base_commit"] is None
    assert follow_receipt["retained_claim_ids"] == first_ids
    assert follow_receipt["new_claim_count"] >= 1
    assert set(follow_receipt["retained_source_hashes"]) == {raw}
    assert [item["path"] for item in follow_receipt["frozen_sources"]] == [second]
    assert raw in follow_receipt["proposal_source_hashes"]
    assert second in follow_receipt["proposal_source_hashes"]
    extended = compile_knowledge(
        service.publisher,
        "alpha",
        [second],
        concept=DEFAULT_NIGHTLY_CONCEPT,
        apply=False,
    )
    assert content_hash(extended["proposal"]) == follow_receipt["proposal_sha256"]
    assert extended["proposal"]["changes"][0]["content"] not in again_text
    assert service.publisher.git.head() == held
    service.publisher.publish(extended["proposal"])
    saved, _body = parse_page(
        (service.publisher.vault / extended["path"]).read_text(encoding="utf-8")
    )
    assert first_ids == [
        claim["id"] for claim in saved["claims"] if claim["id"] in first_ids
    ]
    assert set(first_ids) <= {claim["id"] for claim in saved["claims"]}
    assert any(second_summary in claim["text"] for claim in saved["claims"])

    weekly_context = await service.memory_context(
        service.project("alpha"), payloads["weekly"]
    )
    assert "prepared_compilation" not in weekly_context
    assert (
        weekly_context["bundle"]["selection"]["coverage"]["corpus_acceptance"] is False
    )
    assert weekly_context["bundle"]["selection"]["policy"]["read_only"] is True
    query = await service.memory_context(
        service.project("alpha"), {"memory_operation": "query", "query": ""}
    )
    assert "prepared_compilation" not in query
    assert query["bundle"]["selection"]["abstain_reason"] == "no_useful_terms"
    assert query["bundle"]["documents"] == []
    manual = await service.memory_context(
        service.project("alpha"),
        {"memory_operation": "ingest", "query": "qxvmanualingest"},
    )
    assert "prepared_compilation" not in manual


async def test_reviewed_alias_foreign_and_poison_fail_closed(running_service):
    service = running_service
    source = service.publisher.snapshot(
        "alpha", json.dumps({"summary": "pagina ja revisada"}, ensure_ascii=False)
    )
    compiled = compile_knowledge(
        service.publisher,
        "alpha",
        [source],
        concept=DEFAULT_NIGHTLY_CONCEPT,
        apply=True,
    )
    path = service.publisher.vault / compiled["path"]
    meta, body = parse_page(path.read_text(encoding="utf-8"))
    meta["reviewed"] = True
    reviewed = render_page(meta, body)
    service.publisher.publish(
        {
            "schema_version": 1,
            "batch_id": "mark-reviewed-preflight",
            "operation_id": "mark-reviewed-preflight",
            "project_id": "alpha",
            "base_commit": service.publisher.git.head(),
            "source_hashes": {},
            "changes": [
                {
                    "page_id": compiled["page_id"],
                    "path": compiled["path"],
                    "base_sha256": digest(path.read_text(encoding="utf-8")),
                    "content": reviewed,
                }
            ],
            "citations": [],
            "findings": [],
        }
    )
    added = service.publisher.snapshot(
        "alpha",
        json.dumps({"summary": "sugestao que nao grava"}, ensure_ascii=False),
    )
    nightly, _weekly, payloads = _nightly_payload(service)
    assert payloads["nightly"]["knowledge_sources"] == [added]
    before = path.read_bytes()
    head = service.publisher.git.head()
    context = await service.memory_context(
        service.project("alpha"), payloads["nightly"]
    )
    text, loaded = _load(
        service, "alpha", nightly["job_id"], payloads["nightly"], context
    )
    receipt = loaded["context"]["prepared_compilation"]
    assert receipt["status"] == "review_required"
    assert receipt["code"] == "reviewed_page_protected"
    assert receipt["reviewed"] is True
    assert receipt["target_exists"] is True
    assert receipt["proposal_sha256"] is None
    assert receipt["proposal_base_commit"] is None
    suggestion = compile_knowledge(
        service.publisher,
        "alpha",
        [added],
        concept=DEFAULT_NIGHTLY_CONCEPT,
        apply=False,
    )
    assert suggestion["state"] == "suggestion"
    assert content_hash(suggestion["suggestion"]) == receipt["preview_sha256"]
    assert suggestion["suggestion"]["changes"][0]["content"] not in text
    assert service.publisher.git.head() == head
    refused = await _propose(service, nightly["job_id"], suggestion["suggestion"])
    assert not refused["ok"]
    assert refused["error"]["message"] == "reviewed_page_protected"
    assert path.read_bytes() == before
    assert service.publisher.git.head() == head

    # A second project source collides on the concept alias without being the target.
    # Reuse the same vault only through a fresh service in the next tests.


async def test_alias_conflict_blocks_without_copying_unrelated_canon(running_service):
    service = running_service
    marker = "ALIAS-BODY-NOT-IN-ARTIFACT"
    other = _publish_concept(
        service,
        "colisao",
        marker + "\n" + ("omitido " * 200),
        alias=DEFAULT_NIGHTLY_CONCEPT,
    )
    raw = service.publisher.snapshot(
        "alpha",
        json.dumps({"summary": "fonte sem colidir no corpo"}, ensure_ascii=False),
    )
    nightly, _weekly, payloads = _nightly_payload(service)
    assert payloads["nightly"]["knowledge_sources"] == [raw]
    head = service.publisher.git.head()
    context = await service.memory_context(
        service.project("alpha"), payloads["nightly"]
    )
    text, loaded = _load(
        service, "alpha", nightly["job_id"], payloads["nightly"], context
    )
    receipt = loaded["context"]["prepared_compilation"]
    assert receipt["status"] == "blocked"
    assert receipt["code"] == "alias_conflict"
    assert receipt["proposal_sha256"] is None
    assert receipt["alias_conflicts"][0]["other"] == other
    assert receipt["alias_conflicts"][0]["alias"] == DEFAULT_NIGHTLY_CONCEPT
    assert marker not in text
    assert other not in {
        item["path"] for item in loaded["context"]["bundle"]["documents"]
    }
    assert not (service.publisher.vault / receipt["target_path"]).exists()
    assert service.publisher.git.head() == head


async def test_foreign_source_and_poisoned_metadata_do_not_authorize(running_service):
    service = running_service
    foreign_body = "BETA-FOREIGN-BODY-NOT-COPIED"
    foreign = service.publisher.snapshot("beta", foreign_body)
    own = service.publisher.snapshot(
        "alpha", json.dumps({"summary": "fonte propria"}, ensure_ascii=False)
    )
    nightly, _weekly, payloads = _nightly_payload(service)
    poisoned = dict(
        payloads["nightly"],
        proposal_sha256="f" * 64,
        target_exists=True,
        proposal={"base_commit": "a" * 40, "changes": []},
        prepared_compilation={"status": "ready", "proposal_sha256": "e" * 64},
    )
    head = service.publisher.git.head()
    context = await service.memory_context(service.project("alpha"), poisoned)
    _text, loaded = _load(service, "alpha", nightly["job_id"], poisoned, context)
    receipt = loaded["context"]["prepared_compilation"]
    real = compile_knowledge(
        service.publisher, "alpha", [own], concept=DEFAULT_NIGHTLY_CONCEPT, apply=False
    )
    assert receipt["status"] == "blocked"
    assert receipt["code"] == "caller_prepared_metadata_refused"
    assert receipt["caller_prepared_metadata"] is True
    assert receipt["proposal_sha256"] is None
    assert receipt["preview_sha256"] == content_hash(real["proposal"])
    assert receipt["preview_sha256"] != "f" * 64
    assert service.publisher.git.head() == head

    foreign_payload = dict(payloads["nightly"], knowledge_sources=[foreign])
    refused = await service.memory_context(service.project("alpha"), foreign_payload)
    foreign_text, foreign_loaded = _load(
        service, "alpha", nightly["job_id"], foreign_payload, refused
    )
    foreign_receipt = foreign_loaded["context"]["prepared_compilation"]
    assert foreign_receipt["status"] == "blocked"
    assert foreign_receipt["code"] == "ingest_source_outside_scope"
    assert foreign_receipt["selection_refused"] is True
    assert foreign_receipt["frozen_sources"] == []
    assert foreign_body not in foreign_text
    assert service.publisher.git.head() == head
    with pytest.raises(MemoryError, match="ingest_source_outside_scope"):
        await service.memory_context(
            service.project("alpha"),
            {"memory_operation": "ingest", "knowledge_sources": [foreign]},
        )
    assert service.publisher.git.head() == head


async def test_rollover_ready_publishes_subset_through_grant(
    running_service, monkeypatch
):
    import oraculo.memory.compiler as compiler

    service = running_service
    old = service.publisher.snapshot(
        "alpha", json.dumps({"summary": "histórico " * 300})
    )
    saved = compile_knowledge(
        service.publisher, "alpha", [old], concept=DEFAULT_NIGHTLY_CONCEPT, apply=True
    )
    original = (service.publisher.vault / saved["path"]).read_bytes()
    monkeypatch.setattr(compiler, "MAX_PROPOSAL_BYTES", len(original))
    for index in range(20):
        service.publisher.snapshot(
            "alpha", json.dumps({"summary": f"Nova decisão {index}", "checks": ["ok"]})
        )
    nightly, _, payloads = _nightly_payload(service)
    context = await service.memory_context(
        service.project("alpha"), payloads["nightly"]
    )
    receipt = context["prepared_compilation"]
    assert receipt["status"] == "ready"
    assert receipt["deferred_sources"]
    assert len(receipt["frozen_sources"]) == len(
        payloads["nightly"]["knowledge_sources"]
    )
    assert receipt["target_path"] != saved["path"]
    preview = compile_knowledge(
        service.publisher, "alpha", **receipt["lookup"]["payload"]
    )
    assert content_hash(preview["proposal"]) == receipt["proposal_sha256"]
    published = await _propose(service, nightly["job_id"], preview["proposal"])
    assert published["ok"], published
    assert published["data"]["state"] == "confirmed"
    assert (service.publisher.vault / saved["path"]).read_bytes() == original
    next_sources, _ = _routines(service).pending_sources("alpha", 20)
    assert set(next_sources) == {item["path"] for item in receipt["deferred_sources"]}
