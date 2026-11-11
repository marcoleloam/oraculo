"""Rejected nightly proposals must not poison a later successful settlement."""

import pytest
from test_memory_compiler import publisher, snapshot

from oraculo.memory.compiler import DEFAULT_NIGHTLY_CONCEPT, compile_knowledge
from oraculo.memory.cycles import MemoryCycles
from oraculo.memory.schema import ConflictError
from oraculo.state import State


def test_rejected_cas_does_not_leave_unrepairable_required_cycle(tmp_path):
    pub = publisher(tmp_path)
    initial = snapshot(pub, "alpha", {"summary": "Página inicial."})
    compile_knowledge(
        pub, "alpha", [initial], concept=DEFAULT_NIGHTLY_CONCEPT, apply=True
    )
    selected = snapshot(pub, "alpha", {"summary": "Lote congelado."})
    stale = compile_knowledge(
        pub, "alpha", [selected], concept=DEFAULT_NIGHTLY_CONCEPT
    )["proposal"]
    concurrent = snapshot(pub, "alpha", {"summary": "Publicação concorrente válida."})
    compile_knowledge(
        pub, "alpha", [concurrent], concept=DEFAULT_NIGHTLY_CONCEPT, apply=True
    )
    state = State(tmp_path / "cycles.sqlite")
    cycles = MemoryCycles(state, pub)
    identity = {
        "attempt_id": "attempt",
        "job_id": "job",
        "objective_id": "objective",
        "thread_id": "child",
        "run_id": "run",
    }
    try:
        with pytest.raises(ConflictError):
            cycles.publish_nightly("alpha", identity, stale)
        fresh = compile_knowledge(
            pub, "alpha", [selected], concept=DEFAULT_NIGHTLY_CONCEPT
        )["proposal"]
        assert fresh["operation_id"] != stale["operation_id"]
        assert cycles.publish_nightly("alpha", identity, fresh)["state"] == "confirmed"
        required = cycles.for_objective("objective")
        assert len(required) == 1
        assert all(item["write_state"] == "saved" for item in required)
        assert cycles.pending("objective") == []
    finally:
        state.close()


def test_intent_is_durable_before_journal_and_no_report_raw_is_created(
    tmp_path, monkeypatch
):
    pub = publisher(tmp_path)
    raw = snapshot(pub, "alpha", {"summary": "Intenção antes de qualquer escrita."})
    proposal = compile_knowledge(pub, "alpha", [raw], concept=DEFAULT_NIGHTLY_CONCEPT)[
        "proposal"
    ]
    state = State(tmp_path / "cycles.sqlite")
    cycles = MemoryCycles(state, pub)
    identity = {
        "attempt_id": "attempt",
        "job_id": "job",
        "objective_id": "objective",
        "thread_id": "child",
        "run_id": "run",
    }
    original = pub._transaction
    observed = []

    def transaction(operation_id, *args, **kwargs):
        [intent] = cycles.pending("objective")
        assert intent["publication_proposal"] == proposal
        # A separately opened connection sees a committed intent before the journal.
        reader = State(tmp_path / "cycles.sqlite")
        try:
            assert (
                MemoryCycles.for_state(reader).get(intent["id"])["write_state"]
                == "pending"
            )
        finally:
            reader.close()
        observed.append(operation_id)
        return original(operation_id, *args, **kwargs)

    monkeypatch.setattr(pub, "_transaction", transaction)
    before = sorted((pub.vault / "projects/alpha/raw").glob("*.md"))
    try:
        assert (
            cycles.publish_nightly("alpha", identity, proposal)["state"] == "confirmed"
        )
        assert observed == [proposal["operation_id"]]
        assert sorted((pub.vault / "projects/alpha/raw").glob("*.md")) == before
    finally:
        state.close()
