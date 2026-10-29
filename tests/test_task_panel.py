"""Synthetic project view through the real daemon socket and loopback HTTP."""

import copy
import json
from contextlib import contextmanager
from http.client import HTTPConnection
from threading import Thread

import pytest

from oraculo.presentation import task_tracking
from oraculo.task_panel import PAGE, make_server, project_tasks
from oraculo.task_store import TaskStore


def row(**changes):
    return dict(
        id="demo-task",
        title="Revisar demo",
        phase="executing",
        origin={"project_id": "alpha", "thread_id": "origin"},
        child={"childThreadId": "child"},
        work_status="pending",
        observed={"status": "running", "workState": "working"},
        memory={
            "id": "demo-cycle",
            "write_state": "saved",
            "write_receipt": {
                "state": "confirmed",
                "operation_id": "demo-cycle",
                "commit": "a" * 40,
            },
        },
        **changes,
    )


@pytest.mark.parametrize(
    "phase,work,status,evidence,reason,state",
    [
        ("received", "pending", None, False, None, "waiting"),
        ("blocked", "pending", None, False, "managed_capacity_wait", "waiting"),
        ("executing", "pending", "queued", False, None, "waiting"),
        ("executing", "pending", "running", False, None, "running"),
        ("validating", "pending", "completed", False, None, "waiting"),
        ("executing", "pending", "completed", False, None, "waiting"),
        (
            "blocked",
            "pending",
            "running",
            False,
            "dirty_source_commit_selection_required",
            "blocked",
        ),
        ("executing", "pending", "interrupted", False, None, "blocked"),
        ("blocked", "failed", "completed", True, "work_not_successful", "blocked"),
        ("pr_pending", "succeeded", "completed", True, None, "completed"),
        ("pr_pending", "succeeded", "completed", False, None, "waiting"),
        ("awaiting_approval", "succeeded", "completed", True, None, "waiting"),
        ("memory_pending", "succeeded", "completed", True, None, "waiting"),
        ("validation_submitted", "succeeded", "completed", True, None, "waiting"),
        ("validating", "succeeded", "completed", True, None, "waiting"),
    ],
)
def test_tracking_distinguishes_run_terminal_from_verified_work(
    phase, work, status, evidence, reason, state
):
    task = row()
    task.update(
        phase=phase,
        work_status=work,
        observed={"status": status},
        completion_evidence=evidence,
        blocked_reason=reason,
    )
    view = task_tracking(task)
    assert view["state"] == state
    assert view["origin_url"] == "t3-thread://v1/origin"
    assert view["child_url"] == "t3-thread://v1/child"
    if reason:
        assert view["blocked_reason"] == reason


async def test_official_read_filters_resolved_and_early_blocked_tasks(running_service):
    service = running_service
    store = TaskStore(service.config.state_dir / "managed-tasks.sqlite3")
    early = store.create(
        dict(thread_id="alpha-origin", run_id="run", project_id="alpha"),
        "early",
        {"title": "Pedido bloqueado cedo", "text": "PRIVATE_BRIEF"},
    )
    store.update(
        early["id"],
        "blocked",
        {"reason": "project_error"},
        blocked_reason="project_error",
    )
    task = store.create(
        dict(thread_id="other-origin", run_id="run", project_id="other"),
        "resolved",
        {"title": "Progresso visível"},
    )
    store.update(
        task["id"],
        "executing",
        {"progress": "Conferindo fontes sintéticas"},
        project={"project_id": "alpha", "project_path": "/demo/alpha"},
        child={
            "childThreadId": "child",
            "launch_receipt": {"private": "PRIVATE_RECEIPT"},
        },
        observed={"status": "running", "workState": "working"},
        context={"private": "PRIVATE_CONTEXT"},
    )
    other = store.create(
        dict(thread_id="other", run_id="run", project_id="other"),
        "other",
        {"title": "PRIVATE_OTHER"},
    )
    store.update(
        other["id"], "executing", {"demo": True}, project={"project_id": "other"}
    )
    foreign = store.create(
        dict(thread_id="alpha", run_id="run", project_id="alpha"),
        "foreign",
        {"title": "PRIVATE_DESTINATION"},
    )
    store.update(
        foreign["id"], "executing", {"demo": True}, project={"project_id": "other"}
    )
    before = [store.get(t["id"]) for t in (early, task, other, foreign)]
    result = await project_tasks(service.config, "alpha")
    assert {t["title"] for t in result["tasks"]} == {
        "Pedido bloqueado cedo",
        "Progresso visível",
    }
    assert "Conferindo fontes sintéticas" in json.dumps(result, ensure_ascii=False)
    assert "PRIVATE_" not in json.dumps(result)
    assert result["source"] == "service" and result["fetched_at"]
    assert before == [store.get(t["id"]) for t in (early, task, other, foreign)]
    # No host call, task, memory cycle, admission or run was created by the view.
    assert service.memory_cycles.list() == []
    assert (
        service.state.connection.execute(
            "SELECT count(*) FROM managed_reservations"
        ).fetchone()[0]
        == 0
    )
    store.close()


async def test_no_offline_ledger_fallback_on_socket_failure(service_config):
    config, _ = service_config
    with pytest.raises(OSError):
        await project_tasks(config, "alpha")


@contextmanager
def server(config=None):
    with make_server(config, "alpha", 0) as http:
        thread = Thread(target=http.serve_forever)
        thread.start()
        try:
            yield http
        finally:
            http.shutdown()
            thread.join(timeout=2)
            assert not thread.is_alive()


def get(http, path="/", headers=None):
    connection = HTTPConnection("127.0.0.1", http.server_port, timeout=3)
    connection.request("GET", path, headers=headers or {})
    response = connection.getresponse()
    value = response.read().decode()
    status = response.status
    headers = dict(response.headers)
    connection.close()
    return status, value, headers


def test_http_loopback_fixed_project_no_files_no_parameters_and_no_cache(monkeypatch):
    import oraculo.task_panel as module

    calls = []

    async def query(config, project):
        calls.append(project)
        return {"project_title": "Alpha", "tasks": [], "source": "service"}

    monkeypatch.setattr(module, "project_tasks", query)
    with server() as http:
        assert http.server_address[0] == "127.0.0.1"
        status, body, headers = get(http)
        assert status == 200 and "Atualizar agora" in body
        assert headers["Cache-Control"] == "no-store"
        assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
        assert get(http, "/api/tasks")[0] == 200
        assert calls == ["alpha"]
        for path in [
            "/api/tasks?project=other",
            "/?project=other",
            "/../pyproject.toml",
            "/config",
            "/api/status/demo",
        ]:
            assert get(http, path)[0] == 404
        assert get(http, headers={"Host": "evil.example"})[0] == 403
        assert get(http, "/api/tasks", {"Sec-Fetch-Site": "cross-site"})[0] == 403
        assert len(calls) == 1

        async def failed(*_):
            raise OSError("PRIVATE_ERROR_PATH")

        monkeypatch.setattr(module, "project_tasks", failed)
        status, body, _ = get(http, "/api/tasks")
        assert status == 503 and json.loads(body)["tasks"] == []
        assert "PRIVATE" not in body
    assert "tasks=[];render()" in PAGE
    assert "Estado atual desconhecido" in PAGE
    assert "textContent=text" in PAGE and "innerHTML" not in PAGE


async def test_panel_matches_exact_existing_managed_read_contract(monkeypatch):
    import oraculo.cli as cli

    sent = []
    task = row()
    task.update(events=[dict(phase="executing", proof={"progress": "Progresso real"})])
    original = copy.deepcopy(task)

    async def request(config, envelope):
        sent.append(envelope)
        return dict(
            ok=True, data=dict(source="service", protocol_version=1, tasks=[task])
        )

    monkeypatch.setattr(cli, "request", request)
    result = await project_tasks(None, "alpha")
    assert len(sent) == 1 and sent[0]["kind"] == "managed.read"
    assert sent[0]["payload"] == dict(action="list", payload={})
    assert result["tasks"][0]["tracking"]["activity"] == "Progresso real"
    assert task == original


@pytest.mark.parametrize(
    "memory",
    [
        None,
        {"id": "demo-cycle", "write_state": "pending"},
        {
            "id": "demo-cycle",
            "write_state": "saved",
            "write_receipt": {
                "state": "confirmed",
                "operation_id": "wrong",
                "commit": "a" * 40,
            },
        },
        {
            "id": "demo-cycle",
            "write_state": "saved",
            "write_receipt": {"state": "confirmed", "operation_id": "demo-cycle"},
        },
    ],
)
def test_successful_work_without_coherent_memory_receipt_is_not_completed(memory):
    task = row()
    task.update(
        phase="pr_pending",
        work_status="succeeded",
        completion_evidence={"demo": True},
        memory=memory,
    )
    assert task_tracking(task)["state"] == "waiting"


def test_plan_approval_and_publication_are_distinct_from_certified_work():
    task = row()
    task.update(
        phase="plan_approved",
        work_status="succeeded",
        completion_evidence={"demo": True},
        deliverable_kind="plan",
        approval_state="pending",
    )
    assert task_tracking(task)["state"] == "waiting"
    task["approval_state"] = "approved"
    assert task_tracking(task)["state"] == "completed"
    task.update(phase="pr_waiting", deliverable_kind="implementation")
    view = task_tracking(task)
    assert view["state"] == "completed"
    assert view["phase"] == "Aguardando verificações"
    assert "publicação e implantação têm estados próprios" in view["result"]


async def test_describe_without_events_preserves_daemon_progress(monkeypatch):
    import oraculo.cli as cli

    task = row()
    task["presentation"] = dict(
        title="Título calculado no daemon",
        activity="Validando a importação do demo.",
        updated_at="29/10/2026 08:00 UTC",
        phase="Concluída",
        result="Concluída",
    )
    assert "events" not in task and "latest_event" not in task

    async def request(*_):
        return dict(
            ok=True, data=dict(source="service", protocol_version=1, tasks=[task])
        )

    monkeypatch.setattr(cli, "request", request)
    result = await project_tasks(None, "alpha")
    shown = result["tasks"][0]
    assert shown["title"] == task["presentation"]["title"]
    assert shown["tracking"]["activity"] == task["presentation"]["activity"]
    assert shown["tracking"]["updated_at"] == task["presentation"]["updated_at"]
    assert shown["tracking"]["state"] == "running"
    assert shown["tracking"]["result"] == "Resultado ainda não verificado."


def test_invalid_daemon_presentation_fields_use_structured_fallback():
    task = row()
    task["presentation"] = dict(title={}, activity=["bad"], updated_at="   ")
    tracking = task_tracking(task)
    assert tracking["title"] == task["title"]
    assert tracking["activity"] == "O agente está trabalhando na tarefa."
    assert tracking["updated_at"] == "desconhecida"
