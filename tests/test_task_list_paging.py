"""`task list` pages and the bounded `status`: synthetic ledgers only."""

import json
import time

import pytest

from oraculo.cli import legacy_task_page, main, parse_since
from oraculo.managed_service import command
from oraculo.protocol import MAX_ENVELOPE_BYTES, canonical_json
from oraculo.state import StateError
from oraculo.task_store import TaskStore

SUMMARY_KEYS = {
    "id",
    "title",
    "project_id",
    "phase",
    "work_status",
    "blocked_reason",
    "admission",
    "admission_source",
    "child_thread_id",
    "updated_at",
    "origin_thread_id",
    "key",
}


def populate(store, count=300, *, heavy=20_000):
    """Synthetic tasks, newest last. Each carries a large private report."""
    ids = []
    for index in range(count):
        project = "alpha" if index % 3 else "beta"
        origin = dict(thread_id=f"origin-{index % 4}", project_id=project)
        task = store.create(
            origin,
            f"key-{index}",
            dict(title=f"Synthetic task {index}", text="t" * 200),
        )
        changes = dict(
            report=dict(summary="private-report " + "r" * heavy),
            work_status="succeeded" if index % 5 == 0 else "running",
            child=dict(childThreadId=f"child-{index}"),
            admission=dict(state="reserved", provider="codex", slots=1),
        )
        if index % 10 == 0:
            changes["project"] = dict(project_id="gamma")
        if index % 7 == 0:
            store.update(
                task["id"],
                "blocked",
                {"reason": "example_blocker"},
                blocked_reason="example_blocker",
                **changes,
            )
        else:
            store.update(task["id"], "executing", {"synthetic": index}, **changes)
        ids.append(task["id"])
    return ids


@pytest.fixture
def ledger(tmp_path):
    store = TaskStore(tmp_path / "managed-tasks.sqlite3")
    ids = populate(store)
    yield store, ids
    store.close()


def test_summary_page_is_small_and_ordered_newest_first(ledger):
    store, ids = ledger
    page = store.list_page({})
    assert page["total"] == 300 and page["limit"] == 50 and page["view"] == "summary"
    assert [t["id"] for t in page["tasks"]] == ids[::-1][:50]
    assert all(set(t) == SUMMARY_KEYS for t in page["tasks"])
    first = page["tasks"][0]
    assert first["title"] == "Synthetic task 299"
    assert first["child_thread_id"] == "child-299"
    assert first["admission"] == "reserved"
    assert first["key"] == "key-299" and first["origin_thread_id"] == "origin-3"
    assert first["updated_at"].endswith("Z")
    assert "private-report" not in canonical_json(page)
    assert len(canonical_json(page).encode()) < 64 * 1024


def test_cursor_walks_every_task_exactly_once(ledger):
    store, ids = ledger
    seen, cursor, pages = [], None, 0
    while True:
        page = store.list_page(dict(limit=70) | ({"cursor": cursor} if cursor else {}))
        seen += [t["id"] for t in page["tasks"]]
        pages += 1
        assert page["total"] == 300
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert seen == ids[::-1] and pages == 5


def test_filters_run_in_sql_and_count_matches(ledger):
    store, ids = ledger
    gamma = store.list_page(dict(project="gamma", limit=500))
    assert gamma["total"] == 30 and gamma["next_cursor"] is None
    assert {t["project_id"] for t in gamma["tasks"]} == {"gamma"}
    # Unresolved tasks stay with the project of their origin.
    beta = store.list_page(dict(project="beta", limit=500))
    assert beta["total"] == sum(1 for i in range(300) if i % 3 == 0 and i % 10)
    blocked = store.list_page(dict(state="blocked", limit=500))
    assert blocked["total"] == 43
    assert {t["blocked_reason"] for t in blocked["tasks"]} == {"example_blocker"}
    succeeded = store.list_page(dict(state="succeeded", limit=500))
    assert succeeded["total"] == 60
    both = store.list_page(dict(state="succeeded", project="gamma", limit=500))
    assert both["total"] == 30 and len(both["tasks"]) == 30
    assert store.list_page(dict(state="no-such-state"))["tasks"] == []


def test_since_selects_recently_updated_tasks(ledger):
    store, ids = ledger
    assert store.list_page(dict(since=time.time() + 60))["total"] == 0
    assert store.list_page(dict(since=time.time() - 3600))["total"] == 300
    moment = time.time()
    store.update(ids[5], "executing", {"touched": True})
    recent = store.list_page(dict(since=moment))
    assert [t["id"] for t in recent["tasks"]] == [ids[5]]


def test_key_lookup_finds_the_bound_task(ledger):
    store, ids = ledger
    found = store.list_page(dict(key="key-42", origin_thread="origin-2"))
    assert found["total"] == 1 and found["tasks"][0]["id"] == ids[42]
    assert store.list_page(dict(key="key-42", origin_thread="origin-1"))["total"] == 0
    assert store.list_page(dict(key="key-42"))["tasks"][0]["id"] == ids[42]
    assert store.list_page(dict(key="never-used"))["tasks"] == []
    plan = store.db.execute(
        "EXPLAIN QUERY PLAN SELECT id FROM managed_tasks WHERE origin=? AND dedupe=?",
        ("origin-2", "key-42"),
    ).fetchall()
    assert "USING" in " ".join(str(tuple(row)) for row in plan)


def test_full_view_describes_only_the_page(ledger, monkeypatch):
    store, ids = ledger
    described = []
    original = store.describe

    def describe(task_id):
        described.append(task_id)
        return original(task_id)

    page = store.list_page(dict(view="full", limit=3), full=describe)
    assert described == ids[::-1][:4][:3]
    assert page["view"] == "full" and len(page["tasks"]) == 3
    assert "presentation" in page["tasks"][0]
    assert page["next_cursor"] is not None
    # A byte budget ends a full page early and keeps the cursor consistent.
    monkeypatch.setattr(TaskStore, "LIST_FULL_BYTES", 1)
    small = store.list_page(dict(view="full", limit=50), full=original)
    assert len(small["tasks"]) == 1 and small["next_cursor"] is not None
    following = store.list_page(
        dict(view="full", limit=50, cursor=small["next_cursor"]), full=original
    )
    assert following["tasks"][0]["id"] == ids[-2]


@pytest.mark.parametrize(
    "payload,code",
    [
        (dict(limit=0), "task_list_limit_invalid"),
        (dict(limit=501), "task_list_limit_invalid"),
        (dict(limit="5"), "task_list_limit_invalid"),
        (dict(cursor="abc"), "task_list_cursor_invalid"),
        (dict(cursor="-1"), "task_list_cursor_invalid"),
        (dict(since="yesterday"), "task_list_since_invalid"),
        (dict(view="everything"), "task_list_view_invalid"),
        (dict(project=5), "task_list_project_invalid"),
        (dict(state=""), "task_list_state_invalid"),
        (["list"], "task_list_query_invalid"),
    ],
)
def test_invalid_queries_are_refused(ledger, payload, code):
    with pytest.raises(StateError, match=f"^{code}$"):
        ledger[0].list_page(payload)


def test_filter_values_are_bound_parameters(ledger):
    store, _ids = ledger
    hostile = "x' OR 1=1 --"
    for name in ("project", "state", "key", "origin_thread"):
        assert store.list_page({name: hostile})["total"] == 0
    assert store.list_page({})["total"] == 300


async def test_daemon_pages_300_tasks_without_building_every_description(
    running_service, monkeypatch
):
    service = running_service
    await command(service.config, "list", {}, None)  # Opens the managed ledger.
    store = service.managed_service.store
    ids = populate(store)
    service.state.connection.execute(
        "INSERT INTO managed_reservations VALUES(?,?,?,'bound',NULL)",
        (ids[-1], "codex", 1),
    )
    described = []
    original = store.describe
    monkeypatch.setattr(store, "describe", lambda i: described.append(i) or original(i))
    reply = await command(service.config, "list", dict(view="summary"), None)
    assert described == []
    assert reply["total"] == 300 and len(reply["tasks"]) == 50
    assert reply["source"] == "service" and reply["protocol_version"] == 1
    assert "adapter" in reply and reply["next_cursor"]
    assert len(canonical_json(reply).encode()) < 64 * 1024
    newest = reply["tasks"][0]
    assert newest["admission"] == "bound" and newest["admission_source"] == "service"
    assert newest["admission_at_launch"] == "reserved"
    assert reply["tasks"][1]["admission"] == "not_reserved"

    everything = await command(
        service.config, "list", dict(view="summary", limit=500), None
    )
    assert len(everything["tasks"]) == 300 and everything["next_cursor"] is None
    assert len(canonical_json(everything).encode()) < MAX_ENVELOPE_BYTES

    blocked = await command(
        service.config, "list", dict(state="blocked", project="gamma"), None
    )
    assert blocked["total"] == len([i for i in range(300) if i % 70 == 0])

    full = await command(service.config, "list", dict(view="full", limit=2), None)
    assert described == ids[::-1][:2]
    assert full["view"] == "full" and full["tasks"][0]["admission"]["state"] == "bound"

    lookup = await command(
        service.config, "list", dict(key="key-7", origin_thread="origin-3"), None
    )
    assert [t["id"] for t in lookup["tasks"]] == [ids[7]]

    with pytest.raises(StateError, match="^task_list_limit_invalid$"):
        await command(service.config, "list", dict(limit=10_000), None)


async def test_empty_payload_keeps_the_unpaged_panel_contract(running_service):
    service = running_service
    await command(service.config, "list", {}, None)
    ids = populate(service.managed_service.store, 3, heavy=10)
    reply = await command(service.config, "list", {}, None)
    assert [t["id"] for t in reply["tasks"]] == ids[::-1]
    assert "view" not in reply and "presentation" in reply["tasks"][0]


def offline_cli(service_config, capsys, *flags):
    _config, config_path = service_config
    code = main(["--config", str(config_path), "task", "list", *flags])
    captured = capsys.readouterr()
    return code, captured


def test_cli_lists_300_tasks_from_the_offline_ledger(service_config, capsys):
    config, _path = service_config
    store = TaskStore(config.state_dir / "managed-tasks.sqlite3")
    ids = populate(store)
    store.close()
    code, captured = offline_cli(service_config, capsys)
    body = json.loads(captured.out)
    assert code == 0 and body["source"] == "local_ledger"
    assert body["total"] == 300 and len(body["tasks"]) == 50
    assert len(captured.out.encode()) < 128 * 1024
    assert body["tasks"][0]["admission_source"] == "launch_record"

    code, captured = offline_cli(
        service_config,
        capsys,
        "--project",
        "gamma",
        "--state",
        "blocked",
        "--limit",
        "2",
        "--since",
        "1h",
    )
    body = json.loads(captured.out)
    assert code == 0 and body["total"] == 5 and len(body["tasks"]) == 2
    code, captured = offline_cli(
        service_config,
        capsys,
        "--project",
        "gamma",
        "--state",
        "blocked",
        "--limit",
        "2",
        "--cursor",
        body["next_cursor"],
    )
    following = json.loads(captured.out)
    assert not {t["id"] for t in following["tasks"]} & {t["id"] for t in body["tasks"]}

    code, captured = offline_cli(
        service_config, capsys, "--key", "key-9", "--origin-thread", "origin-1"
    )
    assert [t["id"] for t in json.loads(captured.out)["tasks"]] == [ids[9]]

    code, captured = offline_cli(service_config, capsys, "--full", "--limit", "1")
    full = json.loads(captured.out)
    assert full["view"] == "full" and "presentation" in full["tasks"][0]
    assert full["tasks"][0]["id"] == ids[-1]


@pytest.mark.parametrize(
    "flags,message",
    [
        (["--limit", "501"], "task_list_limit_invalid"),
        (["--since", "soon"], "since_invalid"),
        (["--origin-thread", "origin-1"], "task_list_origin_thread_requires_key"),
        (["--cursor", "x"], "task_list_cursor_invalid"),
    ],
)
def test_cli_refuses_invalid_list_flags(service_config, capsys, flags, message):
    config, _path = service_config
    TaskStore(config.state_dir / "managed-tasks.sqlite3").close()
    code, captured = offline_cli(service_config, capsys, *flags)
    assert code == 1 and captured.out == ""
    assert message in json.loads(captured.err)["message"]


def test_list_flags_are_refused_by_other_task_actions(service_config, capsys, tmp_path):
    _config, config_path = service_config
    payload = tmp_path / "task.json"
    payload.write_text(json.dumps({"task_id": "task-example"}))
    code = main(
        [
            "--config",
            str(config_path),
            "task",
            "status",
            "--file",
            str(payload),
            "--state",
            "blocked",
        ]
    )
    captured = capsys.readouterr()
    assert code == 1 and captured.out == ""
    assert json.loads(captured.err)["message"] == (
        "task_list_flags_require_list: --state"
    )


@pytest.mark.parametrize("code", ["stdio_output_limit", "invalid_response"])
def test_list_over_the_output_limit_names_the_narrowing_flags(
    service_config, capsys, monkeypatch, code
):
    import oraculo.cli as cli

    config, _path = service_config
    TaskStore(config.state_dir / "managed-tasks.sqlite3").close()

    def refuse(_value, _stream=None):
        raise StateError(code)

    monkeypatch.setattr(cli, "print_json", refuse)
    exit_code, captured = offline_cli(service_config, capsys, "--full")
    message = json.loads(captured.err)["message"]
    assert exit_code == 1 and message.startswith(code + ": ")
    for flag in ("--limit", "--project", "--state", "--since", "--full"):
        assert flag in message


def test_status_over_the_output_limit_names_the_narrowing_flags(
    service_config, capsys, monkeypatch
):
    import oraculo.cli as cli

    _config, config_path = service_config

    async def oversized(_config, _envelope):
        raise ValueError("invalid_response")

    monkeypatch.setattr(cli, "request", oversized)
    assert main(["--config", str(config_path), "status"]) == 1
    message = json.loads(capsys.readouterr().err)["message"]
    assert message.startswith("invalid_response: ")
    assert "--limit" in message and "--project" in message and "--full" in message


def test_status_command_requests_a_bounded_view_unless_full(
    service_config, capsys, monkeypatch
):
    import oraculo.cli as cli

    _config, config_path = service_config
    sent = []

    async def record(_config, envelope):
        sent.append(envelope)
        return {"ok": True, "request_id": envelope["request_id"], "data": {}}

    monkeypatch.setattr(cli, "request", record)
    assert main(["--config", str(config_path), "status"]) == 0
    assert main(["--config", str(config_path), "status", "--limit", "5"]) == 0
    assert main(["--config", str(config_path), "status", "--full"]) == 0
    capsys.readouterr()
    assert [e["payload"] for e in sent] == [
        {"view": "summary", "limit": 50},
        {"view": "summary", "limit": 5},
        {},
    ]
    assert {e["kind"] for e in sent} == {"status.get"}


async def test_status_summary_bounds_growing_collections(running_service):
    service = running_service
    for index in range(7):
        service.state.submit_objective(
            "alpha", f"objective {index}", dedupe_key=f"objective-{index}"
        )
    full = service.status("*")
    assert len(full["objectives"]) == 7
    assert "view" not in full and "totals" not in full
    bounded = service.status("*", {"view": "summary", "limit": 3})
    assert [o["id"] for o in bounded["objectives"]] == [
        o["id"] for o in full["objectives"][-3:]
    ]
    assert bounded["view"] == "summary" and bounded["limit"] == 3
    assert bounded["totals"]["objectives"] == 7
    assert "objectives" in bounded["truncated"]
    assert bounded["runtime"] == full["runtime"]
    assert bounded["managed_adapter"] == full["managed_adapter"]
    assert set(full) <= set(bounded)
    with pytest.raises(StateError, match="^status_limit_invalid$"):
        service.status("*", {"view": "summary", "limit": 0})


def test_parse_since_accepts_iso_times_and_durations():
    now = 1_800_000_000.0
    assert parse_since("90m", now) == now - 5400
    assert parse_since("24h", now) == now - 86400
    assert parse_since("7d", now) == now - 604800
    assert parse_since("1.5h", now) == now - 5400
    assert parse_since("2026-11-11T12:00:00Z") == 1794398400.0
    assert parse_since("2026-11-11T09:00:00-03:00") == 1794398400.0
    assert parse_since("2026-11-11") > 0
    for invalid in ("soon", "", "10", "5x", "-5m"):
        with pytest.raises(ValueError, match="^since_invalid"):
            parse_since(invalid)


def test_older_daemon_answer_is_bounded_on_the_client():
    now = time.time()
    tasks = [
        dict(
            id=f"task-{index}",
            title=f"Task {index}",
            origin=dict(project_id="alpha" if index % 2 else "beta"),
            phase="blocked" if index % 5 == 0 else "executing",
            work_status="running",
            blocked_reason=None,
            admission=dict(state="reserved", source="service"),
            child=dict(childThreadId=f"child-{index}"),
            observed_at=now - index,
            presentation=dict(large="x" * 5000),
        )
        for index in range(120)
    ]
    reply = dict(tasks=tasks, source="service", protocol_version=1, adapter={})
    payload = dict(view="summary", limit=50, project="alpha")
    page = legacy_task_page(reply, payload)
    assert page["total"] == 60 and len(page["tasks"]) == 50
    assert page["paging"] == "client_side_legacy_daemon" and page["source"] == "service"
    assert "presentation" not in page["tasks"][0]
    assert page["tasks"][0]["child_thread_id"] == "child-1"
    last = legacy_task_page(reply, dict(payload, cursor=page["next_cursor"]))
    assert len(last["tasks"]) == 10 and last["next_cursor"] is None
    blocked = legacy_task_page(reply, dict(view="full", limit=50, state="blocked"))
    assert blocked["total"] == 24 and "presentation" in blocked["tasks"][0]
    assert (
        legacy_task_page(reply, dict(view="summary", limit=5, since=now - 10.5))[
            "total"
        ]
        == 11
    )
    with pytest.raises(ValueError, match="^task_list_key_requires_service_upgrade$"):
        legacy_task_page(reply, dict(view="summary", limit=5, key="k"))
