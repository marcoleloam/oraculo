from oraculo.task_receipts import consume
from oraculo.task_store import TaskStore


def test_reordered_history_restart_and_lost_ack(tmp_path):
    path = tmp_path / "ledger.db"
    store = TaskStore(path)
    task = store.create(
        {"thread_id": "origin", "run_id": "run"}, "key", {"text": "work"}
    )
    ident = task["id"]
    for phase in ["launch_pending", "blocked", "executing"]:
        store.update(ident, phase, {"proof": phase})
    events = store.pending_returns(ident)
    deliveries = [store.delivery(e) for e in events]
    assert deliveries[1]["historical"]
    assert deliveries[-1]["snapshot"]["phase"] == "executing"
    store.close()
    store = TaskStore(path)
    assert store.delivery(events[0]) == deliveries[0]  # same host dedupe key/content
    state = None
    for event in [deliveries[-1], deliveries[1], deliveries[2], deliveries[0]]:
        state = consume(state, event)["snapshot"]
        assert state["phase"] == "executing"
    assert consume(state, {"phase": "blocked"})["refresh_required"]
    assert len(store.get(ident)["events"]) == 4
    store.close()
