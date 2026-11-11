"""Origin notices: newest state only; possibly delivered ones replay as sent."""

from test_managed_tasks import report, start, system  # noqa: F401
from test_notification_delivery import Host, create, engine

from oraculo.presentation import parse_return_message
from oraculo.task_store import TaskStore


def block(store, ident, reason):
    store.update(ident, "blocked", {"reason": reason}, blocked_reason=reason)


def dispositions(store, ident):
    return [
        (row["disposition"], row["delivery_status"], row.get("superseded", False))
        for row in store.history(ident)["returns"]
    ]


async def test_prepared_but_never_sent_block_is_superseded_by_the_resolution(
    tmp_path,
):
    store = TaskStore(tmp_path / "ledger.db")
    host = Host()
    ident = create(store)
    await engine(store, host).flush_returns(ident)
    block(store, ident, "children_still_running")
    # Frozen for sending, then the process stopped before any host call.
    (prepared,) = store.prepare_returns(ident)
    frozen = store.delivery(prepared)
    assert frozen["snapshot"]["phase"] == "blocked"
    store.update(ident, "validating", {"resolved": True})
    store.update(ident, "pr_pending", {"done": True}, work_status="succeeded")
    host.calls.clear()
    await engine(store, host).flush_returns(ident)
    (sent,) = host.calls
    message = parse_return_message(sent["message"])
    assert message["snapshot"]["phase"] == "pr_pending"
    assert sent["clientRequestId"] != prepared["id"]
    # History keeps the dropped notice, marked as covered by the newer one.
    rows = dispositions(store, ident)
    assert rows[1] == ("coalesced", "covered_by_confirmed", True)
    assert rows[-1] == ("intent", "host_confirmed", False)
    assert store.describe(ident)["pending_returns"] == 0
    assert (
        store.db.execute(
            "SELECT delivery FROM task_returns WHERE id=?", (prepared["id"],)
        ).fetchone()[0]
        is not None
    )
    store.close()


async def test_block_resolved_back_to_the_confirmed_meaning_sends_nothing(tmp_path):
    store = TaskStore(tmp_path / "ledger.db")
    host = Host()
    ident = create(store)
    store.update(ident, "executing", {"step": 1})
    await engine(store, host).flush_returns(ident)
    block(store, ident, "host_response_uncertain")
    store.prepare_returns(ident)  # intent frozen, never attempted
    store.update(ident, "executing", {"step": 1})
    host.calls.clear()
    await engine(store, host).flush_returns(ident)
    assert host.calls == []
    assert store.describe(ident)["pending_returns"] == 0
    assert {row[0] for row in dispositions(store, ident)[2:]} == {"suppressed"}
    store.close()


async def test_attempted_block_replays_unchanged_then_the_newest_state(tmp_path):
    store = TaskStore(tmp_path / "ledger.db")
    host = Host()
    ident = create(store)
    await engine(store, host).flush_returns(ident)
    block(store, ident, "children_still_running")
    host.lose_ack = True  # the host accepted it; the receipt was lost
    await engine(store, host).flush_returns(ident)
    first = host.calls[-1]
    store.update(ident, "validating", {"resolved": True})
    store.update(ident, "pr_pending", {"done": True}, work_status="succeeded")
    host.calls.clear()
    await engine(store, host).flush_returns(ident)
    replay, newest = host.calls
    assert replay == first  # same bytes, same key: it may have been delivered
    assert parse_return_message(newest["message"])["snapshot"]["phase"] == "pr_pending"
    assert [row[1] for row in dispositions(store, ident)].count("host_confirmed") == 3
    store.close()


async def test_frozen_delivery_from_before_the_attempt_column_replays(tmp_path):
    store = TaskStore(tmp_path / "ledger.db")
    host = Host()
    ident = create(store)
    await engine(store, host).flush_returns(ident)
    block(store, ident, "children_still_running")
    (prepared,) = store.prepare_returns(ident)
    frozen = store.delivery(prepared)
    # A ledger written by the previous version: frozen, attempt unknown.
    store.db.execute("UPDATE task_returns SET attempt=NULL")
    store.update(ident, "pr_pending", {"done": True}, work_status="succeeded")
    host.calls.clear()
    await engine(store, host).flush_returns(ident)
    assert parse_return_message(host.calls[0]["message"]) == frozen
    assert len(host.calls) == 2
    store.close()


async def test_only_the_newest_of_several_unsent_states_is_delivered(tmp_path):
    store = TaskStore(tmp_path / "ledger.db")
    host = Host()
    ident = create(store)
    await engine(store, host).flush_returns(ident)
    for reason in ("first_block", "second_block"):
        block(store, ident, reason)
        store.prepare_returns(ident)
    host.calls.clear()
    await engine(store, host).flush_returns(ident)
    (sent,) = host.calls
    assert parse_return_message(sent["message"])["snapshot"]["blocked_reason"] == (
        "second_block"
    )
    store.close()


async def test_operation_that_resolves_a_block_does_not_notify_the_block(
    system,  # noqa: F811
):
    host = system.host
    task = await start(system)
    child = task["child"]["childThreadId"]
    host.complete(child, pending=True)
    request = {"task_id": task["id"], "report": report(task)}
    # The first attempt blocks on a live descendant.
    blocked = await system.engine.command("finish", request)
    assert blocked["blocked_reason"] == "children_still_running"
    sent_before = dict(host.sends)
    block_keys = [
        row["return_id"]
        for row in system.store.history(task["id"])["returns"]
        if row["disposition"] == "intent"
    ]
    # Put the ledger in the state of a crash between freezing that notice
    # and sending it: prepared, never handed to the host.
    system.store.db.execute(
        "UPDATE task_returns SET receipt=NULL,attempt='prepared' WHERE id=?",
        (block_keys[-1],),
    )
    host.sends = {k: v for k, v in sent_before.items() if k != block_keys[-1]}
    host.threads["reviewer-" + child].update(status="completed", activeRunId=None)
    host.complete(child)
    done = await system.engine.command("finish", request)
    assert done["phase"] == "pr_pending" and done["work_status"] == "succeeded"
    new = [k for k in host.sends if k not in sent_before or k == block_keys[-1]]
    assert block_keys[-1] not in host.sends
    assert len(new) == 1
    message = parse_return_message(host.sends[new[0]]["arguments"]["message"])
    assert message["snapshot"]["phase"] == "pr_pending"
