"""CLI boundary for the unified reading vault. Service I/O is fake."""

import hashlib
import json
import sqlite3
import sys
import types
from pathlib import Path

from oraculo.cli import main, parser

FIXTURE = Path(__file__).parent / "fixtures/experience.json"


def invoke(argv):
    code = main(argv)
    return code


def snapshot(
    project_id,
    *,
    title="Projeto",
    observed_at="2026-11-01T00:00:00+00:00",
    task_id="local-1",
    body="nota única",
    empty=False,
):
    documents = []
    tasks = []
    if not empty:
        documents.append(
            {
                "path": f"projects/{project_id}/wiki/index.md",
                "content": f"# {title}\n\n{body}\n",
            }
        )
        tasks.append(
            {
                "id": task_id,
                "title": title,
                "project": {"project_id": project_id},
                "parent_task_id": "parent-explicit",
            }
        )
    return {
        "schema_version": 1,
        "project_id": project_id,
        "observed_at": observed_at,
        "base_commit": "abc123",
        "projects": [{"id": project_id, "title": title}],
        "documents": documents,
        "tasks": tasks,
        "cycles": [],
    }


def catalog_row(project_id, *, status="registered", title=None, path=None):
    row = {
        "id": project_id,
        "path": path or f"/synthetic/{project_id}",
        "status": status,
        "coordinator_thread_id": None,
    }
    if title is not None:
        row["title"] = title
    return row


def status_payload(rows, *, health="online", protocol=1):
    return {
        "ok": True,
        "state": "ok",
        "data": {
            "health": health,
            "runtime": {"protocol_version": protocol},
            "projects": rows,
            "plans": [{"private": "do-not-copy"}],
        },
    }


def install_portfolio(monkeypatch, files=None):
    recorded = {}
    module = types.ModuleType("oraculo.experience_portfolio")

    def portfolio_model(snapshots, project_titles=None):
        recorded["snapshots"] = snapshots
        recorded["titles"] = project_titles
        observed = next(
            (
                item.get("observed_at")
                for item in reversed(snapshots)
                if isinstance(item.get("observed_at"), str)
            ),
            None,
        )
        return {
            "schema_version": 1,
            "kind": "portfolio",
            "observed_at": observed,
            "projects": [
                {
                    "id": item["project_id"],
                    "export_root": item["project_id"],
                }
                for item in snapshots
            ],
            "shared": {"entries": [], "documents": {}},
            "links": [],
            "counts": {
                "projects": len(snapshots),
                "tasks": sum(len(item.get("tasks") or []) for item in snapshots),
                "documents": sum(
                    len(item.get("documents") or []) for item in snapshots
                ),
            },
        }

    def portfolio_export_files(model):
        recorded["model"] = model
        if files is not None:
            return files(model)
        names = ",".join(project["id"] for project in model["projects"])
        stamp = model.get("observed_at") or ""
        exported = {
            "Inicio.md": f"# Início {names} {stamp}\n",
            "index.html": "<!doctype html><title>cofre</title>\n",
            ".obsidian/workspace.json": "{}\n",
            "modelo.json": json.dumps(model["counts"]) + "\n",
        }
        for project in model["projects"]:
            exported[f"{project['export_root']}/Projeto.md"] = project["id"] + "\n"
        return exported

    module.portfolio_model = portfolio_model
    module.portfolio_export_files = portfolio_export_files
    monkeypatch.setitem(sys.modules, "oraculo.experience_portfolio", module)
    return recorded


def write_config(tmp_path):
    root = tmp_path / "config-projects"
    vault = tmp_path / "config-vault"
    state = tmp_path / "config-state"
    for path in (root, vault, state):
        path.mkdir()
    config = tmp_path / "config.toml"
    config.write_text(
        "\n".join(
            [
                "schema_version = 1",
                f"projects_root = {json.dumps(str(root))}",
                f"vault_root = {json.dumps(str(vault))}",
                f"state_dir = {json.dumps(str(state))}",
                't3_mcp_url = "https://t3.example.invalid/mcp"',
                "additional_spend_allowed = false",
                "",
            ]
        )
    )
    return config


def run(monkeypatch, capsys, argv):
    def refuse_sqlite(*_args, **_kwargs):
        raise AssertionError("sqlite")

    monkeypatch.setattr(sqlite3, "connect", refuse_sqlite)
    code = main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def error_message(err):
    return json.loads(err)["message"]


def test_parser_keeps_single_project_commands():
    export = parser().parse_args(
        [
            "experience",
            "export",
            "--file",
            "bundle.json",
            "--project",
            "demo",
            "--output",
            "out",
        ]
    )
    assert export.action == "export" and export.project == "demo"
    portfolio = parser().parse_args(
        ["experience", "portfolio", "--file", "bundle.json", "--output", "out"]
    )
    assert portfolio.action == "portfolio"
    sync = parser().parse_args(
        ["experience", "sync", "--output", "out", "--project", "a", "--project", "b"]
    )
    assert sync.project == ["a", "b"]


def test_offline_portfolio_preserves_distinct_projects(tmp_path, monkeypatch, capsys):
    recorded = install_portfolio(monkeypatch)
    bundle = {
        "schema_version": 1,
        "project_titles": {"alpha": "Alpha", "beta": "Alpha"},
        "snapshots": [
            snapshot("beta", title="Alpha", task_id="local-1", body="beta-only"),
            snapshot("alpha", title="Alpha", task_id="local-1", body="alpha-only"),
            snapshot("empty", title="Vazio", empty=True, observed_at=None),
        ],
    }
    path = tmp_path / "bundle.json"
    path.write_text(json.dumps(bundle))
    missing = tmp_path / "missing.toml"
    code, out, err = run(
        monkeypatch,
        capsys,
        [
            "--config",
            str(missing),
            "experience",
            "portfolio",
            "--file",
            str(path),
            "--output",
            str(tmp_path / "vault"),
        ],
    )
    assert code == 0, err
    assert not missing.exists()
    receipt = json.loads(out)
    assert receipt["mode"] == "snapshot"
    assert receipt["projects"] == ["beta", "alpha", "empty"]
    assert receipt["freshness"][2]["observed_at"] is None
    assert receipt["freshness"][2]["source"] == "file"
    forwarded = recorded["snapshots"]
    assert [item["project_id"] for item in forwarded] == ["beta", "alpha", "empty"]
    assert forwarded[0]["documents"][0]["content"].endswith("beta-only\n")
    assert forwarded[1]["tasks"][0]["id"] == forwarded[0]["tasks"][0]["id"] == "local-1"
    assert recorded["titles"] == {"alpha": "Alpha", "beta": "Alpha"}
    vault = tmp_path / "vault"
    assert (vault / "Inicio.md").read_text().startswith("# Início beta,alpha,empty")
    assert (vault / ".obsidian/workspace.json").is_file()
    assert "beta-only" not in (vault / "alpha/Projeto.md").read_text()
    assert not (vault / "alpha/.obsidian").exists()


def test_offline_portfolio_rejects_invalid_bundles(tmp_path, monkeypatch, capsys):
    monkeypatch.delitem(sys.modules, "oraculo.experience_portfolio", raising=False)
    destination = tmp_path / "vault"
    cases = [
        ("{", "experience_portfolio_invalid_artifact"),
        (
            {"schema_version": 2, "snapshots": [snapshot("alpha")]},
            "experience_portfolio_invalid_artifact",
        ),
        (
            {"schema_version": 1, "snapshots": [], "extra": True},
            "experience_portfolio_invalid_artifact",
        ),
        ({"schema_version": 1, "snapshots": []}, "experience_portfolio_no_projects"),
        (
            {
                "schema_version": 1,
                "snapshots": [snapshot("alpha"), snapshot("alpha")],
            },
            "experience_portfolio_duplicate_project",
        ),
        (
            {
                "schema_version": 1,
                "snapshots": [snapshot("alpha")],
                "project_titles": {"other": "Nome"},
            },
            "experience_portfolio_unknown_title",
        ),
        (
            {
                "schema_version": 1,
                "snapshots": [snapshot("alpha")],
                "project_titles": {"alpha": ""},
            },
            "experience_portfolio_invalid_artifact",
        ),
    ]
    for payload, expected in cases:
        path = tmp_path / "bundle.json"
        path.write_text(payload if isinstance(payload, str) else json.dumps(payload))
        code, _out, err = run(
            monkeypatch,
            capsys,
            [
                "experience",
                "portfolio",
                "--file",
                str(path),
                "--output",
                str(destination),
            ],
        )
        assert code == 1
        assert error_message(err) == expected
        assert not destination.exists()
        assert "oraculo.experience_portfolio" not in sys.modules


def test_offline_portfolio_refuses_traversal_before_write(
    tmp_path, monkeypatch, capsys
):
    install_portfolio(
        monkeypatch, files=lambda model: {"../escape.txt": "no", "Inicio.md": "#\n"}
    )
    path = tmp_path / "bundle.json"
    path.write_text(json.dumps({"schema_version": 1, "snapshots": [snapshot("alpha")]}))
    outside = tmp_path / "escape.txt"
    code, _out, err = run(
        monkeypatch,
        capsys,
        [
            "experience",
            "portfolio",
            "--file",
            str(path),
            "--output",
            str(tmp_path / "vault"),
        ],
    )
    assert code == 1
    assert error_message(err) == "experience_portfolio_unsafe_path"
    assert not outside.exists()
    assert not (tmp_path / "vault").exists()


def test_single_project_export_still_runs_without_portfolio_module(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.delitem(sys.modules, "oraculo.experience_portfolio", raising=False)
    code, out, err = run(
        monkeypatch,
        capsys,
        [
            "experience",
            "export",
            "--file",
            str(FIXTURE),
            "--project",
            "demo",
            "--output",
            str(tmp_path / "one"),
        ],
    )
    assert code == 0, err
    assert json.loads(out)["state"] == "exported"
    assert (tmp_path / "one" / "Inicio.md").is_file()
    assert "oraculo.experience_portfolio" not in sys.modules


def test_sync_selects_authorized_projects_and_catalog_titles(
    tmp_path, monkeypatch, capsys
):
    recorded = install_portfolio(monkeypatch)
    config = write_config(tmp_path)
    bodies = {
        "beta": snapshot(
            "beta", title="Pasta", observed_at="2026-11-01T01:00:00+00:00", body="B"
        ),
        "alpha": snapshot(
            "alpha",
            title="Pasta",
            observed_at="2026-11-01T02:00:00+00:00",
            task_id="local-1",
            body="A",
        ),
        "empty": snapshot("empty", empty=True, observed_at="2026-11-01T03:00:00+00:00"),
    }
    rows = [
        catalog_row("zeta", status="retired"),
        catalog_row("hidden", title="Não entra"),
        catalog_row("empty"),
        catalog_row("beta", title="  Beta humano  "),
        catalog_row("alpha", title="Alpha humano"),
        catalog_row("blank", title=""),
    ]
    calls = []

    async def fake_request(_config, envelope):
        calls.append((envelope["kind"], envelope["project_id"]))
        if envelope["kind"] == "status.get":
            assert envelope["project_id"] == "*"
            return status_payload(rows, health="degraded", protocol=1)
        project_id = envelope["project_id"]
        if project_id in {"hidden", "blank"}:
            return {
                "ok": False,
                "error": {
                    "code": "StateError",
                    "message": "project_not_registered_or_outside_scope",
                },
            }
        return {"ok": True, "data": bodies[project_id]}

    monkeypatch.setattr("oraculo.cli.request", fake_request)
    code, out, err = run(
        monkeypatch,
        capsys,
        [
            "--config",
            str(config),
            "experience",
            "sync",
            "--output",
            str(tmp_path / "vault"),
        ],
    )
    assert code == 0, err
    receipt = json.loads(out)
    assert receipt["mode"] == "refresh"
    assert receipt["projects"] == ["alpha", "beta", "empty"]
    assert receipt["excluded_unauthorized"] == ["blank", "hidden"]
    assert receipt["service"] == {"health": "degraded", "protocol_version": 1}
    assert [item["observed_at"] for item in receipt["freshness"]] == [
        "2026-11-01T02:00:00+00:00",
        "2026-11-01T01:00:00+00:00",
        "2026-11-01T03:00:00+00:00",
    ]
    assert recorded["titles"] == {
        "alpha": "Alpha humano",
        "beta": "  Beta humano  ",
    }
    assert "plans" not in json.dumps(recorded["snapshots"])
    assert [item["documents"][0]["content"] for item in recorded["snapshots"][:2]] == [
        "# Pasta\n\nA\n",
        "# Pasta\n\nB\n",
    ]
    assert recorded["snapshots"][0]["tasks"][0]["id"] == "local-1"
    assert recorded["snapshots"][1]["tasks"][0]["id"] == "local-1"
    assert calls[0] == ("status.get", "*")
    assert ("experience.snapshot", "zeta") not in calls
    assert ("experience.snapshot", "hidden") in calls
    assert json.dumps(receipt).count("/synthetic/") == 0


def test_sync_explicit_projects_skip_unselected_and_download_chunks(
    tmp_path, monkeypatch, capsys
):
    recorded = install_portfolio(monkeypatch)
    config = write_config(tmp_path)
    alpha = snapshot("alpha", body="chunked-alpha")
    wrapper = {
        "project_id": "alpha",
        "job_id": "experience",
        "context": {"bundle": alpha},
    }
    raw = json.dumps(wrapper, ensure_ascii=False)
    digest = hashlib.sha256(raw.encode()).hexdigest()
    calls = []

    async def fake_request(_config, envelope):
        calls.append((envelope["kind"], envelope["project_id"], envelope["payload"]))
        if envelope["kind"] == "status.get":
            return status_payload(
                [
                    catalog_row("gamma", title="Gamma"),
                    catalog_row("alpha"),
                    catalog_row("beta", title="Beta"),
                ]
            )
        project_id = envelope["project_id"]
        assert project_id == "alpha"
        payload = envelope["payload"]
        if not payload:
            return {
                "ok": True,
                "data": {
                    "schema_version": 1,
                    "project_id": "alpha",
                    "observed_at": alpha["observed_at"],
                    "artifact": {
                        "artifact": digest,
                        "sha256": digest,
                        "bytes": len(raw.encode()),
                    },
                },
            }
        assert payload == {"artifact": digest, "offset": 0}
        return {
            "ok": True,
            "data": {
                "project_id": "alpha",
                "job_id": "experience",
                "artifact": digest,
                "sha256": digest,
                "offset": 0,
                "content": raw,
                "next_offset": len(raw),
                "eof": True,
            },
        }

    monkeypatch.setattr("oraculo.cli.request", fake_request)
    code, out, err = run(
        monkeypatch,
        capsys,
        [
            "--config",
            str(config),
            "experience",
            "sync",
            "--output",
            str(tmp_path / "vault"),
            "--project",
            "alpha",
        ],
    )
    assert code == 0, err
    receipt = json.loads(out)
    assert receipt["projects"] == ["alpha"]
    assert recorded["titles"] is None
    assert recorded["snapshots"][0]["documents"][0]["content"].endswith(
        "chunked-alpha\n"
    )
    assert "gamma" not in json.dumps(recorded["snapshots"])
    assert [call[1] for call in calls if call[0] == "experience.snapshot"] == [
        "alpha",
        "alpha",
    ]


def test_sync_argument_and_catalog_failures(tmp_path, monkeypatch, capsys):
    config = write_config(tmp_path)
    destination = tmp_path / "vault"
    missing = tmp_path / "absent.toml"

    code, _out, err = run(
        monkeypatch,
        capsys,
        [
            "--config",
            str(missing),
            "experience",
            "sync",
            "--output",
            str(destination),
            "--project",
            "alpha",
            "--project",
            "alpha",
        ],
    )
    assert code == 1
    assert error_message(err) == "experience_sync_duplicate_project"
    assert not missing.exists()

    code, _out, err = run(
        monkeypatch,
        capsys,
        [
            "--config",
            str(missing),
            "experience",
            "sync",
            "--output",
            str(destination),
            "--project",
            "",
        ],
    )
    assert code == 1
    assert error_message(err) == "experience_sync_invalid_project"

    async def failed(_config, envelope):
        return {"ok": False, "error": {"message": "socket down"}}

    monkeypatch.setattr("oraculo.cli.request", failed)
    code, _out, err = run(
        monkeypatch,
        capsys,
        ["--config", str(config), "experience", "sync", "--output", str(destination)],
    )
    assert code == 1
    assert error_message(err) == "experience_sync_service_failed:socket down"

    async def malformed(_config, envelope):
        return {"ok": True, "data": {"projects": "no"}}

    monkeypatch.setattr("oraculo.cli.request", malformed)
    code, _out, err = run(
        monkeypatch,
        capsys,
        ["--config", str(config), "experience", "sync", "--output", str(destination)],
    )
    assert code == 1
    assert error_message(err) == "experience_sync_invalid_catalog"

    async def duplicate_catalog(_config, envelope):
        return status_payload([catalog_row("alpha"), catalog_row("alpha")])

    monkeypatch.setattr("oraculo.cli.request", duplicate_catalog)
    code, _out, err = run(
        monkeypatch,
        capsys,
        ["--config", str(config), "experience", "sync", "--output", str(destination)],
    )
    assert error_message(err) == "experience_sync_invalid_catalog"

    async def empty_catalog(_config, envelope):
        return status_payload([catalog_row("old", status="retired")])

    monkeypatch.setattr("oraculo.cli.request", empty_catalog)
    code, _out, err = run(
        monkeypatch,
        capsys,
        ["--config", str(config), "experience", "sync", "--output", str(destination)],
    )
    assert error_message(err) == "experience_sync_no_authorized_projects"

    async def unknown(_config, envelope):
        if envelope["kind"] == "status.get":
            return status_payload([catalog_row("alpha")])
        raise AssertionError("snapshot")

    monkeypatch.setattr("oraculo.cli.request", unknown)
    code, _out, err = run(
        monkeypatch,
        capsys,
        [
            "--config",
            str(config),
            "experience",
            "sync",
            "--output",
            str(destination),
            "--project",
            "missing",
        ],
    )
    assert error_message(err) == "experience_sync_unknown_project:missing"

    async def retired(_config, envelope):
        if envelope["kind"] == "status.get":
            return status_payload([catalog_row("alpha", status="retired")])
        raise AssertionError("snapshot")

    monkeypatch.setattr("oraculo.cli.request", retired)
    code, _out, err = run(
        monkeypatch,
        capsys,
        [
            "--config",
            str(config),
            "experience",
            "sync",
            "--output",
            str(destination),
            "--project",
            "alpha",
        ],
    )
    assert error_message(err) == "experience_sync_project_not_registered:alpha"
    assert not destination.exists()


def test_sync_preserves_last_export_when_refresh_fails(tmp_path, monkeypatch, capsys):
    recorded = install_portfolio(monkeypatch)
    config = write_config(tmp_path)
    destination = tmp_path / "vault"
    state = {"fail": False, "stamp": "2026-11-01T01:00:00+00:00"}

    async def fake_request(_config, envelope):
        if envelope["kind"] == "status.get":
            return status_payload(
                [catalog_row("alpha", title="Alpha"), catalog_row("beta", title="Beta")]
            )
        project_id = envelope["project_id"]
        if state["fail"] and project_id == "beta":
            return {"ok": False, "error": {"message": "snapshot_unavailable"}}
        if state["fail"] and project_id == "alpha":
            return {
                "ok": False,
                "error": {
                    "message": "project_not_registered_or_outside_scope",
                },
            }
        return {
            "ok": True,
            "data": snapshot(project_id, observed_at=state["stamp"], body=project_id),
        }

    monkeypatch.setattr("oraculo.cli.request", fake_request)
    argv = ["--config", str(config), "experience", "sync", "--output", str(destination)]
    code, out, err = run(monkeypatch, capsys, argv)
    assert code == 0, err
    first = (destination / "Inicio.md").read_text()
    assert "2026-11-01T01:00:00+00:00" in first
    state["stamp"] = "2026-11-01T04:00:00+00:00"
    code, out, err = run(monkeypatch, capsys, argv)
    assert code == 0, err
    refreshed = (destination / "Inicio.md").read_text()
    assert refreshed != first
    assert "2026-11-01T04:00:00+00:00" in refreshed
    (destination / "Inicio.md").write_text("nota local\n")
    code, _out, err = run(monkeypatch, capsys, argv)
    assert code == 1
    assert "locally_modified" in error_message(err)
    assert (destination / "Inicio.md").read_text() == "nota local\n"
    (destination / "Inicio.md").write_text(refreshed)
    state["fail"] = True
    code, _out, err = run(
        monkeypatch,
        capsys,
        argv + ["--project", "beta", "--project", "alpha"],
    )
    assert code == 1
    message = error_message(err)
    assert message.startswith("experience_sync_incomplete:")
    assert "beta:experience_snapshot_service_failed:snapshot_unavailable" in message
    assert "alpha:not_authorized" in message
    assert (destination / "Inicio.md").read_text() == refreshed
    assert [item["project_id"] for item in recorded["snapshots"]] == ["alpha", "beta"]


def test_sync_all_unauthorized_names_the_projects(tmp_path, monkeypatch, capsys):
    config = write_config(tmp_path)

    async def fake_request(_config, envelope):
        if envelope["kind"] == "status.get":
            return status_payload([catalog_row("alpha"), catalog_row("beta")])
        return {
            "ok": False,
            "error": {"message": "project_not_registered_or_outside_scope"},
        }

    monkeypatch.setattr("oraculo.cli.request", fake_request)
    code, _out, err = run(
        monkeypatch,
        capsys,
        [
            "--config",
            str(config),
            "experience",
            "sync",
            "--output",
            str(tmp_path / "vault"),
        ],
    )
    assert code == 1
    assert error_message(err) == "experience_sync_no_authorized_projects:alpha,beta"
    assert not (tmp_path / "vault").exists()


def test_sync_timeout_does_not_create_output(tmp_path, monkeypatch, capsys):
    config = write_config(tmp_path)

    async def fake_request(_config, envelope):
        raise TimeoutError("slow")

    monkeypatch.setattr("oraculo.cli.request", fake_request)
    code, _out, err = run(
        monkeypatch,
        capsys,
        [
            "--config",
            str(config),
            "experience",
            "sync",
            "--output",
            str(tmp_path / "vault"),
        ],
    )
    assert code == 1
    assert json.loads(err)["error"] == "TimeoutError"
    assert not (tmp_path / "vault").exists()
