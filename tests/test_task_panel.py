"""Synthetic project view through the real daemon socket and loopback HTTP."""

import asyncio
import copy
import json
from contextlib import contextmanager
from http.client import HTTPConnection
from threading import Thread

import pytest

from oraculo.presentation import task_tracking
from oraculo.task_panel import all_project_tasks, make_server, project_tasks
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
def server(config=None, *, all_projects=False):
    with make_server(
        config, None if all_projects else "alpha", 0, all_projects=all_projects
    ) as http:
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


async def test_project_identity_and_history_are_projected_without_private_metadata(
    monkeypatch,
):
    import oraculo.cli as cli

    active, stopped, completed, unverified = [row() for _ in range(4)]
    for i, task in enumerate((active, stopped, completed, unverified)):
        task.update(
            id=f"task-demo-{i}-full-id",
            project={"project_id": "alpha", "project_path": "/private/demo/alpha"},
            observed_at=1793260800 + i,
            report={"summary": "PRIVATE_REPORT"},
        )
    active["title"] = '<img src=x onerror="alert(1)"> & demo'
    stopped.update(work_status="stopped", observed={"status": "interrupted"})
    completed.update(
        phase="pr_pending",
        work_status="succeeded",
        completion_evidence={"private": "PRIVATE_EVIDENCE"},
    )
    unverified.update(observed={"status": "completed"})
    tasks = [active, stopped, completed, unverified]
    before = copy.deepcopy(tasks)

    async def request(*_):
        return dict(
            ok=True, data=dict(source="service", protocol_version=1, tasks=tasks)
        )

    monkeypatch.setattr(cli, "request", request)
    result = await project_tasks(None, "alpha")
    assert result["project_title"] == "alpha"
    assert [t["history"] for t in result["tasks"]] == [False, True, True, False]
    assert [t["tracking"]["state"] for t in result["tasks"]] == [
        "running",
        "blocked",
        "completed",
        "waiting",
    ]
    for projected, original in zip(result["tasks"], tasks, strict=True):
        assert projected["id"] == original["id"]
        assert projected["short_id"] == original["id"].removeprefix("task-")[:10]
        assert projected["project_id"] == "alpha"
        assert projected["project_title"] == "alpha"
        assert projected["observed_at"] == original["observed_at"]
        assert projected["tracking"]["next_action"]
    assert tasks == before
    encoded = json.dumps(result)
    assert "PRIVATE_" not in encoded and "/private/" not in encoded
    # Text remains text in JSON; the HTML shell never interpolates service data.
    assert "<img" in result["tasks"][0]["title"]


@pytest.mark.parametrize("observed_at", [None, True, {}, "unknown", float("inf")])
async def test_invalid_recency_is_unknown(monkeypatch, observed_at):
    import oraculo.cli as cli

    task = row()
    task["observed_at"] = observed_at

    async def request(*_):
        return dict(
            ok=True, data=dict(source="service", protocol_version=1, tasks=[task])
        )

    monkeypatch.setattr(cli, "request", request)
    assert (await project_tasks(None, "alpha"))["tasks"][0]["observed_at"] is None


def test_http_task_text_roundtrips_as_json_without_entering_the_html(monkeypatch):
    import oraculo.task_panel as module

    title = '</script><img src=x onerror="alert(1)"> & demo'

    async def query(*_):
        return dict(project_title="alpha", tasks=[dict(id="demo", title=title)])

    monkeypatch.setattr(module, "project_tasks", query)
    with server() as http:
        status, body, headers = get(http, "/api/tasks")
        assert status == 200
        assert headers["Content-Type"].startswith("application/json")
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert json.loads(body)["tasks"][0]["title"] == title
        assert title not in get(http)[1]


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


@pytest.fixture
def global_catalog(running_service):
    """Only alpha/beta/gamma are allowed; gamma has no tasks."""
    service = running_service
    for identifier in ("beta", "gamma", "excluded", "removed"):
        path = service.config.projects_root / identifier
        path.mkdir()
        service.state.register_project(
            identifier,
            str(path),
            "unregistered" if identifier == "removed" else "registered",
        )
    service.state.register_project(
        "outside", str(service.config.projects_root.parent / "outside")
    )
    service.config.allowed_projects = [
        service.config.projects_root / identifier
        for identifier in ("alpha", "beta", "gamma", "removed")
    ]
    return service


async def test_global_catalog_scope_binding_empty_and_minimal_projection(
    global_catalog,
):
    service = global_catalog
    store = TaskStore(service.config.state_dir / "managed-tasks.sqlite3")
    fixtures = [
        ("alpha", None, "Título igual"),
        ("beta", None, "Título igual"),
        ("alpha", "beta", "Destino resolvido"),
        ("unknown", "alpha", "Origem desconhecida, destino válido"),
        ("alpha", "unknown", "PRIVATE_UNKNOWN_DESTINATION"),
        ("alpha", "excluded", "PRIVATE_EXCLUDED_DESTINATION"),
        ("alpha", "removed", "PRIVATE_REMOVED_DESTINATION"),
        ("alpha", "outside", "PRIVATE_OUTSIDE_DESTINATION"),
        ("unknown", None, "PRIVATE_UNKNOWN_ORIGIN"),
        ("excluded", None, "PRIVATE_EXCLUDED_ORIGIN"),
        ("removed", None, "PRIVATE_REMOVED_ORIGIN"),
        ("outside", None, "PRIVATE_OUTSIDE_ORIGIN"),
    ]
    for i, (origin, destination, title) in enumerate(fixtures):
        task = store.create(
            dict(thread_id=f"origin-{i}", run_id="run", project_id=origin),
            f"synthetic-{i}",
            dict(title=title, text="PRIVATE_PROMPT", cwd="PRIVATE_CWD"),
        )
        store.update(
            task["id"],
            "executing",
            {"synthetic": True},
            project=dict(
                project_id=destination,
                project_path="/PRIVATE_REQUESTED_PATH/forged-name",
            )
            if destination
            else None,
            report={"summary": "PRIVATE_REPORT"},
            context={"body": "PRIVATE_CONTEXT"},
            observed={"status": "completed"},
        )
    original = [
        store.get(t[0]) for t in store.db.execute("SELECT id FROM managed_tasks")
    ]
    snapshot = await all_project_tasks(service.config)
    assert snapshot["mode"] == "all"
    assert snapshot["source"] == "service" and snapshot["fetched_at"]
    assert snapshot["projects"] == [
        dict(id=p, title=p) for p in ("alpha", "beta", "gamma")
    ]
    assert len(snapshot["tasks"]) == 4
    assert [t["project_id"] for t in snapshot["tasks"]].count("alpha") == 2
    assert [t["project_id"] for t in snapshot["tasks"]].count("beta") == 2
    identical = [t for t in snapshot["tasks"] if t["title"] == "Título igual"]
    assert len({t["id"] for t in identical}) == 2
    assert {t["project_id"] for t in identical} == {"alpha", "beta"}
    assert all(
        t["tracking"]["state"] == "waiting" and not t["history"]
        for t in snapshot["tasks"]
    )
    assert all(t["project_title"] == t["project_id"] for t in snapshot["tasks"])
    assert not any(t["project_id"] == "gamma" for t in snapshot["tasks"])
    assert "PRIVATE_" not in json.dumps(snapshot)
    assert set(snapshot) == {"mode", "projects", "source", "fetched_at", "tasks"}
    assert set(snapshot["tasks"][0]) == {
        "id",
        "short_id",
        "project_id",
        "project_title",
        "title",
        "tracking",
        "history",
        "observed_at",
        "snapshot_revision",
    }
    assert original == [
        store.get(t[0]) for t in store.db.execute("SELECT id FROM managed_tasks")
    ]
    assert service.memory_cycles.list() == []
    assert not service.state.connection.execute(
        "SELECT * FROM managed_reservations"
    ).fetchall()
    store.close()


async def test_global_catalog_names_come_only_from_registered_paths(global_catalog):
    service = global_catalog
    root = service.config.projects_root
    long_name = '<img src=x onerror="alert(1)">' + "muito-longo-" * 20
    # Similar display names never merge IDs or capture tasks in an empty project.
    for identifier in ("alpha", "beta"):
        path = root / identifier / long_name
        service.state.register_project(identifier, str(path))
    service.config.allowed_projects = None
    result = await all_project_tasks(service.config)
    named = [p for p in result["projects"] if p["id"] in {"alpha", "beta"}]
    assert len(named) == 2 and named[0]["title"] == named[1]["title"]
    assert "<img" in named[0]["title"]
    assert all(set(p) == {"id", "title"} for p in result["projects"])
    assert str(root) not in json.dumps(result)


@pytest.mark.parametrize("failed_kind", ["status.get", "managed.read"])
async def test_global_partial_failure_never_serves_partial_snapshot(
    global_catalog, monkeypatch, failed_kind
):
    import oraculo.cli as cli

    service = global_catalog
    original = cli.request
    sent = []

    async def request(config, envelope):
        sent.append(envelope)
        if envelope["kind"] == failed_kind:
            return dict(ok=False, error={"message": "PRIVATE_SERVICE_ERROR"})
        return await original(config, envelope)

    monkeypatch.setattr(cli, "request", request)
    with server(service.config, all_projects=True) as http:
        status, body, headers = await asyncio.to_thread(get, http, "/api/tasks")
        assert status == 503
        assert json.loads(body)["error"] == "service_unavailable"
        assert "projects" not in json.loads(body) and "PRIVATE" not in body
        assert headers["Cache-Control"] == "no-store"
    assert all(e["kind"] in {"status.get", "managed.read"} for e in sent)
    for envelope in sent:
        assert envelope["project_id"] == "*"
        assert envelope["payload"] == (
            {} if envelope["kind"] == "status.get" else dict(action="list", payload={})
        )


async def test_global_http_and_fixed_mode_isolation_through_real_socket(global_catalog):
    service = global_catalog
    store = TaskStore(service.config.state_dir / "managed-tasks.sqlite3")
    for project in ("alpha", "beta"):
        store.create(
            dict(project_id=project, thread_id=project, run_id="run"),
            "same",
            {"title": f"Visible {project}"},
        )
    with (
        server(service.config) as fixed,
        server(service.config, all_projects=True) as global_http,
    ):
        status, body, _ = await asyncio.to_thread(get, global_http, "/api/tasks")
        assert status == 200
        assert {t["project_id"] for t in json.loads(body)["tasks"]} == {"alpha", "beta"}
        status, body, _ = await asyncio.to_thread(get, fixed, "/api/tasks")
        assert status == 200 and "Visible beta" not in body
        assert "projects" not in json.loads(body)
        assert 'data-mode="all"' in (await asyncio.to_thread(get, global_http))[1]
        assert 'data-mode="project"' in (await asyncio.to_thread(get, fixed))[1]
        for http in (fixed, global_http):
            for path in (
                "/?project=unknown",
                "/api/tasks?project=beta",
                "/api/tasks?all-projects=true",
                "/config.toml",
            ):
                assert (await asyncio.to_thread(get, http, path))[0] == 404
            assert (await asyncio.to_thread(get, http, headers={"Host": "localhost"}))[
                0
            ] == 403
            assert (
                await asyncio.to_thread(
                    get, http, "/api/tasks", {"Sec-Fetch-Site": "cross-site"}
                )
            )[0] == 403
    store.close()


@pytest.mark.parametrize(
    "data",
    [
        None,
        {},
        {"projects": None},
        {"projects": []},
        {"runtime": "invalid", "projects": []},
        {"runtime": {"protocol_version": 1}, "projects": [None]},
        {
            "runtime": {"protocol_version": 1},
            "projects": [{"id": "alpha", "status": "registered"}],
        },
    ],
)
async def test_invalid_catalog_is_unavailable_instead_of_empty(monkeypatch, data):
    import oraculo.cli as cli

    async def request(*_):
        return dict(ok=True, data=data)

    monkeypatch.setattr(cli, "request", request)
    with server(all_projects=True) as http:
        assert get(http, "/api/tasks")[0] == 503


async def test_global_socket_unavailable_has_no_offline_fallback(service_config):
    with pytest.raises(OSError):
        await all_project_tasks(service_config[0])


@pytest.mark.parametrize(
    "data",
    [
        None,
        {"source": "local_ledger", "protocol_version": 1, "tasks": []},
        {"source": "service", "protocol_version": 2, "tasks": []},
        {"source": "service", "protocol_version": 1, "tasks": None},
        {"source": "service", "protocol_version": 1, "tasks": [None]},
    ],
)
async def test_invalid_task_list_never_uses_successful_catalog(
    global_catalog, monkeypatch, data
):
    import oraculo.cli as cli

    original = cli.request

    async def request(config, envelope):
        if envelope["kind"] == "managed.read":
            return dict(ok=True, data=data)
        return await original(config, envelope)

    monkeypatch.setattr(cli, "request", request)
    with server(global_catalog.config, all_projects=True) as http:
        status, body, _ = await asyncio.to_thread(get, http, "/api/tasks")
        assert status == 503 and "projects" not in json.loads(body)


async def test_entire_authorized_catalog_can_be_empty(global_catalog):
    service = global_catalog
    service.config.allowed_projects = []
    result = await all_project_tasks(service.config)
    assert result["projects"] == [] and result["tasks"] == []
    assert result["source"] == "service" and result["fetched_at"]


def test_global_scope_must_be_explicit_and_exclusive():
    for project, all_projects in (
        (None, False),
        ("alpha", True),
        ("*", False),
        (None, "true"),
    ):
        with pytest.raises(ValueError):
            make_server(None, project, 0, all_projects=all_projects)


def test_cli_global_scope_and_exclusion(monkeypatch, service_config):
    from oraculo.cli import main

    calls = []

    def serve(config, project, port, **kwargs):
        calls.append((project, port, kwargs))

    monkeypatch.setattr("oraculo.task_panel.serve", serve)
    args = ["--config", str(service_config[1]), "experience", "serve"]
    assert main([*args, "--all-projects", "--port", "8768"]) == 0
    assert calls == [(None, 8768, {"all_projects": True})]
    for scope in ([], ["--all-projects", "--project", "alpha"]):
        with pytest.raises(SystemExit) as error:
            main([*args, *scope])
        assert error.value.code == 2
    assert len(calls) == 1
