import json

import pytest

from oraculo.presentation import (
    PHASES,
    parse_return_message,
    return_message,
    summary_markdown,
    task_view,
    thread_url,
)
from oraculo.protocol import is_control_event
from oraculo.task_receipts import present
from oraculo.task_store import TaskStore


def create(store, **request):
    return store.create(
        {"thread_id": "origin", "run_id": "run"},
        "key",
        {"title": "Importar notas", **request},
    )


@pytest.mark.parametrize("phase", list(PHASES))
def test_human_phase_action_timestamp_and_no_invented_eta(phase):
    view = task_view(
        {
            "id": "task",
            "request": {"title": "Importar notas"},
            "project": {"project_path": "/demo/Jardim"},
            "phase": phase,
            "observed_at": 1,
            "child": {"childThreadId": "child"},
        }
    )
    text = summary_markdown(view)
    assert "Importar notas" in text and "Jardim" in text
    assert "Última atualização observada" in text and "Próxima ação" in text
    assert "t3-thread://v1/child" in text
    assert "%" not in text and "ETA" not in text
    assert view["phase"] != "Estado não reconhecido"


def test_readable_envelope_roundtrip_and_replay_after_lost_ack(tmp_path):
    path = tmp_path / "ledger.db"
    store = TaskStore(path)
    task = create(store)
    ident = task["id"]
    store.update(
        ident,
        "executing",
        {"progress": "safe"},
        project={"project_path": "/demo/Jardim"},
    )
    event = store.pending_returns(ident)[-1]
    frozen = store.delivery(event)
    message = return_message(frozen)
    assert message.startswith("**Importar notas")
    assert "<details>" in message
    assert parse_return_message(message) == frozen
    assert is_control_event(message)
    assert frozen["refresh_required"] is True
    store.update(
        ident,
        "blocked",
        {"reason": "technical-private"},
        blocked_reason="technical-private",
    )
    store.close()
    store = TaskStore(path)
    assert return_message(store.delivery(event)) == message
    assert "technical-private" not in summary_markdown(task_view(store.get(ident)))
    store.acknowledge_return(event["id"], {"threadId": "origin", "messageId": "ack"})
    assert all(e["id"] != event["id"] for e in store.pending_returns(ident))
    store.close()


def test_json_format_and_old_cached_delivery_remain_pure_json(tmp_path):
    store = TaskStore(tmp_path / "ledger.db")
    task = create(store, return_format="json")
    event = store.pending_returns(task["id"])[0]
    delivery = store.delivery(event)
    assert json.loads(return_message(delivery)) == delivery
    old = {"marker": "ORACULO_CONTROL_V1", "event_id": "old", "task_id": task["id"]}
    store.db.execute(
        "UPDATE task_returns SET delivery=? WHERE id=?", (json.dumps(old), event["id"])
    )
    assert (
        return_message(store.delivery(event))
        == '{"event_id":"old","marker":"ORACULO_CONTROL_V1","task_id":"'
        + task["id"]
        + '"}'
    )
    store.close()


def test_old_task_without_format_keeps_json(tmp_path):
    store = TaskStore(tmp_path / "ledger.db")
    task = create(store)
    task.pop("return_format")
    store.db.execute(
        "UPDATE managed_tasks SET payload=? WHERE id=?", (json.dumps(task), task["id"])
    )
    assert (
        json.loads(
            return_message(store.delivery(store.pending_returns(task["id"])[0]))
        )["return_format"]
        == "json"
    )
    store.close()


def test_untrusted_title_cannot_escape_protocol_html(tmp_path):
    store = TaskStore(tmp_path / "ledger.db")
    task = create(store, title="</pre><script>alert(1)</script> ``` [[oops]]")
    delivery = store.delivery(store.pending_returns(task["id"])[0])
    text = return_message(delivery)
    assert "<script>" not in text
    assert parse_return_message(text) == delivery
    assert text.count("<pre ") == 1
    store.close()


def test_duplicate_out_of_order_and_newer_event_need_fresh_status():
    current = dict(id="task", phase="executing", snapshot_revision=4)
    event = dict(event_id="e4", task_id="task", snapshot_revision=4, snapshot=current)
    seen = set()
    assert present(current, event, seen)["presentation"]["phase"] == "Em execução"
    assert present(current, event, seen)["presentation"] is None
    older = dict(
        event_id="e3", snapshot_revision=3, snapshot=dict(current, snapshot_revision=3)
    )
    assert present(current, older, seen)["presentation"] is None
    newer = dict(
        event_id="e5", snapshot_revision=5, snapshot=dict(current, snapshot_revision=5)
    )
    assert present(current, newer, seen)["refresh_required"]
    assert "e5" not in seen
    assert present(dict(current, snapshot_revision=5), newer, seen)["presentation"]
    with pytest.raises(ValueError, match="task_mismatch"):
        present(dict(current, id="other"), event, set())


def test_latest_progress_and_known_blocker_have_human_explanation():
    view = task_view(
        dict(
            phase="executing",
            events=[
                dict(
                    phase="executing", proof={"progress": "Conferindo links das notas."}
                )
            ],
        )
    )
    assert view["activity"] == "Conferindo links das notas."
    blocked = task_view(
        dict(phase="blocked", blocked_reason="memory_write_unconfirmed")
    )
    assert "salvamento da memória" in blocked["blocker"]


@pytest.mark.parametrize("approved", [True, False])
def test_plan_success_does_not_claim_implementation_validated(approved):
    view = task_view(
        dict(
            phase="plan_approved" if approved else "awaiting_approval",
            deliverable_kind="plan",
            work_status="succeeded",
            approval_state="approved" if approved else "pending",
        )
    )
    assert "Planejamento" in view["result"]
    assert "Trabalho validado" not in view["result"]
    assert ("implementação ainda" if approved else "aprovação ainda") in view["result"]


@pytest.mark.parametrize("snapshot", [None, {}, {"id": "task", "snapshot_revision": 3}])
def test_incomplete_or_incoherent_event_is_not_presented(snapshot):
    current = dict(id="task", phase="executing", snapshot_revision=4)
    event = dict(event_id="e4", task_id="task", snapshot_revision=4, snapshot=snapshot)
    seen = set()
    result = present(current, event, seen)
    assert result == {"presentation": None, "refresh_required": True}
    assert "e4" not in seen


def test_top_level_event_identity_must_match_snapshot_and_current():
    current = dict(id="task", phase="executing", snapshot_revision=4)
    event = dict(event_id="e4", task_id="other", snapshot_revision=4, snapshot=current)
    with pytest.raises(ValueError, match="task_mismatch"):
        present(current, event, set())


@pytest.mark.parametrize(
    "thread", ["child-1", "thread:delegated-task:command%3Aexample:task"]
)
def test_real_thread_id_link_shape(thread):
    assert thread_url(thread) == "t3-thread://v1/" + thread


@pytest.mark.parametrize(
    "thread", ["javascript:alert(1)", "id\nnext", 'id"><script>', "id/../other"]
)
def test_unsafe_thread_id_rejected(thread):
    assert thread_url(thread) is None
