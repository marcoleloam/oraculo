import concurrent.futures
import json

import pytest
from test_memory_schema import page, proposal, publisher

from oraculo.memory.cycles import MemoryCycles
from oraculo.memory.schema import ConflictError, MemoryError, digest
from oraculo.state import State


@pytest.fixture
def cycles(tmp_path):
    state = State(tmp_path / "cycles.sqlite")
    result = MemoryCycles(state, publisher(tmp_path))
    yield result
    state.close()


def start(cycles, key="run-1", project="alpha"):
    return cycles.begin(project, key, "use learned result", {"objective_id": "obj-1"})


def test_empty_delivery_publication_then_next_task_retrieves(cycles):
    item = start(cycles)
    assert item["read_state"] == "empty"
    assert item["read_receipt"]["base_commit"]
    assert item["read_receipt"]["sha256"]
    assert item["read_receipt"]["sources"] == []
    cycles.bind(item["id"], {"thread_id": "thread-1", "run_id": "r1"})
    delivery = cycles.delivered(item["id"], "thread-1", "r1", {"prompt_hash": "abc"})
    assert delivery["deliveries"][0]["kind"] == "transport_delivery_only"
    finished = cycles.finish(
        item["id"],
        "succeeded",
        {
            "decisions": ["use learned result with project isolation"],
            "checks": ["pytest passed"],
            "transcript": "excluded transcript",
            "secret": "excluded secret",
            "summary": "Implementation [check](private/file) [[unsafe]]",
        },
    )
    assert finished["work_status"] == "succeeded"
    assert finished["write_state"] == "pending"
    assert finished["write_receipt"] is None
    saved = cycles.publish_one(item["id"])
    assert saved["write_state"] == "saved", saved.get("write_error")
    assert saved["write_receipt"]["state"] == "confirmed"
    assert saved["write_receipt"]["operation_id"] == item["id"]
    assert saved["write_receipt"]["at"] >= finished["finished_at"]
    raw = next(iter(saved["proposal"]["source_hashes"]))
    text = (cycles.publisher.vault / raw).read_text()
    assert digest(text) == saved["report_sha256"]
    assert "excluded transcript" not in text and "excluded secret" not in text
    assert json.loads(text)["identity"]["thread_id"] == "thread-1"
    next_task = start(cycles, "run-2")
    assert next_task["read_state"] == "found"
    assert "use learned result" in json.dumps(next_task["context"])
    assert next_task["read_receipt"]["base_commit"] == saved["write_receipt"]["commit"]
    assert (
        cycles.state.connection.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0
    )


@pytest.mark.parametrize("status", ["succeeded", "failed", "stopped"])
def test_all_terminal_statuses_are_history_not_false_success(cycles, status):
    item = start(cycles)
    cycles.finish(item["id"], status, {"limitations": ["delivery not asserted"]})
    saved = cycles.publish_one(item["id"])
    assert saved["write_state"] == "saved", saved.get("write_error")
    assert saved["report"]["work_status"] == status
    assert saved["work_status"] == status


def test_idempotency_and_causal_identity(cycles):
    first = start(cycles)
    assert start(cycles) == first
    with pytest.raises(ConflictError, match="key_reused"):
        cycles.begin("alpha", "run-1", "different", {"objective_id": "obj-1"})
    cycles.bind(first["id"], {"thread_id": "t1"})
    with pytest.raises(ConflictError, match="identity_rebound"):
        cycles.bind(first["id"], {"thread_id": "t2"})
    with pytest.raises(ConflictError, match="delivery_identity"):
        cycles.delivered(first["id"], "t2", None, {})
    delivery = cycles.delivered(first["id"], "t1", None, "prompt-hash")
    assert cycles.delivered(first["id"], "t1", None, "prompt-hash") == delivery
    another_delivery = cycles.delivered(first["id"], "t1", None, "other-hash")
    assert len(another_delivery["deliveries"]) == 2
    finished = cycles.finish(first["id"], "failed", {"summary": "failure"})
    assert cycles.finish(first["id"], "failed", {"summary": "failure"}) == finished
    with pytest.raises(ConflictError, match="finish_reused"):
        cycles.finish(first["id"], "succeeded", {"summary": "failure"})
    with pytest.raises(ConflictError, match="read_after_finish"):
        cycles.retry_read(first["id"])
    with pytest.raises(ConflictError, match="identity_after_finish"):
        cycles.bind(first["id"], {"task_id": "late"})
    saved = cycles.publish_one(first["id"])
    head = cycles.publisher.git.head()
    assert cycles.publish_one(first["id"]) == saved
    assert cycles.publisher.git.head() == head
    assert not cycles.pending()


def test_unavailable_read_is_explicit_and_retryable(cycles, monkeypatch):
    original = cycles.publisher._check_integrity
    monkeypatch.setattr(
        cycles.publisher,
        "_check_integrity",
        lambda: (_ for _ in ()).throw(OSError("offline")),
    )
    first = start(cycles)
    assert first["read_state"] == "unavailable"
    assert first["read_receipt"]["sha256"] is None
    assert first["context"] == {}
    assert "offline" in first["read_receipt"]["error"]
    assert (
        start(cycles) == first
    )  # begin does not disguise changed context as same read
    monkeypatch.setattr(cycles.publisher, "_check_integrity", original)
    retried = cycles.retry_read(first["id"])
    assert retried["read_state"] == "empty"
    assert [read["state"] for read in retried["read_history"]] == [
        "unavailable",
        "empty",
    ]


def test_scope_is_private_plus_shared_and_never_other_project(cycles):
    cycles.publisher.publish(proposal(page(body="alpha private")))
    cycles.publisher.publish(
        proposal(
            page(project="beta", body="beta confidential"),
            project="beta",
            operation="beta-op",
        )
    )
    cycles.publisher.publish(
        proposal(
            page(project="shared", body="allowed shared"),
            project="shared",
            operation="shared-op",
        )
    )
    first = start(cycles)
    content = json.dumps(first["context"])
    assert "alpha private" in content and "allowed shared" in content
    assert "beta confidential" not in content
    with pytest.raises(MemoryError, match="private_project"):
        start(cycles, project="shared")
    cycles.finish(first["id"], "succeeded", {"summary": "private learned result"})
    saved = cycles.publish_one(first["id"])
    assert all(
        path.startswith("projects/alpha/") for path in saved["write_receipt"]["paths"]
    )
    assert len(cycles.list("alpha")) == 1 and cycles.list("beta") == []


def test_publication_unconfirmed_preserves_report_and_retry(cycles, monkeypatch):
    first = start(cycles)
    finished = cycles.finish(first["id"], "succeeded", {"results": ["done"]})
    original = cycles.publisher.publish
    monkeypatch.setattr(
        cycles.publisher, "publish", lambda proposal: {"state": "pending"}
    )
    failed = cycles.publish_one(first["id"])
    assert failed["write_state"] == "failed"
    assert failed["write_receipt"] is None
    assert failed["report"] == finished["report"]
    assert cycles.publish_pending() == []
    assert cycles.pending("obj-1")[0]["id"] == first["id"]
    monkeypatch.setattr(cycles.publisher, "publish", original)
    assert cycles.retry_write(first["id"])["write_state"] == "saved"


def test_crash_after_publisher_commit_replays_without_duplicate(cycles, monkeypatch):
    first = start(cycles)
    cycles.finish(first["id"], "succeeded", {"summary": "durable"})
    original = cycles.publisher.publish

    def crash(proposal):
        original(proposal)
        raise KeyboardInterrupt("process exited before DB receipt")

    monkeypatch.setattr(cycles.publisher, "publish", crash)
    with pytest.raises(KeyboardInterrupt):
        cycles.publish_one(first["id"])
    head = cycles.publisher.git.head()
    db_path = cycles.state.path
    cycles.state.close()
    cycles.state = State(db_path)
    restarted = MemoryCycles(cycles.state, cycles.publisher)
    monkeypatch.setattr(cycles.publisher, "publish", original)
    saved = restarted.publish_pending()[0]
    assert saved["write_state"] == "saved"
    assert saved["write_receipt"]["commit"] == head
    assert cycles.publisher.git.head() == head
    log = (cycles.publisher.vault / "projects/alpha/wiki/log.md").read_text()
    assert log.count("operation_id: " + first["id"]) == 1


def test_conflict_is_preserved_and_retry_recovers(cycles):
    cycles.publisher.publish(proposal())
    path = cycles.publisher.vault / proposal()["changes"][0]["path"]
    original = path.read_bytes()
    first = start(cycles)
    cycles.finish(first["id"], "failed", {"summary": "work failed"})
    path.write_text("external edit must not be overwritten")
    failed = cycles.publish_one(first["id"])
    assert failed["write_state"] == "failed"
    assert "external_change" in failed["write_error"]
    assert path.read_text() == "external edit must not be overwritten"
    path.write_bytes(original)  # recovery of test fixture, not a production write
    assert cycles.retry_write(first["id"])["write_state"] == "saved"


def test_concurrent_publishers_dedupe_same_cycle_and_preserve_distinct_cycles(cycles):
    same = start(cycles)
    cycles.finish(same["id"], "succeeded", {"summary": "same-cycle"})
    other = start(cycles, "other-run")
    cycles.finish(other["id"], "failed", {"summary": "other-cycle"})

    def publish(cycle_id):
        state = State(cycles.state.path)
        try:
            return MemoryCycles(state, cycles.publisher).publish_one(cycle_id)
        finally:
            state.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        results = list(executor.map(publish, [same["id"], same["id"], other["id"]]))
    assert [result["write_state"] for result in results] == ["saved"] * 3
    assert (
        results[0]["write_receipt"]["commit"] == results[1]["write_receipt"]["commit"]
    )
    log = (cycles.publisher.vault / "projects/alpha/wiki/log.md").read_text()
    assert log.count("operation_id: " + same["id"]) == 1
    assert log.count("operation_id: " + other["id"]) == 1
    assert len(cycles.publisher.git.run("status", "--porcelain").stdout.strip()) == 0


def test_pending_limit_and_backoff_does_not_starve_other_cycle(cycles, monkeypatch):
    first, second = start(cycles), start(cycles, "run-2")
    for item in [first, second]:
        cycles.finish(item["id"], "stopped", {"summary": "cancelled"})
    original = cycles.publisher.snapshot
    monkeypatch.setattr(
        cycles.publisher,
        "snapshot",
        lambda *args: (_ for _ in ()).throw(OSError("offline")),
    )
    assert cycles.publish_pending(limit=1)[0]["id"] == first["id"]
    monkeypatch.setattr(cycles.publisher, "snapshot", original)
    assert cycles.publish_pending(limit=1)[0]["id"] == second["id"]
    assert cycles.get(first["id"])["write_state"] == "failed"
    assert cycles.get(second["id"])["write_state"] == "saved"


def test_begin_replay_after_bind_and_readonly_settlement_lookup(cycles):
    first = start(cycles)
    cycles.bind(first["id"], {"attempt_id": "attempt-1", "thread_id": "t1"})
    assert start(cycles)["identity"]["thread_id"] == "t1"
    other = start(cycles, project="beta")
    assert first["id"] != other["id"]
    cycles.finish(first["id"], "succeeded", {"summary": "pending publication"})
    read_only = MemoryCycles.for_state(cycles.state)
    assert read_only.publisher is None
    assert len(read_only.for_objective("obj-1")) == 2
    assert len(read_only.pending("obj-1")) == 1


def test_read_unavailable_can_finish_and_publish_honest_history(cycles, monkeypatch):
    original = cycles.publisher._check_integrity
    monkeypatch.setattr(
        cycles.publisher,
        "_check_integrity",
        lambda: (_ for _ in ()).throw(OSError("offline")),
    )
    first = start(cycles)
    monkeypatch.setattr(cycles.publisher, "_check_integrity", original)
    cycles.finish(
        first["id"],
        "stopped",
        {"limitations": ["Memory unavailable before cancellation"]},
    )
    saved = cycles.publish_one(first["id"])
    assert saved["write_state"] == "saved"
    assert saved["report"]["read_state"] == "unavailable"
    assert saved["report"]["read_receipt"]["sha256"] is None
    assert saved["report"]["work_status"] == "stopped"


def test_crash_during_publisher_transaction_recovers(cycles, monkeypatch):
    import oraculo.memory.publish as publish_module

    first = start(cycles)
    cycles.finish(first["id"], "failed", {"summary": "recover journal"})
    original = publish_module.atomic_write
    interrupted = False

    def crash(path, data):
        nonlocal interrupted
        if "/wiki/sources/" in str(path) and not interrupted:
            interrupted = True
            raise OSError("simulated disk error")
        return original(path, data)

    monkeypatch.setattr(publish_module, "atomic_write", crash)
    failed = cycles.publish_one(first["id"])
    assert failed["write_state"] == "failed"
    assert failed["proposal"]["operation_id"] == first["id"]
    monkeypatch.setattr(publish_module, "atomic_write", original)
    saved = cycles.retry_write(first["id"])
    assert saved["write_state"] == "saved", saved.get("write_error")
    log = (cycles.publisher.vault / "projects/alpha/wiki/log.md").read_text()
    assert log.count("operation_id: " + first["id"]) == 1


def test_concurrent_begin_returns_one_stable_receipt(cycles):
    def begin(_):
        state = State(cycles.state.path)
        try:
            return start(MemoryCycles(state, cycles.publisher))
        finally:
            state.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        results = list(executor.map(begin, range(3)))
    assert len({item["id"] for item in results}) == 1
    stable = cycles.get(results[0]["id"])
    assert stable["read_state"] == "empty"
    assert len(stable["read_history"]) == 1
    for item in results:
        assert item["read_state"] == "empty"
        assert item["read_receipt"] == stable["read_receipt"]


def test_delivery_after_read_retry_keeps_versions_and_rejects_artifact_reuse(
    cycles, monkeypatch
):
    original = cycles.publisher._check_integrity
    monkeypatch.setattr(
        cycles.publisher,
        "_check_integrity",
        lambda: (_ for _ in ()).throw(OSError("offline")),
    )
    first = start(cycles)
    first_delivery = cycles.delivered(first["id"], "t1", "r1", "unavailable-artifact")
    unavailable = first_delivery["delivery_receipt"]
    assert unavailable["context_sha256"] is None
    monkeypatch.setattr(cycles.publisher, "_check_integrity", original)
    retried = cycles.retry_read(first["id"])
    assert retried["read_state"] == "empty"
    with pytest.raises(ConflictError, match="delivery_reused"):
        cycles.delivered(first["id"], "t1", "r1", "unavailable-artifact")
    delivered = cycles.delivered(first["id"], "t1", "r1", "recovered-artifact")
    assert delivered["deliveries"][0] == unavailable
    assert (
        delivered["delivery_receipt"]["context_sha256"]
        == retried["read_receipt"]["sha256"]
    )
    assert len(delivered["deliveries"]) == 2
    assert cycles.delivered(first["id"], "t1", "r1", "recovered-artifact") == delivered
    finished = cycles.finish(first["id"], "succeeded", {"summary": "recovered"})
    assert finished["report"]["deliveries"] == delivered["deliveries"]


def test_unavailable_retry_versions_receipt_even_with_null_context_hash(
    cycles, monkeypatch
):
    monkeypatch.setattr(
        cycles.publisher,
        "_check_integrity",
        lambda: (_ for _ in ()).throw(OSError("offline")),
    )
    first = start(cycles)
    first_delivery = cycles.delivered(first["id"], "t1", "r1", "first-unavailable")
    cycles.retry_read(first["id"])
    with pytest.raises(ConflictError, match="delivery_reused"):
        cycles.delivered(first["id"], "t1", "r1", "first-unavailable")
    second = cycles.delivered(first["id"], "t1", "r1", "second-unavailable")
    assert second["delivery_receipt"]["context_sha256"] is None
    assert (
        second["delivery_receipt"]["read_receipt_sha256"]
        != first_delivery["delivery_receipt"]["read_receipt_sha256"]
    )


def test_oversized_report_preserves_terminal_work_and_does_not_block_others(cycles):
    first, other = start(cycles), start(cycles, "other-run")
    evidence = {"summary": "x" * (256 * 1024)}
    blocked = cycles.finish(first["id"], "succeeded", evidence)
    assert blocked["work_status"] == "succeeded"
    assert blocked["write_state"] == "blocked"
    assert "report" not in blocked and blocked["write_receipt"] is None
    rejection = blocked["finish_rejection"]
    assert rejection["error"] == "memory_report_too_large"
    assert rejection["bytes"] > 256 * 1024
    assert rejection["status"] == "succeeded"
    assert cycles.finish(first["id"], "succeeded", evidence) == blocked
    with pytest.raises(ConflictError, match="finish_reused"):
        cycles.finish(first["id"], "failed", {"summary": "wrong status"})
    cycles.finish(other["id"], "failed", {"summary": "independent report"})
    assert [item["id"] for item in cycles.publish_pending()] == [other["id"]]
    assert cycles.get(other["id"])["write_state"] == "saved"
    assert cycles.get(first["id"])["write_state"] == "blocked"
    corrected = cycles.finish(
        first["id"],
        "succeeded",
        {"summary": "bounded report with source retained by caller"},
    )
    assert corrected["write_state"] == "pending"
    assert corrected["finished_at"] == blocked["finished_at"]
    assert corrected["report"]["initial_finish_rejection"] == rejection
    assert cycles.publish_one(first["id"])["write_state"] == "saved"


def test_unstructured_report_is_explicitly_blocked_and_correctable(cycles):
    first = start(cycles)
    blocked = cycles.finish(first["id"], "stopped", "unstructured result")
    assert blocked["work_status"] == "stopped"
    assert blocked["write_state"] == "blocked"
    assert blocked["finish_rejection"]["error"] == "memory_report_must_be_structured"
    assert cycles.publish_pending() == []
    corrected = cycles.finish(
        first["id"],
        "stopped",
        {"summary": "cancelled", "evidence": "unstructured result"},
    )
    assert corrected["write_state"] == "pending"
    assert cycles.publish_one(first["id"])["write_state"] == "saved"


def test_begin_recovers_initial_reservation_after_crash_without_sql_io_lock(
    cycles, monkeypatch
):
    retry = cycles.retry_read
    monkeypatch.setattr(
        cycles,
        "retry_read",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            KeyboardInterrupt("crash after reservation")
        ),
    )
    with pytest.raises(KeyboardInterrupt):
        start(cycles)
    reserved = cycles.list()[0]
    assert reserved["read_receipt"]["error"] == "memory_read_pending"
    original_check = cycles.publisher._check_integrity

    def check():
        assert not cycles.state.connection.in_transaction
        return original_check()

    monkeypatch.setattr(cycles.publisher, "_check_integrity", check)
    monkeypatch.setattr(cycles, "retry_read", retry)
    recovered = start(cycles)
    assert recovered["id"] == reserved["id"]
    assert recovered["read_state"] == "empty"
    assert len(recovered["read_history"]) == 1


def test_concurrent_begin_failure_returns_same_genuine_unavailable_receipt(
    cycles, monkeypatch
):
    import threading

    barrier = threading.Barrier(3)
    retry = MemoryCycles.retry_read

    def synchronized_retry(self, *args, **kwargs):
        assert not self.state.connection.in_transaction
        barrier.wait(timeout=5)
        return retry(self, *args, **kwargs)

    monkeypatch.setattr(MemoryCycles, "retry_read", synchronized_retry)
    monkeypatch.setattr(
        cycles.publisher,
        "_check_integrity",
        lambda: (_ for _ in ()).throw(OSError("offline")),
    )

    def begin(_):
        state = State(cycles.state.path)
        try:
            return start(MemoryCycles(state, cycles.publisher))
        finally:
            state.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        results = list(executor.map(begin, range(3)))
    assert all(item["read_receipt"] == results[0]["read_receipt"] for item in results)
    assert all(item["read_receipt"]["error"] == "OSError:offline" for item in results)
    assert len(cycles.get(results[0]["id"])["read_history"]) == 1


def test_wiki_summary_is_readable_and_full_evidence_stays_in_source(cycles):
    first = start(cycles)
    cycles.finish(
        first["id"],
        "succeeded",
        {
            "summary": "Implementation summary",
            "decisions": ["Use explicit provenance"],
            "changes": ["Read before dispatch"],
            "results": "passed",
            "limitations": ["[[unsafe]] private/reviews/file"],
            "pending": ["Activation"],
            "next_steps": ["Integrate"],
            "checks": ["CHECK_DETAILS_ONLY_IN_RAW"],
            "evidence": {"trace": "EVIDENCE_ONLY_IN_RAW"},
        },
    )
    saved = cycles.publish_one(first["id"])
    assert saved["write_state"] == "saved", saved.get("write_error")
    wiki = saved["proposal"]["changes"][0]["content"]
    raw = next(iter(saved["proposal"]["source_hashes"]))
    source = (cycles.publisher.vault / raw).read_text()
    assert "## Decisões\n\n- Use explicit provenance" in wiki
    assert "## Próximos passos" in wiki
    assert (
        "CHECK_DETAILS_ONLY_IN_RAW" not in wiki and "EVIDENCE_ONLY_IN_RAW" not in wiki
    )
    assert "CHECK_DETAILS_ONLY_IN_RAW" in source and "EVIDENCE_ONLY_IN_RAW" in source
    assert "[[unsafe]]" not in wiki and "[[unsafe]]" in source
    assert f"[[{raw}]]" in wiki


def test_wiki_summary_is_bounded_without_truncating_raw_source(cycles):
    first = start(cycles)
    summary = "meaningful result " * 2000
    cycles.finish(first["id"], "succeeded", {"summary": summary})
    saved = cycles.publish_one(first["id"])
    assert saved["write_state"] == "saved", saved.get("write_error")
    wiki = saved["proposal"]["changes"][0]["content"]
    assert len(wiki) < 14000
    assert "Síntese abreviada; consulte a fonte" in wiki
    raw = next(iter(saved["proposal"]["source_hashes"]))
    assert (
        json.loads((cycles.publisher.vault / raw).read_text())["evidence"]["summary"]
        == summary
    )


def test_cancelled_before_start_records_no_query_and_publishes_history(
    cycles, monkeypatch
):
    monkeypatch.setattr(
        cycles.publisher, "_check_integrity", lambda: pytest.fail("must not query")
    )
    item = cycles.begin(
        "alpha",
        "cancelled-job",
        "query",
        {"objective_id": "cancelled"},
        skip_read_reason="cancelled_before_start",
    )
    assert item["read_state"] == "unavailable"
    assert item["read_receipt"]["error"] == "cancelled_before_start"
    assert item["read_receipt"]["sha256"] is None
    assert item["read_history"] == []
    assert item["context"] == {}
    monkeypatch.undo()
    cycles.finish(item["id"], "stopped", {"summary": "Cancelled before execution"})
    saved = cycles.publish_one(item["id"])
    assert saved["write_state"] == "saved"
    assert saved["report"]["work_status"] == "stopped"
    assert saved["report"]["read_receipt"]["error"] == "cancelled_before_start"


def test_cancel_initial_reservation_never_calls_publisher_even_inside_transaction(
    cycles, monkeypatch
):
    retry = cycles.retry_read
    monkeypatch.setattr(
        cycles,
        "retry_read",
        lambda *args, **kwargs: (_ for _ in ()).throw(KeyboardInterrupt("reserved")),
    )
    with pytest.raises(KeyboardInterrupt):
        start(cycles)
    monkeypatch.setattr(cycles, "retry_read", retry)
    monkeypatch.setattr(
        cycles.publisher,
        "locked",
        lambda: pytest.fail("cancel must not acquire vault lock"),
    )
    with cycles.state.transaction():
        cancelled = cycles.begin(
            "alpha",
            "run-1",
            "use learned result",
            {"objective_id": "obj-1"},
            skip_read_reason="cancelled_before_start",
        )
        assert cancelled["read_receipt"]["error"] == "cancelled_before_start"
        assert cancelled["read_receipt"]["sha256"] is None
        assert cancelled["context"] == {}
        cycles.finish(cancelled["id"], "stopped", {"summary": "Cancelled"})
    assert cycles.get(cancelled["id"])["work_status"] == "stopped"


def test_cancel_preserves_completed_query(cycles, monkeypatch):
    first = start(cycles)
    monkeypatch.setattr(
        cycles.publisher, "locked", lambda: pytest.fail("must not query again")
    )
    cancelled = cycles.begin(
        "alpha",
        "run-1",
        "use learned result",
        {"objective_id": "obj-1"},
        skip_read_reason="cancelled_before_start",
    )
    assert cancelled["read_receipt"] == first["read_receipt"]
    assert cancelled["context"] == first["context"]


def test_inflight_read_cannot_overwrite_cancelled_terminal_receipt(cycles, monkeypatch):
    original = cycles.publisher._check_integrity
    terminal = None

    def cancel_during_read():
        nonlocal terminal
        cancelled = cycles.begin(
            "alpha",
            "run-1",
            "use learned result",
            {"objective_id": "obj-1"},
            skip_read_reason="cancelled_before_start",
        )
        terminal = cycles.finish(
            cancelled["id"], "stopped", {"summary": "Cancelled while preparing context"}
        )
        return original()

    monkeypatch.setattr(cycles.publisher, "_check_integrity", cancel_during_read)
    result = start(cycles)
    assert result == terminal
    assert result["work_status"] == "stopped"
    assert result["read_receipt"]["error"] == "cancelled_before_start"
    assert result["report"]["read_receipt"] == result["read_receipt"]
    assert result["context"] == {} and result["read_history"] == []
