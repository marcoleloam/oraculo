import json
from datetime import date

import pytest
from test_memory_schema import page, proposal, publisher

from oraculo.memory.claims import validate_knowledge
from oraculo.memory.migration import apply_migration, migration_digest, plan_migration
from oraculo.memory.schema import (
    ConflictError,
    MemoryError,
    digest,
    parse_page,
    render_page,
)


def approve(plan):
    approved = dict(plan)
    approved["approval"] = plan["plan_hash"]
    return approved


def tree_bytes(root):
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def test_dry_run_is_stable_and_does_not_write(tmp_path):
    pub = publisher(tmp_path)
    raw = pub.snapshot("alpha", "evidência sintética")
    pub.publish(
        proposal(page(sources=[f"[[{raw.removesuffix('.md')}]]"], reviewed=True))
    )
    pub.publish(
        proposal(page(name="nota", body="Texto legado."), operation="nota", name="nota")
    )
    before = tree_bytes(tmp_path)
    first = plan_migration(pub, "alpha")
    second = plan_migration(pub, "alpha")
    assert first == second
    assert tree_bytes(tmp_path) == before
    assert first["limitations"] == [
        "legacy_marked_not_evaluated_not_supported",
        "reviewed_pages_pending_not_edited",
        "temporal_lint_left_to_freshness",
        "no_live_vault_migration",
    ]
    assert [item["path"] for item in first["pending_review"]] == [
        "projects/alpha/wiki/concepts/alpha--decision.md"
    ]
    assert [item["page_id"] for item in first["operations"]] == ["alpha--nota"]


def test_apply_requires_approval_and_preserves_raw_reviewed_and_ids(tmp_path):
    pub = publisher(tmp_path)
    raw_text = "evidência sintética"
    raw = pub.snapshot("alpha", raw_text)
    reviewed = page(sources=[f"[[{raw.removesuffix('.md')}]]"], reviewed=True)
    pub.publish(proposal(reviewed))
    legacy = page(name="nota", body="Texto legado.")
    pub.publish(proposal(legacy, operation="nota", name="nota"))
    reviewed_path = pub.vault / proposal()["changes"][0]["path"]
    legacy_path = pub.vault / proposal(name="nota")["changes"][0]["path"]
    reviewed_before = reviewed_path.read_bytes()
    legacy_before = legacy_path.read_bytes()
    raw_before = (pub.vault / raw).read_bytes()
    plan = plan_migration(pub, "alpha")
    with pytest.raises(MemoryError, match="migration_approval_required"):
        apply_migration(pub, plan)
    tampered = approve(plan)
    tampered["limitations"] = ["alterado"]
    with pytest.raises(MemoryError, match="migration_plan_tampered"):
        apply_migration(pub, tampered)

    applied = apply_migration(pub, approve(plan))
    replay = apply_migration(pub, approve(plan))
    assert replay["receipts"] == applied["receipts"]
    stored = legacy_path.read_text()
    parsed, body = parse_page(stored)
    assert body == "Texto legado."
    assert parsed["page_id"] == "alpha--nota"
    assert parsed["schema_version"] == 1
    assert parsed["knowledge_schema"] == 1
    assert parsed["claims"] == []
    assert parsed["reviewed"] is False
    findings = validate_knowledge(stored, "alpha", {str(legacy_path): stored})
    assert findings[0]["support"] == "not_evaluated"
    assert "supported" not in {item["code"] for item in findings}
    assert reviewed_path.read_bytes() == reviewed_before
    assert (pub.vault / raw).read_bytes() == raw_before
    log = (pub.vault / "projects/alpha/wiki/log.md").read_text()
    operation_id = plan["batches"][0]["operation_id"]
    assert log.count(f"operation_id: {operation_id}\n") == 1
    journal = json.loads((pub.journals / f"{operation_id}.json").read_text())
    relative = legacy_path.relative_to(pub.vault).as_posix()
    original = __import__("base64").b64decode(journal["before"][relative])
    assert original == legacy_before
    assert applied["rollback"]["prepared"] is True
    assert applied["rollback"]["restores"][0]["base_sha256"] == digest(legacy_before)


def test_concurrent_edit_and_downgrades_are_refused(tmp_path):
    pub = publisher(tmp_path)
    text = "A válvula fechou às 10:00."
    raw = pub.snapshot("alpha", text)
    pub.publish(proposal(page(name="one", body="Um."), operation="one", name="one"))
    pub.publish(proposal(page(name="two", body="Dois."), operation="two", name="two"))
    supported = render_page(
        {
            "type": "concept",
            "created": "2026-10-18",
            "updated": "2026-10-18",
            "sources": [f"[[{raw.removesuffix('.md')}]]"],
            "tags": [],
            "aliases": [],
            "reviewed": False,
            "page_id": "alpha--supported",
            "project_id": "alpha",
            "schema_version": 1,
            "knowledge_schema": 1,
            "claims": [
                {
                    "id": "kept",
                    "project_id": "alpha",
                    "text": "A válvula fechou às 10:00.",
                    "kind": "fact",
                    "category": "observation",
                    "support": "supported",
                    "evidence": [
                        {
                            "path": raw,
                            "sha256": digest(text),
                            "start": 0,
                            "end": len(text.encode()),
                            "quote": text,
                        }
                    ],
                    "assessment": {
                        "verdict": "supported",
                        "reviewer": "revisora-sintetica",
                        "reason": "O trecho afirma o fechamento.",
                        "assessed_at": "2026-11-01T00:00:00+00:00",
                    },
                }
            ],
        },
        "Apoiado.",
    )
    pub.publish(proposal(supported, operation="supported", name="supported"))
    future = render_page(
        {
            "type": "concept",
            "created": "2026-10-18",
            "updated": "2026-10-18",
            "sources": [],
            "tags": [],
            "aliases": [],
            "reviewed": False,
            "page_id": "alpha--future",
            "project_id": "alpha",
            "schema_version": 1,
            "knowledge_schema": 2,
            "claims": [],
        },
        "Versão futura.",
    )
    future_path = pub.vault / "projects/alpha/wiki/concepts/alpha--future.md"
    future_path.write_text(future)
    plan = plan_migration(pub, "alpha")
    assert {item["page_id"] for item in plan["operations"]} == {
        "alpha--one",
        "alpha--two",
    }
    assert any(item["reason"] == "supported_preserved" for item in plan["preserved"])
    assert any(
        item["reason"] == "schema_downgrade_refused" for item in plan["deferred"]
    )
    supported_path = pub.vault / proposal(name="supported")["changes"][0]["path"]
    supported_before = supported_path.read_bytes()
    one = pub.vault / proposal(name="one")["changes"][0]["path"]
    pub.publish(
        proposal(
            page(name="one", body="Edição concorrente."),
            operation="race",
            name="one",
            base=digest(one.read_bytes()),
        )
    )
    with pytest.raises(ConflictError, match="base_hash_mismatch"):
        apply_migration(pub, approve(plan))
    assert "knowledge_schema" not in parse_page(one.read_text())[0]
    two = pub.vault / proposal(name="two")["changes"][0]["path"]
    assert "knowledge_schema" not in parse_page(two.read_text())[0]
    assert supported_path.read_bytes() == supported_before
    assert future_path.read_text() == future

    hostile = {
        "kind": "knowledge-migration-v1",
        "project_id": "alpha",
        "base_commit": pub.git.head(),
        "limits": {"max_pages": 20, "max_bytes": 2 * 1024 * 1024},
        "limitations": [],
        "base_hashes": {},
        "raw_hashes": {raw: digest(text)},
        "preserved": [],
        "pending_review": [],
        "deferred": [],
        "operations": [],
        "batches": [
            {
                "operation_id": "mdowngrade0000000000000000000001",
                "proposal": {
                    "schema_version": 1,
                    "batch_id": "mdowngrade0000000000000000000001",
                    "operation_id": "mdowngrade0000000000000000000001",
                    "project_id": "alpha",
                    "base_commit": pub.git.head(),
                    "source_hashes": {},
                    "changes": [
                        {
                            "page_id": "alpha--supported",
                            "path": supported_path.relative_to(pub.vault).as_posix(),
                            "base_sha256": digest(supported_before),
                            "content": render_page(
                                {
                                    **parse_page(supported)[0],
                                    "claims": [],
                                    "knowledge_schema": 1,
                                },
                                "Apoiado.",
                            ),
                        }
                    ],
                    "citations": [],
                    "findings": [],
                },
            }
        ],
        "rollback": {"prepared": True, "restores": []},
    }
    hostile["plan_hash"] = migration_digest(hostile)
    with pytest.raises(MemoryError, match="migration_downgrade_refused"):
        apply_migration(pub, approve(hostile))
    assert supported_path.read_bytes() == supported_before


def test_batches_replay_after_crash_and_size_limit(tmp_path, monkeypatch):
    pub = publisher(tmp_path)
    first_changes = []
    for index in range(20):
        name = f"p{index:02d}"
        first_changes.append(
            {
                "page_id": f"alpha--{name}",
                "path": f"projects/alpha/wiki/concepts/alpha--{name}.md",
                "base_sha256": None,
                "content": page(name=name, body=f"Página {index}."),
            }
        )
    pub.publish(
        {
            "schema_version": 1,
            "batch_id": "seed-a",
            "operation_id": "seed-a",
            "project_id": "alpha",
            "base_commit": None,
            "source_hashes": {},
            "changes": first_changes,
            "citations": [],
            "findings": [],
        }
    )
    pub.publish(
        proposal(page(name="p20", body="Página 20."), operation="seed-b", name="p20")
    )
    huge_body = "a" * 2_200_000
    huge = page(name="huge", body=huge_body)
    huge_path = pub.vault / "projects/alpha/wiki/concepts/alpha--huge.md"
    huge_path.write_text(huge)
    plan = plan_migration(pub, "alpha")
    assert [len(batch["proposal"]["changes"]) for batch in plan["batches"]] == [20, 1]
    assert all(len(batch["proposal"]["changes"]) <= 20 for batch in plan["batches"])
    assert any(item["reason"] == "proposal_too_large" for item in plan["deferred"])
    assert len(json.dumps(plan)) < 500_000
    second_id = plan["batches"][1]["operation_id"]
    real = pub.publish
    state = {"armed": True}

    def crash_once(proposal_payload):
        operation = (
            proposal_payload["operation_id"]
            if isinstance(proposal_payload, dict)
            else proposal_payload.operation_id
        )
        if state["armed"] and operation == second_id:
            state["armed"] = False
            raise RuntimeError("crash-before-second-batch")
        return real(proposal_payload)

    monkeypatch.setattr(pub, "publish", crash_once)
    with pytest.raises(RuntimeError, match="crash-before-second-batch"):
        apply_migration(pub, approve(plan))
    monkeypatch.setattr(pub, "publish", real)
    apply_migration(pub, approve(plan))
    log = (pub.vault / "projects/alpha/wiki/log.md").read_text()
    assert log.count(f"operation_id: {plan['batches'][0]['operation_id']}\n") == 1
    assert log.count(f"operation_id: {second_id}\n") == 1
    for index in range(21):
        name = f"p{index:02d}"
        stored = (
            pub.vault / f"projects/alpha/wiki/concepts/alpha--{name}.md"
        ).read_text()
        parsed, body = parse_page(stored)
        assert parsed["knowledge_schema"] == 1
        assert parsed["claims"] == []
        assert parsed["schema_version"] == 1
        assert body == f"Página {index}."
    assert huge_path.read_text() == huge


def _approve_hostile(pub, operation, path, base, content, page_id):
    plan = {
        "kind": "knowledge-migration-v1",
        "project_id": "alpha",
        "base_commit": pub.git.head(),
        "limits": {"max_pages": 20, "max_bytes": 2 * 1024 * 1024},
        "limitations": [],
        "base_hashes": {},
        "raw_hashes": {},
        "preserved": [],
        "pending_review": [],
        "deferred": [],
        "operations": [],
        "batches": [
            {
                "operation_id": operation,
                "proposal": {
                    "schema_version": 1,
                    "batch_id": operation,
                    "operation_id": operation,
                    "project_id": "alpha",
                    "base_commit": pub.git.head(),
                    "source_hashes": {},
                    "changes": [
                        {
                            "page_id": page_id,
                            "path": path,
                            "base_sha256": base,
                            "content": content,
                        }
                    ],
                    "citations": [],
                    "findings": [],
                },
            }
        ],
        "rollback": {"prepared": True, "restores": []},
    }
    plan["plan_hash"] = migration_digest(plan)
    plan["approval"] = plan["plan_hash"]
    return plan


def _frontmatter(**extra):
    meta = {
        "type": "concept",
        "created": "2026-10-18",
        "updated": "2026-10-18",
        "sources": [],
        "tags": [],
        "aliases": ["Decisão"],
        "reviewed": False,
        "page_id": "alpha--page",
        "project_id": "alpha",
        "schema_version": 1,
    }
    meta.update(extra)
    return meta


@pytest.mark.parametrize(
    "claims",
    [
        [],
        [{"id": "kept", "support": "not_evaluated"}],
        [{"id": "kept", "support": "source_linked", "evidence": []}],
        [{"id": "kept", "support": "insufficient"}],
        [{"id": "kept", "support": "contested"}],
        [{"id": "kept", "support": "supported"}],
        [{"id": "kept", "support": "retired"}],
    ],
)
def test_typed_claims_cannot_be_wiped_or_rewritten(tmp_path, claims):
    pub = publisher(tmp_path)
    path = "projects/alpha/wiki/concepts/alpha--kept.md"
    target = pub.vault / path
    target.parent.mkdir(parents=True, exist_ok=True)
    original = render_page(
        _frontmatter(
            page_id="alpha--kept",
            knowledge_schema=1,
            claims=claims,
            note="nota-original",
        ),
        "Corpo estável.",
    )
    target.write_text(original)
    before = target.read_bytes()
    wiped = render_page(
        _frontmatter(
            page_id="alpha--kept", knowledge_schema=1, claims=[], note="trocada"
        ),
        "Corpo estável.",
    )
    with pytest.raises(MemoryError, match="^migration_downgrade_refused$"):
        apply_migration(
            pub,
            _approve_hostile(
                pub,
                "mwipe00000000000000000000000001",
                path,
                digest(original),
                wiped,
                "alpha--kept",
            ),
        )
    assert target.read_bytes() == before


@pytest.mark.parametrize(
    ("extra", "code"),
    [
        (
            {"claims": [{"id": "kept", "support": "source_linked"}]},
            "migration_downgrade_refused",
        ),
        ({"knowledge_schema": 1}, "migration_downgrade_refused"),
        ({"knowledge_schema": 2, "claims": []}, "schema_downgrade_refused"),
    ],
)
def test_partial_or_future_knowledge_is_not_legacy(tmp_path, extra, code):
    pub = publisher(tmp_path)
    path = "projects/alpha/wiki/concepts/alpha--partial.md"
    target = pub.vault / path
    target.parent.mkdir(parents=True, exist_ok=True)
    original = render_page(
        _frontmatter(page_id="alpha--partial", **extra),
        "Corpo estável.",
    )
    target.write_text(original)
    before = target.read_bytes()
    proposed = render_page(
        _frontmatter(page_id="alpha--partial", knowledge_schema=1, claims=[]),
        "Corpo estável.",
    )
    with pytest.raises(MemoryError, match=f"^{code}$"):
        apply_migration(
            pub,
            _approve_hostile(
                pub,
                "mpartial00000000000000000000001",
                path,
                digest(original),
                proposed,
                "alpha--partial",
            ),
        )
    assert target.read_bytes() == before


def test_legacy_migration_cannot_inject_claims_or_edit_frontmatter(tmp_path):
    pub = publisher(tmp_path)
    text = "A válvula fechou às 10:00."
    raw = pub.snapshot("alpha", text)
    raw_before = (pub.vault / raw).read_bytes()
    legacy = render_page(
        _frontmatter(page_id="alpha--legacy", note="original"),
        "Antiga.",
    )
    pub.publish(proposal(legacy, operation="legacy", name="legacy"))
    path = "projects/alpha/wiki/concepts/alpha--legacy.md"
    target = pub.vault / path
    current = target.read_text()
    current_meta, current_body = parse_page(current)
    assert "claims" not in current_meta
    assert "knowledge_schema" not in current_meta
    injected = render_page(
        {
            **current_meta,
            "knowledge_schema": 1,
            "claims": [
                {
                    "id": "newfact",
                    "project_id": "alpha",
                    "text": "A válvula fechou.",
                    "kind": "fact",
                    "category": "observation",
                    "support": "source_linked",
                    "evidence": [
                        {
                            "path": raw,
                            "sha256": digest(text),
                            "start": 0,
                            "end": len(text.encode()),
                            "quote": text,
                        }
                    ],
                }
            ],
        },
        current_body,
    )
    with pytest.raises(MemoryError, match="^migration_promotion_refused$"):
        apply_migration(
            pub,
            _approve_hostile(
                pub,
                "minject000000000000000000000001",
                path,
                digest(current),
                injected,
                "alpha--legacy",
            ),
        )
    bare = render_page(
        {
            **current_meta,
            "knowledge_schema": 1,
            "claims": [
                {
                    "id": "nofact",
                    "project_id": "alpha",
                    "text": "Sem fonte.",
                    "kind": "fact",
                    "category": "observation",
                    "support": "source_linked",
                    "evidence": [],
                }
            ],
        },
        current_body,
    )
    with pytest.raises(MemoryError, match="^migration_promotion_refused$"):
        apply_migration(
            pub,
            _approve_hostile(
                pub,
                "mbare0000000000000000000000001",
                path,
                digest(current),
                bare,
                "alpha--legacy",
            ),
        )
    noted = render_page(
        {**current_meta, "note": "trocada", "knowledge_schema": 1, "claims": []},
        current_body,
    )
    with pytest.raises(MemoryError, match="^migration_identity_changed$"):
        apply_migration(
            pub,
            _approve_hostile(
                pub,
                "mnote0000000000000000000000001",
                path,
                digest(current),
                noted,
                "alpha--legacy",
            ),
        )
    stored_meta, stored_body = parse_page(target.read_text())
    assert stored_body == current_body
    assert stored_meta == current_meta
    assert (pub.vault / raw).read_bytes() == raw_before


def test_reviewed_and_raw_stay_untouched_under_hostile_plan(tmp_path):
    pub = publisher(tmp_path)
    raw = pub.snapshot("alpha", "bruto imutável")
    raw_before = (pub.vault / raw).read_bytes()
    locked = page(name="locked", body="Travada.", reviewed=True)
    pub.publish(proposal(locked, operation="locked", name="locked"))
    path = "projects/alpha/wiki/concepts/alpha--locked.md"
    before = (pub.vault / path).read_bytes()
    meta, body = parse_page(before.decode())
    unlocked = render_page(
        {**meta, "reviewed": False, "knowledge_schema": 1, "claims": []},
        body,
    )
    with pytest.raises(ConflictError, match="^reviewed_page_protected$"):
        apply_migration(
            pub,
            _approve_hostile(
                pub,
                "mlock0000000000000000000000001",
                path,
                digest(before.decode()),
                unlocked,
                "alpha--locked",
            ),
        )
    assert (pub.vault / path).read_bytes() == before
    with pytest.raises(MemoryError, match="^raw_immutable$"):
        apply_migration(
            pub,
            _approve_hostile(
                pub,
                "mraw00000000000000000000000001",
                raw,
                digest(raw_before.decode()),
                "bruto reescrito",
                "alpha--raw",
            ),
        )
    assert (pub.vault / raw).read_bytes() == raw_before
    assert (pub.vault / path).read_bytes() == before


def test_batch_limits_fail_before_validation_and_do_not_leak_payload(tmp_path):
    pub = publisher(tmp_path)
    secret = "SENTINELA-MIGRACAO-QUE-NAO-PODE-VAZAR"
    changes = []
    for index in range(21):
        changes.append(
            {
                "page_id": f"alpha--n{index}",
                "path": f"projects/alpha/wiki/concepts/alpha--n{index}.md",
                "base_sha256": None,
                "content": page(name=f"n{index}", body=f"{secret} {index}"),
            }
        )
    operation = "mlimit0000000000000000000000001"
    plan = {
        "kind": "knowledge-migration-v1",
        "project_id": "alpha",
        "base_commit": pub.git.head(),
        "limits": {"max_pages": 20, "max_bytes": 2 * 1024 * 1024},
        "limitations": [],
        "base_hashes": {},
        "raw_hashes": {},
        "preserved": [],
        "pending_review": [],
        "deferred": [],
        "operations": [],
        "batches": [
            {
                "operation_id": operation,
                "proposal": {
                    "schema_version": 1,
                    "batch_id": operation,
                    "operation_id": operation,
                    "project_id": "alpha",
                    "base_commit": None,
                    "source_hashes": {},
                    "changes": changes,
                    "citations": [],
                    "findings": [],
                },
            }
        ],
        "rollback": {"prepared": True, "restores": []},
    }
    plan["plan_hash"] = migration_digest(plan)
    plan["approval"] = plan["plan_hash"]
    with pytest.raises(MemoryError, match="^migration_batch_limit$") as caught:
        apply_migration(pub, plan)
    assert secret not in str(caught.value)
    assert not (pub.vault / "projects/alpha/wiki/concepts/alpha--n0.md").exists()

    huge = page(name="huge", body=secret + ("a" * (2 * 1024 * 1024)))
    huge_plan = _approve_hostile(
        pub,
        "mhuge0000000000000000000000001",
        "projects/alpha/wiki/concepts/alpha--huge.md",
        None,
        huge,
        "alpha--huge",
    )
    with pytest.raises(MemoryError, match="^migration_batch_limit$") as oversized:
        apply_migration(pub, huge_plan)
    assert secret not in str(oversized.value)
    assert not (pub.vault / "projects/alpha/wiki/concepts/alpha--huge.md").exists()

    malformed = _approve_hostile(
        pub,
        "mbad00000000000000000000000001",
        "projects/alpha/wiki/concepts/alpha--bad.md",
        None,
        page(name="bad", body=secret),
        "alpha--bad",
    )
    malformed["batches"][0]["proposal"]["changes"][0]["leak"] = secret
    malformed["plan_hash"] = migration_digest(malformed)
    malformed["approval"] = malformed["plan_hash"]
    with pytest.raises(MemoryError, match="^migration_plan_invalid$") as invalid:
        apply_migration(pub, malformed)
    assert str(invalid.value) == "migration_plan_invalid"
    assert secret not in str(invalid.value)
    assert not (pub.vault / "projects/alpha/wiki/concepts/alpha--bad.md").exists()


def test_apply_preserves_frontmatter_except_publisher_stamp(tmp_path, monkeypatch):
    pub = publisher(tmp_path)
    raw = pub.snapshot("alpha", "evidência sintética")
    raw_before = (pub.vault / raw).read_bytes()
    legacy = render_page(
        _frontmatter(
            page_id="alpha--noted",
            note="manter-esta-nota",
            sources=[f"[[{raw.removesuffix('.md')}]]"],
            aliases=["Nota"],
        ),
        "Texto legado.\n",
    )
    pub.publish(proposal(legacy, operation="noted", name="noted"))
    path = pub.vault / "projects/alpha/wiki/concepts/alpha--noted.md"
    before_meta, before_body = parse_page(path.read_text())

    class Later(date):
        @classmethod
        def today(cls):
            return cls(2030, 1, 2)

    monkeypatch.setattr("oraculo.memory.publish.date", Later)
    applied = apply_migration(pub, approve(plan_migration(pub, "alpha")))
    stored_meta, stored_body = parse_page(path.read_text())
    assert stored_body == before_body == "Texto legado.\n"
    assert stored_meta["knowledge_schema"] == 1
    assert stored_meta["claims"] == []
    assert stored_meta["note"] == "manter-esta-nota"
    assert stored_meta["sources"] == before_meta["sources"]
    assert stored_meta["tags"] == []
    assert stored_meta["aliases"] == ["Nota"]
    assert stored_meta["created"] == before_meta["created"]
    assert stored_meta["page_id"] == "alpha--noted"
    assert stored_meta["reviewed"] is False
    assert stored_meta["updated"] == "2030-01-02"
    assert applied["state"] == "confirmed"
    assert (pub.vault / raw).read_bytes() == raw_before
    journal = json.loads(
        (pub.journals / f"{applied['receipts'][0]['operation_id']}.json").read_text()
    )
    assert journal["before"]["projects/alpha/wiki/concepts/alpha--noted.md"]


def test_n_memory_compiler_2_replay_across_midnight_returns_receipts(
    tmp_path, monkeypatch
):
    """N-memory-compiler-2: a later-day replay returns the existing receipts."""

    class Clock(date):
        current = date(2030, 1, 2)

        @classmethod
        def today(cls):
            return cls.current

    monkeypatch.setattr("oraculo.memory.publish.date", Clock)
    pub = publisher(tmp_path)
    raw = pub.snapshot("alpha", "evidência sintética")
    legacy = render_page(
        _frontmatter(
            page_id="alpha--noted",
            note="manter-esta-nota",
            sources=[f"[[{raw.removesuffix('.md')}]]"],
            aliases=["Nota"],
        ),
        "Texto legado.\n",
    )
    pub.publish(proposal(legacy, operation="noted", name="noted"))
    plan = approve(plan_migration(pub, "alpha"))
    applied = apply_migration(pub, plan)
    assert applied["state"] == "confirmed"
    assert applied["receipts"]
    Clock.current = date(2030, 1, 3)
    replay = apply_migration(pub, plan)
    assert replay["state"] == "confirmed"
    assert replay["receipts"] == applied["receipts"]
    assert replay["pending_review"] == applied["pending_review"]
    path = pub.vault / "projects/alpha/wiki/concepts/alpha--noted.md"
    meta, body = parse_page(path.read_text(encoding="utf-8"))
    assert meta["updated"] == "2030-01-02"
    assert body == "Texto legado.\n"
    meta["updated"] = "2030-02-01"
    path.write_text(render_page(meta, body), encoding="utf-8")
    _retarget(pub, path)
    later = apply_migration(pub, plan)
    assert later["state"] == "confirmed"
    meta["updated"] = "2029-12-31"
    path.write_text(render_page(meta, body), encoding="utf-8")
    _retarget(pub, path)
    with pytest.raises(MemoryError, match="^migration_identity_changed$"):
        apply_migration(pub, plan)


def _retarget(pub, path):
    relative = path.relative_to(pub.vault).as_posix()
    catalog = json.loads(pub.catalog.read_bytes().decode("utf-8"))
    catalog[relative] = digest(path.read_bytes())
    pub.catalog.write_bytes(json.dumps(catalog, sort_keys=True).encode())
