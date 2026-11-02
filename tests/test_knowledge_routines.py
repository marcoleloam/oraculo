"""Opt-in F6 jobs use real disposable State, grants, quota and a labeled fake clock."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from oraculo.cli import make_envelope, request
from oraculo.memory.routines import KnowledgeRoutines
from oraculo.memory.schema import MemoryError


def routines(service, *, date=None):
    now = [date or datetime(2026, 11, 4, 3, tzinfo=timezone.utc)]
    return KnowledgeRoutines(service, clock=lambda: now[0]), now


async def test_disabled_default_and_disable_rollback_preserve_corpus(running_service):
    service = running_service
    routine, now = routines(service)
    raw = service.publisher.snapshot("alpha", "fonte pendente")
    head = service.publisher.git.head()
    assert routine.tick("alpha")["state"] == "disabled"
    assert not service.state.list_objectives()
    routine.configure("alpha", enabled=True)
    first = routine.tick("alpha")
    assert first["state"] == "scheduled" and len(first["results"]) == 1
    run = first["results"][0]
    job = service.state.get_job(run["job_id"])
    payload = json.loads(job["payload"])
    assert payload["knowledge_sources"] == [raw]
    assert "execution_evidence" not in payload
    assert payload["memory_operation"] == "ingest"
    assert service.state.capacity()["occupied"] == 0  # queuing never starts an executor
    assert routine.tick("alpha")["state"] == "not_due"
    assert len(service.state.list_objectives()) == 1
    assert run["selection_latency_ms"] >= 0
    disabled = routine.cancel("alpha")
    assert disabled["state"] == "disabled"
    assert disabled["cancelled_objectives"] == [run["objective_id"]]
    assert service.state.get_job(run["job_id"])["state"] == "stopped"
    now[0] += timedelta(days=1)
    assert routine.tick("alpha")["state"] == "disabled"
    assert service.publisher.git.head() == head
    assert (service.publisher.vault / raw).read_text() == "fonte pendente"
    assert len(routine.status("alpha")["runs"]) == 1


async def test_capacity_quota_offline_and_backoff_never_start_fallback(running_service):
    service = running_service
    routine, now = routines(service)
    service.publisher.snapshot("alpha", "pendente")
    routine.configure("alpha", enabled=True)
    capacity = service.state.capacity()["available"]
    service.state.reserve_managed("occupied-test-only", "codex", slots=capacity)
    blocked = routine.tick("alpha")
    assert blocked["state"] == "deferred" and blocked["reason"] == "capacity_zero"
    assert not service.state.list_objectives()
    service.state.release_managed(
        "occupied-test-only",
        {"workState": "result_available", "hasPendingChildRuns": False},
    )
    assert routine.tick("alpha")["state"] == "deferred"  # persisted backoff
    now[0] += timedelta(seconds=301)
    service.router.mark_quota("codex", now=now[0].timestamp())
    blocked = routine.tick("alpha")
    assert blocked["state"] == "deferred" and blocked["reason"] == "quota_wait"
    assert not service.state.list_objectives()
    now[0] += timedelta(seconds=301)
    service.catalog = {"providers": []}
    blocked = routine.tick("alpha")
    assert blocked["state"] == "deferred" and blocked["reason"] == "unavailable"
    assert not service.state.list_objectives()


async def test_weekly_is_readonly_and_sources_remain_exact(running_service):
    service = running_service
    routine, _ = routines(service, date=datetime(2026, 11, 2, 3, tzinfo=timezone.utc))
    selected = service.publisher.snapshot("alpha", "fonte selecionada")
    service.publisher.snapshot("beta", "fora do projeto")
    service.publisher.snapshot("shared", "geral compartilhado")
    routine.configure("alpha", enabled=True, max_pending_jobs=2)
    head = service.publisher.git.head()
    results = routine.tick("alpha")["results"]
    assert [item["mode"] for item in results] == ["nightly", "weekly"]
    payloads = [
        json.loads(service.state.get_job(item["job_id"])["payload"]) for item in results
    ]
    assert payloads[0]["knowledge_sources"] == [selected]
    assert payloads[1]["read_only"] and payloads[1]["memory_operation"] == "lint"
    context = await service.memory_context(service.project("alpha"), payloads[0])
    assert [item["path"] for item in context["context"]["raw_sources"]] == [selected]
    nightly = context["bundle"]["selection"]
    assert nightly["operation"] == "ingest" and nightly["abstained"] is False
    assert nightly["coverage"]["state"] == "empty_canon"
    assert nightly["coverage"]["canonical_pages"] == 0
    assert nightly["coverage"]["relevant_canonical_pages"] == 0
    assert nightly["policy"]["read_only"] is False
    assert (
        nightly["source_hashes"][selected]
        == context["context"]["raw_sources"][0]["sha256"]
    )
    weekly = await service.memory_context(service.project("alpha"), payloads[1])
    assert weekly["bundle"]["selection"]["operation"] == "lint"
    assert weekly["bundle"]["selection"]["coverage"]["state"] == "inspected"
    assert weekly["bundle"]["selection"]["policy"]["read_only"] is True
    assert weekly["bundle"]["selection"]["abstain_reason"] is None
    assert service.publisher.git.head() == head  # routine metadata did not become raw
    assert routine.tick("alpha")["state"] == "not_due"


async def test_pilot_requires_real_elapsed_time_and_preserves_review_history(
    running_service,
):
    service = running_service
    routine, now = routines(service)
    source = service.publisher.snapshot("alpha", "corpus piloto explícito")
    head = service.publisher.git.head()
    pilot = routine.pilot_start("alpha", curator="curador", source_manifest=[source])
    assert pilot["simulated_clock"] is True
    assert routine.status("alpha")["enabled"] is False
    with pytest.raises(MemoryError, match="first_week"):
        routine.pilot_review("alpha", curator="curador", outcome="useful", note="cedo")
    now[0] += timedelta(days=7)
    routine.pilot_review(
        "alpha",
        curator="curador",
        outcome="needs_work",
        note="ajustar",
        curation_minutes=12,
    )
    routine.pilot_review(
        "alpha",
        curator="curador",
        outcome="useful",
        note="ajuste conferido",
        curation_minutes=8,
    )
    with pytest.raises(MemoryError, match="two_weeks"):
        routine.pilot_review(
            "alpha", curator="curador", outcome="useful", note="ainda cedo", accept=True
        )
    now[0] += timedelta(days=7)
    accepted = routine.pilot_review(
        "alpha",
        curator="curador",
        outcome="useful",
        note="duas revisões",
        accept=True,
        curation_minutes=10,
    )
    assert accepted["state"] == "accepted_simulation"
    assert len(accepted["reviews"]) == 3
    assert service.publisher.git.head() == head
    restarted = routine.pilot_start(
        "alpha", curator="curador", source_manifest=[source]
    )
    assert restarted["state"] == "running"
    assert len(routine.status("alpha")["pilot_history"]) == 1
    stopped = routine.pilot_review(
        "alpha", curator="curador", outcome="stop", note="interromper"
    )
    assert stopped["state"] == "stopped" and routine.status("alpha")["enabled"] is False
    assert service.publisher.git.head() == head


async def test_pilot_scope_and_configuration_errors_are_explicit(running_service):
    service = running_service
    routine, _ = routines(service)
    other = service.publisher.snapshot("beta", "privado")
    with pytest.raises(MemoryError, match="outside_scope"):
        routine.pilot_start("alpha", curator="curador", source_manifest=[other])
    for data in (
        {"enabled": "yes"},
        {"enabled": True, "max_sources": 21},
        {"enabled": True, "timezone_name": "Not/A_Timezone"},
    ):
        with pytest.raises(MemoryError):
            routine.configure("alpha", **data)


async def test_cancel_active_job_retains_uncertainty_and_stops_new_effects(
    running_service,
):
    from test_memory_schema import proposal

    from oraculo.memory.schema import digest
    from oraculo.protocol import canonical_json

    service = running_service
    routine, _ = routines(service)
    service.publisher.snapshot("alpha", "selecionada")
    routine.configure("alpha", enabled=True)
    queued = routine.tick("alpha")["results"][0]
    envelope = make_envelope("grant.acquire", "alpha", {"job_id": queued["job_id"]})
    envelope["origin"] = {"thread_id": "coordinator", "run_id": "coordinator-run"}
    granted = await request(service.config, envelope)
    assert granted["ok"] and "attempt" in granted["data"], granted
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
    raw = canonical_json(proposal()).encode()
    (staging / "legacy.json").write_bytes(raw)
    publish = make_envelope(
        "memory.propose",
        "alpha",
        {
            "attempt_id": attempt["id"],
            "generation": attempt["generation"],
            "artifact": "legacy.json",
            "sha256": digest(raw),
        },
    )
    publish["origin"] = {"thread_id": "nightly-child", "run_id": "nightly-run"}
    head = service.publisher.git.head()
    rejected = await request(service.config, publish)
    assert not rejected["ok"] and "typed_claims" in str(rejected), rejected
    disabled = routine.cancel("alpha")
    assert disabled["effects_may_be_uncertain"] is True
    assert service.state.get_job(queued["job_id"])["state"] == "cancel_requested"
    assert not (await request(service.config, publish))["ok"]
    assert routine.tick("alpha")["state"] == "disabled"
    assert service.publisher.git.head() == head


async def test_routine_replay_after_enqueue_before_marker_is_deduplicated(
    running_service,
):
    service = running_service
    routine, _ = routines(service)
    service.publisher.snapshot("alpha", "primeira")
    routine.configure("alpha", enabled=True)
    record = routine._record("alpha")
    first = routine._enqueue("alpha", record, "nightly", "2026-11-04")
    service.publisher.snapshot("alpha", "segunda chegou após commit do job")
    replay = routine._enqueue("alpha", record, "nightly", "2026-11-04")
    assert replay["receipt_replay"] and replay["job_id"] == first["job_id"]
    assert len(service.state.list_objectives()) == 1


async def test_nightly_frozen_evidence_and_retained_claims(running_service):
    from copy import deepcopy

    from oraculo.knowledge import validate_nightly_proposal
    from oraculo.memory.compiler import compile_knowledge
    from oraculo.memory.schema import parse_page, render_page

    service = running_service
    first = service.publisher.snapshot("alpha", '{"summary":"fonte um"}')
    second = service.publisher.snapshot("alpha", '{"summary":"fonte dois"}')
    initial = compile_knowledge(
        service.publisher, "alpha", [first], concept="evidências"
    )
    validate_nightly_proposal(service.publisher, "alpha", initial["proposal"], [first])
    with pytest.raises(MemoryError, match="outside_selection"):
        validate_nightly_proposal(
            service.publisher, "alpha", initial["proposal"], [second]
        )
    empty = deepcopy(initial["proposal"])
    metadata, body = parse_page(empty["changes"][0]["content"])
    metadata["claims"] = []
    empty["changes"][0]["content"] = render_page(metadata, body)
    with pytest.raises(MemoryError, match="typed_claims"):
        validate_nightly_proposal(service.publisher, "alpha", empty, [first])
    service.publisher.publish(initial["proposal"])
    delta = compile_knowledge(
        service.publisher, "alpha", [second], concept="evidências"
    )
    validate_nightly_proposal(service.publisher, "alpha", delta["proposal"], [second])
    tampered = deepcopy(delta["proposal"])
    metadata, body = parse_page(tampered["changes"][0]["content"])
    old = next(
        claim for claim in metadata["claims"] if claim["evidence"][0]["path"] == first
    )
    old["text"] = "alteração não sustentada na seleção nova"
    tampered["changes"][0]["content"] = render_page(metadata, body)
    with pytest.raises(MemoryError, match="outside_selection"):
        validate_nightly_proposal(service.publisher, "alpha", tampered, [second])


def _concept(project, name, body, *, alias, reviewed=False, sources=None, claims=None):
    from oraculo.memory.schema import corpus, render_page

    page_id = ("shared--" if project == "shared" else f"{project}--") + name
    path = f"{corpus(project)}/wiki/concepts/{page_id}.md"
    metadata = {
        "type": "concept",
        "created": "2026-11-02",
        "updated": "2026-11-02",
        "sources": sources or [],
        "tags": [],
        "aliases": [alias],
        "reviewed": reviewed,
        "page_id": page_id,
        "project_id": project,
        "schema_version": 1,
    }
    if claims is not None:
        metadata["knowledge_schema"] = 1
        metadata["claims"] = claims
    return path, render_page(metadata, body)


def _publish(service, project, name, body, **kwargs):
    path, content = _concept(project, name, body, **kwargs)
    service.publisher.publish(
        {
            "schema_version": 1,
            "batch_id": "f6" + name.replace("-", ""),
            "operation_id": "f6" + name.replace("-", ""),
            "project_id": project,
            "base_commit": service.publisher.git.head(),
            "source_hashes": {},
            "changes": [
                {
                    "page_id": path.rsplit("/", 1)[-1][:-3],
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


def _transport(service, project_id, job_id, payload, context):
    import hashlib
    import json

    from oraculo.memory.transport import MemoryArtifacts, memory_message

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
    assert hashlib.sha256(text.encode()).hexdigest() == reference["sha256"]
    return json.loads(text)


async def test_large_corpus_routines_keep_context_through_transport(running_service):
    """Empty-query routines must not inherit budgeted abstention.

    A handful of pages still comes back through strict_abstain=False
    compatibility. The installed failure appears only after the legacy bundle
    exceeds the 6000-token detail budget.
    """
    import hashlib
    import json
    from datetime import timedelta

    from oraculo.memory import ConflictError
    from oraculo.memory.compiler import compile_knowledge
    from oraculo.memory.context import Context
    from oraculo.memory.maintenance import DETAIL_TOKENS
    from oraculo.memory.retrieval import estimate_tokens
    from oraculo.memory.schema import digest

    service = running_service
    routine, now = routines(service, date=datetime(2026, 11, 2, 3, tzinfo=timezone.utc))
    routine.configure("alpha", enabled=True, max_pending_jobs=2)
    head = service.publisher.git.head()
    opened = routine.tick("alpha")
    assert [item["state"] for item in opened["results"]] == [
        "no_pending_sources",
        "queued",
    ]
    weekly_payload = json.loads(
        service.state.get_job(opened["results"][1]["job_id"])["payload"]
    )
    empty = await service.memory_context(service.project("alpha"), weekly_payload)
    empty_bundle = _transport(
        service,
        "alpha",
        opened["results"][1]["job_id"],
        weekly_payload,
        empty,
    )["context"]["bundle"]
    assert empty_bundle["selection"]["coverage"]["state"] == "no_content"
    assert empty_bundle["selection"]["coverage"]["corpus_acceptance"] is False
    assert empty_bundle["selection"]["abstained"] is False
    assert empty_bundle["selection"]["policy"]["read_only"] is True
    assert service.publisher.git.head() == head
    routine.cancel("alpha")

    routine.configure("alpha", enabled=True, max_pending_jobs=2)
    now[0] += timedelta(days=1)
    raw = service.publisher.snapshot(
        "alpha",
        json.dumps(
            {"summary": "A fila preserva a ordem de entrada."}, ensure_ascii=False
        ),
    )
    service.publisher.snapshot("beta", "fonte de outro projeto")
    service.publisher.snapshot("shared", "nota compartilhada")
    queued = routine.tick("alpha")["results"][0]
    assert queued["state"] == "queued"
    nightly_payload = json.loads(service.state.get_job(queued["job_id"])["payload"])
    assert nightly_payload["knowledge_sources"] == [raw]
    assert "query" not in nightly_payload
    first = await service.memory_context(service.project("alpha"), nightly_payload)
    assert first["bundle"]["selection"]["coverage"]["state"] == "empty_canon"
    assert first["bundle"]["selection"]["coverage"]["canonical_pages"] == 0
    assert first["bundle"]["selection"]["coverage"]["relevant_canonical_pages"] == 0
    assert first["bundle"]["selection"]["policy"]["read_only"] is False
    assert first["bundle"]["selection"]["abstain_reason"] is None
    assert first["bundle"]["selection"]["source_hashes"][raw] == digest(
        (service.publisher.vault / raw).read_text()
    )
    assert (
        first["context"]["raw_sources"][0]["sha256"]
        == first["bundle"]["selection"]["source_hashes"][raw]
    )

    concepts = []
    for number in range(19):
        compiled = compile_knowledge(
            service.publisher,
            "alpha",
            [raw],
            concept=f"regra documental da fila {number}",
            apply=True,
        )
        assert compiled["state"] == "confirmed", compiled
        concepts.append(compiled["path"])
    linked = f"[[{raw}]]"
    broken = _publish(
        service,
        "alpha",
        "link-partido",
        "O corpo não aponta para página ausente.\n",
        alias="Link partido",
        sources=[linked],
        claims=[
            {
                "id": "link-partido",
                "project_id": "alpha",
                "text": "A referência [[pagina-ausente]] não resolve.",
                "kind": "hypothesis",
                "category": "observation",
                "support": "not_evaluated",
                "evidence": [],
            }
        ],
    )
    alias_a = _publish(
        service,
        "alpha",
        "alias-a",
        "Primeira página com o mesmo alias.\n",
        alias="mesma fila",
        sources=[linked],
    )
    alias_b = _publish(
        service,
        "alpha",
        "alias-b",
        "Segunda página com o mesmo alias.\n",
        alias="mesma fila",
        sources=[linked],
    )
    reviewed = _publish(
        service,
        "alpha",
        "revisada",
        "Página revisada que cita a fonte congelada.\n",
        alias="Pagina revisada",
        reviewed=True,
        sources=[linked],
    )
    unrelated = _publish(
        service,
        "alpha",
        "sem-fonte",
        "Assunto que não cita o raw congelado.\n",
        alias="Assunto solto",
    )
    shared = _publish(
        service,
        "shared",
        "leitura",
        "Leitura compartilhada sem link no corpo.\n",
        alias="Leitura compartilhada",
        claims=[
            {
                "id": "leitura-compartilhada",
                "project_id": "shared",
                "text": f"A fonte [[{raw}]] permanece no projeto.",
                "kind": "hypothesis",
                "category": "observation",
                "support": "not_evaluated",
                "evidence": [],
            }
        ],
    )
    foreign = _publish(
        service,
        "beta",
        "alheio",
        "Página de outro projeto.\n",
        alias="mesma fila",
    )
    legacy = Context(service.publisher.vault).legacy_bundle("alpha", "", 20)
    legacy_tokens = sum(
        estimate_tokens(item["content"]) for item in legacy["documents"]
    )
    assert legacy_tokens > DETAIL_TOKENS
    budgeted = Context(service.publisher.vault).bundle(
        "alpha", "", mode="budgeted", strict_abstain=False
    )
    assert budgeted["documents"] == []
    assert budgeted["selection"]["abstain_reason"] == "no_useful_terms"
    assert budgeted["selection"]["compatibility"] is None

    head = service.publisher.git.head()
    nightly = await service.memory_context(service.project("alpha"), nightly_payload)
    carried = _transport(service, "alpha", queued["job_id"], nightly_payload, nightly)[
        "context"
    ]
    bundle = carried["bundle"]
    selection = bundle["selection"]
    assert selection["version"] == "f6-operation-context-v1"
    assert selection["operation"] == "ingest"
    assert selection["abstained"] is False
    assert selection["coverage"]["state"] == "bounded"
    assert selection["coverage"]["corpus_acceptance"] is False
    assert selection["policy"]["read_only"] is False
    assert selection["policy"]["shared_promotion"] is False
    assert selection["policy"]["autoapproval"] is False
    assert selection["policy"]["detail_tokens"] == DETAIL_TOKENS
    assert bundle["base_commit"] == head
    assert selection["source_hashes"] == {
        raw: digest((service.publisher.vault / raw).read_text())
    }
    assert len(carried["context"]["raw_sources"]) == 1
    assert (
        carried["context"]["raw_sources"][0]["content"]
        == (service.publisher.vault / raw).read_text()
    )
    bases = {item["path"]: item for item in selection["bases"]}
    assert selection["coverage"]["canonical_pages"] > len(bases)
    assert selection["coverage"]["relevant_canonical_pages"] == len(bases)
    assert unrelated not in {item["path"] for item in bundle["documents"]}
    assert set(concepts) <= set(bases)
    assert broken in bases and alias_a in bases and alias_b in bases
    assert reviewed in bases and bases[reviewed]["reviewed"] is True
    assert unrelated not in bases and foreign not in bases and shared not in bases
    assert shared in selection["readable_shared"]
    assert any(item["code"] == "duplicate_alias" for item in bundle["findings"])
    assert any(
        item["code"] == "broken_link" and item["path"] == broken
        for item in bundle["findings"]
    )
    assert any(
        item["code"] == "reviewed_page_protected" for item in selection["conflicts"]
    )
    documents = {item["path"]: item for item in bundle["documents"]}
    assert selection["index"][0]["path"] in documents
    assert set(bases) - set(documents)
    assert (
        sum(estimate_tokens(item["content"]) for item in bundle["documents"])
        <= DETAIL_TOKENS
    )
    for path, item in list(documents.items()):
        full = (service.publisher.vault / path).read_text()
        assert item["content"] == full
        assert item["sha256"] == hashlib.sha256(full.encode()).hexdigest()
    for path, item in bases.items():
        full = (service.publisher.vault / path).read_text()
        assert item["sha256"] == hashlib.sha256(full.encode()).hexdigest()
        assert path.startswith("projects/alpha/")
    assert all(not path.startswith("projects/beta/") for path in documents)
    assert service.publisher.git.head() == head

    query = await service.memory_context(
        service.project("alpha"),
        {"memory_operation": "query", "query": "the a of"},
    )
    assert query["bundle"]["selection"]["version"] == "f1-v2"
    assert query["bundle"]["selection"]["abstain_reason"] == "no_useful_terms"
    assert query["bundle"]["documents"] == []
    guided = await service.memory_context(
        service.project("alpha"),
        {"memory_operation": "ingest", "query": "qxvnightlyabsent"},
    )
    assert guided["bundle"]["selection"]["version"] == "f1-v2"
    assert guided["bundle"]["selection"]["abstain_reason"] == "unsupported"
    assert service.publisher.git.head() == head

    routine.cancel("alpha")
    routine.configure("alpha", enabled=True, max_pending_jobs=2)
    now[0] = datetime(2026, 11, 9, 3, tzinfo=timezone.utc)
    week = routine.tick("alpha")
    weekly_run = next(item for item in week["results"] if item["mode"] == "weekly")
    assert weekly_run["state"] == "queued"
    lint_payload = json.loads(service.state.get_job(weekly_run["job_id"])["payload"])
    lint = await service.memory_context(service.project("alpha"), lint_payload)
    lint_bundle = _transport(
        service, "alpha", weekly_run["job_id"], lint_payload, lint
    )["context"]["bundle"]
    codes = {item["code"] for item in lint_bundle["findings"]}
    assert {"broken_link", "duplicate_alias"} <= codes
    assert all(
        not str(item.get("path", "")).startswith("projects/beta/")
        for item in lint_bundle["findings"]
    )
    assert lint_bundle["selection"]["coverage"]["state"] == "findings"
    assert lint_bundle["selection"]["coverage"]["canonical_pages"] >= 19
    assert lint_bundle["selection"]["coverage"]["corpus_acceptance"] is False
    assert lint_bundle["selection"]["policy"]["read_only"] is True
    assert lint_bundle["base_commit"] == head
    assert service.publisher.git.head() == head
    assert (service.publisher.vault / raw).read_text().startswith("{")

    from oraculo.knowledge import validate_nightly_proposal
    from oraculo.memory.schema import parse_page, render_page

    proposed = compile_knowledge(
        service.publisher, "alpha", [raw], concept="proposta sem gravar", apply=False
    )
    assert proposed["state"] == "proposed"
    validate_nightly_proposal(service.publisher, "alpha", proposed["proposal"], [raw])
    stripped = json.loads(json.dumps(proposed["proposal"]))
    metadata, body = parse_page(stripped["changes"][0]["content"])
    metadata["claims"] = []
    stripped["changes"][0]["content"] = render_page(metadata, body)
    with pytest.raises(MemoryError, match="typed_claims"):
        validate_nightly_proposal(service.publisher, "alpha", stripped, [raw])
    reviewed_body = (service.publisher.vault / reviewed).read_text()
    rewritten = reviewed_body.replace(
        "Página revisada que cita a fonte congelada.",
        "sobrescrita recusada",
    )
    with pytest.raises(ConflictError, match="reviewed_page_protected"):
        service.publisher.publish(
            {
                "schema_version": 1,
                "batch_id": "f6overwrite",
                "operation_id": "f6overwrite",
                "project_id": "alpha",
                "base_commit": head,
                "source_hashes": {},
                "changes": [
                    {
                        "page_id": "alpha--revisada",
                        "path": reviewed,
                        "base_sha256": digest(reviewed_body),
                        "content": rewritten,
                    }
                ],
                "citations": [],
                "findings": [],
            }
        )
    with pytest.raises(ConflictError, match="base_hash_mismatch"):
        service.publisher.publish(
            {
                "schema_version": 1,
                "batch_id": "f6stale",
                "operation_id": "f6stale",
                "project_id": "alpha",
                "base_commit": head,
                "source_hashes": {},
                "changes": [
                    {
                        "page_id": "alpha--revisada",
                        "path": reviewed,
                        "base_sha256": "0" * 64,
                        "content": reviewed_body,
                    }
                ],
                "citations": [],
                "findings": [],
            }
        )
    assert service.publisher.git.head() == head
    assert (service.publisher.vault / reviewed).read_text() == reviewed_body


def _frozen_record(vault, path):
    from oraculo.memory.schema import digest

    text = (vault / path).read_text()
    return {"path": path, "content": text, "sha256": digest(text)}


def test_citation_forms_distinguish_empty_canon_from_uncited_pages(tmp_path):
    """Publisher-accepted raw links select a page; ambiguous ones do not.

    The R1 probe reported empty_canon and canonical_pages 0 while an uncited
    canonical page was on disk, and it missed [[stem]] / [[filename.md]].
    """
    from types import SimpleNamespace

    from test_memory_schema import publisher

    from oraculo.memory.maintenance import Maintenance, _cites
    from oraculo.memory.schema import digest

    pub = publisher(tmp_path)
    service = SimpleNamespace(publisher=pub)
    raw = pub.snapshot("alpha", '{"summary":"frozen raw exact bytes"}\n')
    disk = (pub.vault / raw).read_bytes()
    stem = raw[:-3].rsplit("/", 1)[-1]
    maintenance = Maintenance(pub)
    record = _frozen_record(pub.vault, raw)

    head = pub.git.head()
    with pub.locked():
        first = maintenance.ingest_bundle("alpha", [record])
    assert first["selection"]["coverage"]["state"] == "empty_canon"
    assert first["selection"]["coverage"]["canonical_pages"] == 0
    assert first["selection"]["coverage"]["relevant_canonical_pages"] == 0
    assert first["selection"]["source_hashes"][raw] == digest(disk)
    assert (pub.vault / raw).read_bytes() == disk
    assert pub.git.head() == head

    shared_cite = _publish(
        service,
        "shared",
        "sharedcite",
        "Leitura compartilhada sem link no corpo.\n",
        alias="Shared cite",
        claims=[
            {
                "id": "shared-cite",
                "project_id": "shared",
                "text": f"A fonte [[{raw}]] permanece no projeto.",
                "kind": "hypothesis",
                "category": "observation",
                "support": "not_evaluated",
                "evidence": [],
            }
        ],
    )
    with pub.locked():
        shared_only = maintenance.ingest_bundle("alpha", [record])
    assert shared_only["selection"]["coverage"]["state"] == "empty_canon"
    assert shared_only["selection"]["coverage"]["canonical_pages"] == 0
    assert shared_only["selection"]["coverage"]["relevant_canonical_pages"] == 0
    assert shared_cite in shared_only["selection"]["readable_shared"]
    assert shared_cite not in {
        item["path"] for item in shared_only["selection"]["bases"]
    }

    uncited = _publish(
        service,
        "alpha",
        "uncited",
        "Pagina canonica que nao cita o raw.\n",
        alias="Uncited canon",
    )
    with pub.locked():
        labeled = maintenance.ingest_bundle("alpha", [record])
    assert labeled["selection"]["coverage"]["state"] == "no_relevant_bases"
    assert labeled["selection"]["coverage"]["canonical_pages"] == 1
    assert labeled["selection"]["coverage"]["relevant_canonical_pages"] == 0
    assert uncited not in {item["path"] for item in labeled["selection"]["bases"]}
    assert uncited not in {item["path"] for item in labeled["documents"]}

    stem_page = _publish(
        service,
        "alpha",
        "stemcite",
        f"Cita o stem com alias [[{stem}|nome]].\n",
        alias="Stem cite",
        sources=[f"[[{stem}|nome]]"],
    )
    filename_page = _publish(
        service,
        "alpha",
        "filecite",
        f"Cita o arquivo [[{stem}.md#topo]].\n",
        alias="File cite",
        sources=[f"[[{stem}.md#topo]]"],
    )
    full_page = _publish(
        service,
        "alpha",
        "fullcite",
        "Cita o caminho completo.\n",
        alias="Full cite",
        sources=[f"[[{raw}]]"],
    )
    relative_page = _publish(
        service,
        "alpha",
        "relcite",
        f"Caminho relativo ao arquivo [fonte]({stem}.md).\n",
        alias="Relative cite",
    )
    sibling = _publish(
        service,
        "alpha",
        "sibling",
        "Resolve outra pagina, nao o raw.\n",
        alias="Sibling cite",
        sources=["[[alpha--fullcite]]"],
    )
    outside = _publish(
        service,
        "alpha",
        "outside",
        "Hipotese com link fora do escopo.\n",
        alias="Outside cite",
        claims=[
            {
                "id": "outside-cite",
                "project_id": "alpha",
                "text": "A fonte [[projects/beta/raw/nope]] fica de fora.",
                "kind": "hypothesis",
                "category": "observation",
                "support": "not_evaluated",
                "evidence": [],
            }
        ],
    )
    unsafe = "projects/alpha/wiki/concepts/alpha--unsafe.md"
    relative_escape = "../../" + raw.removeprefix("projects/alpha/")
    with pub.locked():
        pub._transaction(
            "f6unsafe",
            {unsafe: f"Nao seguir [[{relative_escape}]].\n".encode()},
        )
        cited = maintenance.ingest_bundle("alpha", [record])
    bases = {item["path"] for item in cited["selection"]["bases"]}
    documents = {item["path"] for item in cited["documents"]}
    assert {stem_page, filename_page, full_page, relative_page} <= bases
    assert uncited not in bases and uncited not in documents
    assert sibling not in bases and outside not in bases and unsafe not in bases
    assert cited["selection"]["coverage"]["state"] == "bounded"
    assert cited["selection"]["coverage"]["relevant_canonical_pages"] == len(bases)
    assert cited["selection"]["coverage"]["canonical_pages"] > len(bases)
    assert (pub.vault / raw).read_bytes() == disk

    twin = pub.snapshot("shared", (pub.vault / raw).read_text())
    assert twin.rsplit("/", 1)[-1] == raw.rsplit("/", 1)[-1]
    with pub.locked():
        ambiguous = maintenance.ingest_bundle("alpha", [record])
    ambiguous_bases = {item["path"] for item in ambiguous["selection"]["bases"]}
    assert full_page in ambiguous_bases
    assert stem_page not in ambiguous_bases
    assert filename_page not in ambiguous_bases
    assert relative_page not in ambiguous_bases
    assert twin not in ambiguous_bases

    page = "projects/alpha/raw/listing.md"
    scoped = {raw, twin, page}
    assert _cites(f"[[{stem}.md]]", page, {raw}, scoped) is True
    far = "projects/alpha/wiki/concepts/alpha--far.md"
    assert _cites(f"[[{stem}.md]]", far, {raw}, scoped | {far}) is False
    assert _cites(f"[[{raw}]]", far, {raw}, scoped | {far}) is True
    assert _cites("[[/etc/passwd]]", far, {raw}, scoped | {far}) is False
    assert _cites(f"[[{twin}]]", far, {raw}, scoped | {far}) is False


def test_invalid_frozen_sources_fail_closed_before_the_bundle(tmp_path):
    """R1 accepted mismatched, oversized and traversing frozen input."""
    from test_memory_schema import publisher

    from oraculo.memory.maintenance import MAX_RAW_BYTES, Maintenance
    from oraculo.memory.schema import digest

    pub = publisher(tmp_path)
    raw = pub.snapshot("alpha", '{"summary":"frozen raw exact bytes"}\n')
    disk = (pub.vault / raw).read_bytes()
    text = disk.decode()
    maintenance = Maintenance(pub)
    valid = _frozen_record(pub.vault, raw)

    def rejected(sources, code):
        head = pub.git.head()
        with pytest.raises(MemoryError, match=code), pub.locked():
            maintenance.ingest_bundle("alpha", sources)
        assert pub.git.head() == head
        assert (pub.vault / raw).read_bytes() == disk

    stale = text + "drift"
    rejected(
        [{"path": raw, "content": stale, "sha256": digest(stale)}],
        "source_hash_mismatch",
    )
    rejected(
        [{"path": raw, "content": text, "sha256": "0" * 64}],
        "source_hash_mismatch",
    )
    rejected(
        [valid, {"path": raw, "content": stale, "sha256": digest(stale)}],
        "source_hash_mismatch",
    )
    rejected(
        [
            {
                "path": "projects/alpha/raw/../../projects/beta/raw/escaped.md",
                "content": "nope",
                "sha256": digest("nope"),
            }
        ],
        "ingest_source_outside_scope",
    )
    rejected(
        [
            {
                "path": f"projects/alpha/raw/../../{raw}",
                "content": text,
                "sha256": digest(text),
            }
        ],
        "ingest_source_outside_scope",
    )
    dotted = raw.replace("/raw/", "/raw/./")
    rejected(
        [{"path": dotted, "content": text, "sha256": digest(text)}],
        "ingest_source_outside_scope",
    )
    foreign = "projects/beta/raw/" + ("ab" * 32) + ".md"
    rejected(
        [{"path": foreign, "content": "x", "sha256": digest("x")}],
        "ingest_source_outside_scope",
    )
    missing = "projects/alpha/raw/" + ("c" * 64) + ".md"
    rejected(
        [{"path": missing, "content": "x", "sha256": digest("x")}],
        "ingest_source_outside_scope",
    )
    misnamed = "projects/alpha/raw/not-a-hash.md"
    with pub.locked():
        pub._transaction("f6misnamed", {misnamed: b"conteudo cru\n"})
    misnamed_text = (pub.vault / misnamed).read_text()
    rejected(
        [
            {
                "path": misnamed,
                "content": misnamed_text,
                "sha256": digest(misnamed_text),
            }
        ],
        "source_hash_mismatch",
    )

    oversized = pub.snapshot("alpha", "o" * (MAX_RAW_BYTES + 1))
    rejected([_frozen_record(pub.vault, oversized)], "source_too_large")
    head = pub.git.head()
    with pytest.raises(MemoryError, match="too_many_ingest_sources"):
        maintenance.ingest_bundle("alpha", [valid] * 21)
    assert pub.git.head() == head

    exact = "b" * MAX_RAW_BYTES
    exact_sha = digest(exact)
    exact_path = f"projects/alpha/raw/{exact_sha}.md"
    accepted, conflicts = maintenance._frozen_sources(
        "alpha",
        [{"path": exact_path, "content": exact, "sha256": exact_sha}],
        {exact_path: exact},
    )
    assert conflicts == []
    assert accepted == [
        {
            "path": exact_path,
            "sha256": exact_sha,
            "bytes": MAX_RAW_BYTES,
            "mutable": True,
        }
    ]
    shared = pub.snapshot("shared", "nota compartilhada distinta")
    with pub.locked():
        shared_bundle = maintenance.ingest_bundle(
            "alpha", [_frozen_record(pub.vault, shared)], read_only=True
        )
        own_bundle = maintenance.ingest_bundle("alpha", [valid])
        lint = maintenance.lint_bundle("alpha", read_only=False)
    assert shared_bundle["selection"]["source_hashes"][shared] == digest(
        (pub.vault / shared).read_text()
    )
    assert shared_bundle["selection"]["policy"]["read_only"] is True
    assert shared_bundle["selection"]["policy"]["mutable_root"] == "projects/alpha"
    shared_rows, shared_conflicts = maintenance._frozen_sources(
        "alpha",
        [_frozen_record(pub.vault, shared)],
        maintenance.context.files("alpha"),
    )
    assert shared_conflicts == [] and shared_rows[0]["mutable"] is False
    assert own_bundle["selection"]["policy"]["read_only"] is False
    assert own_bundle["selection"]["source_hashes"][raw] == digest(disk)
    assert lint["selection"]["policy"]["read_only"] is True
    assert (pub.vault / raw).read_bytes() == disk


async def test_read_only_ingest_flag_still_rejects_propose(running_service):
    from test_memory_schema import proposal

    from oraculo.memory.schema import digest
    from oraculo.protocol import canonical_json

    service = running_service
    raw = service.publisher.snapshot("alpha", "fonte somente leitura")
    payload = {
        "text": "Ingestão marcada somente leitura",
        "kind": "memory",
        "memory_operation": "ingest",
        "read_only": True,
        "knowledge_routine": "nightly",
        "knowledge_sources": [raw],
    }
    head = service.publisher.git.head()
    context = await service.memory_context(service.project("alpha"), payload)
    assert context["bundle"]["selection"]["operation"] == "ingest"
    assert context["bundle"]["selection"]["policy"]["read_only"] is True
    assert context["bundle"]["selection"]["coverage"]["state"] == "empty_canon"
    lint = service.consistent_bundle("alpha", "", operation="lint", read_only=False)
    assert lint["selection"]["policy"]["read_only"] is True
    assert service.publisher.git.head() == head

    objective = service.state.submit_objective("alpha", payload["text"])
    job = service.state.enqueue_job(objective["id"], "memory", payload)
    envelope = make_envelope("grant.acquire", "alpha", {"job_id": job["id"]})
    envelope["origin"] = {"thread_id": "coordinator", "run_id": "coordinator-run"}
    granted = await request(service.config, envelope)
    assert granted["ok"] and "attempt" in granted["data"], granted
    attempt = granted["data"]["attempt"]
    claim = make_envelope(
        "grant.claim",
        "alpha",
        {
            "attempt_id": attempt["id"],
            "generation": attempt["generation"],
            "thread_id": "readonly-child",
            "run_id": "readonly-run",
            "task_id": "readonly-task",
        },
    )
    claim["origin"] = envelope["origin"]
    assert (await request(service.config, claim))["ok"]
    staging = service.config.state_dir / "staging"
    staging.mkdir(exist_ok=True)
    body = canonical_json(proposal()).encode()
    (staging / "readonly.json").write_bytes(body)
    publish = make_envelope(
        "memory.propose",
        "alpha",
        {
            "attempt_id": attempt["id"],
            "generation": attempt["generation"],
            "artifact": "readonly.json",
            "sha256": digest(body),
        },
    )
    publish["origin"] = {"thread_id": "readonly-child", "run_id": "readonly-run"}
    rejected = await request(service.config, publish)
    assert not rejected["ok"] and "read_only_memory_operation" in str(rejected)
    assert service.publisher.git.head() == head
