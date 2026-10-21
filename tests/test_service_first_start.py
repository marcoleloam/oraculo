"""First start and retries preserve recoverable maintenance authority."""

import json
import os
import sys
import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_runtime_install import config
from test_service_control import PRODUCT_DAEMON, product_release

from oraculo import maintenance
from oraculo import runtime as rt
from oraculo import service_control as sc
from oraculo.config import Config
from oraculo.state import State

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin" or os.environ.get("ORACULO_TEST_LAUNCHD") != "1",
    reason="Requires explicit isolated macOS GUI launchd test opt-in",
)


@pytest.fixture
def isolated():
    with tempfile.TemporaryDirectory(prefix="ora-r4-", dir="/tmp") as directory:
        base = Path(directory).resolve()
        root = base / "runtime"
        conf = config(base / "config.toml")
        script = base / "serve.py"
        script.write_text(PRODUCT_DAEMON)
        for name in ("healthy", "crashing"):
            product_release(root, name, script)
        ctl = sc.ServiceController(
            root,
            conf,
            label="local.oraculo.test." + uuid.uuid4().hex,
            plist_dir=base / "agents",
            log_dir=base / "logs",
            timeout=1.5,
        )
        args = SimpleNamespace(
            root=root,
            config=conf,
            label=ctl.label,
            plist_dir=ctl.plist.parent,
            log_dir=ctl.logs,
            timeout=1.5,
            dry_run=False,
            service_action="start",
        )
        state = State(Config.load(conf).db_path, max_executors=3)
        state.close()
        try:
            yield base, root, conf, ctl, args
        finally:
            ctl.stop()


@pytest.mark.parametrize("action", ["start", "restart"])
def test_first_start_failure_has_official_recovery(isolated, action):
    base, root, conf, ctl, args = isolated
    with rt.runtime_lock(root):
        activation = rt.activate(root, base / "bin", conf, ctl, "crashing")
    assert activation["state"] == "active" and activation["service_started"] is False
    args.service_action = action
    failed = sc.dispatch(args)
    assert (
        failed["error_code"] == "start_timeout"
        and ctl.inspect_launchd()["loaded"] is True
    )
    args.service_action = "stop"
    recovery = sc.dispatch(args)
    print(
        "FIRST_START",
        {
            "activation": activation,
            "failure": failed,
            "recovery": recovery,
            "loaded": ctl.inspect_launchd()["loaded"],
            "service_journal_exists": (root / "service-operation.json").exists(),
        },
    )
    assert recovery["state"] == "unloaded", (
        "official first service start leaves loaded failed job with no usable recovery journal"
    )


@pytest.mark.parametrize("action", ["start", "restart"])
def test_config_swap_during_starting_journal_write_blocks_bootstrap(
    isolated, monkeypatch, action
):
    base, root, conf, ctl, args = isolated
    rt.switch(root, "healthy")
    ctl.install("healthy")
    state = State(Config.load(conf).db_path, max_executors=3)
    guard = maintenance.begin(state, "round4-race")
    state.close()
    path = root / "service-operation.json"
    rt.write_json(
        path,
        dict(
            operation_id=guard["operation_id"],
            token=guard["token"],
            state="stopped",
            config_sha256=rt.config_hash(conf),
        ),
    )
    old = conf.read_text()
    write = sc.write_json

    def race(p, v):
        write(p, v)
        if p == path and v["state"] == "starting":
            conf.write_text(old + "\n# concurrent config replacement\n")

    monkeypatch.setattr(sc, "write_json", race)
    args.service_action = action
    result = sc.dispatch(args)
    assert (
        result["error_code"] == "config_changed"
        and ctl.inspect_launchd()["loaded"] is False
    )
    assert json.loads(path.read_text())["token"] == guard["token"]
    conf.write_text(old)


def test_restart_recovers_end_ack_loss_with_same_token(isolated, monkeypatch):
    base, root, conf, ctl, args = isolated
    rt.switch(root, "healthy")
    ctl.install("healthy")
    assert ctl.start()["state"] == "online"
    original = sc.ServiceController.request
    fired = False

    def request(self, kind, payload):
        nonlocal fired
        result = original(self, kind, payload)
        if kind == "maintenance.end" and not fired:
            fired = True
            raise OSError("injected ACK loss after successful end")
        return result

    monkeypatch.setattr(sc.ServiceController, "request", request)
    args.service_action = "restart"
    failed = sc.dispatch(args)
    assert failed["state"] == "failed"
    old = json.loads((root / "service-operation.json").read_text())
    assert ctl.request("maintenance.status", {})["admission_closed"] is False
    final = sc.dispatch(args)
    journal = json.loads((root / "service-operation.json").read_text())
    print(
        "RESTART_ACK_LOSS",
        {
            "first": failed,
            "second": final["state"],
            "old_token": old["token"],
            "new_token": journal["token"],
        },
    )
    assert (
        final["state"] == "online"
        and journal["state"] == "complete"
        and journal["token"] == old["token"]
    )


@pytest.mark.parametrize("action", ["start", "restart"])
def test_first_bootstrap_blocks_existing_work(isolated, action):
    base, root, conf, ctl, args = isolated
    rt.switch(root, "healthy")
    ctl.install("healthy")
    state = State(Config.load(conf).db_path, max_executors=3)
    try:
        state.reserve_managed("existing-work", "codex")
        args.service_action = action
        result = sc.dispatch(args)
        assert result["error_code"] == "maintenance_work_active_or_uncertain"
        assert ctl.inspect_launchd()["loaded"] is False
        assert state.capacity()["occupied"] == 1
        assert state.get_meta("runtime_maintenance") is None
    finally:
        state.close()


@pytest.mark.parametrize(
    "fault_state", ["acquiring_maintenance", "starting", "complete"]
)
def test_first_bootstrap_retries_journal_faults(isolated, monkeypatch, fault_state):
    base, root, conf, ctl, args = isolated
    rt.switch(root, "healthy")
    ctl.install("healthy")
    original = sc.write_json
    fired = False

    def write(path, value):
        nonlocal fired
        if (
            path == root / "service-operation.json"
            and value.get("state") == fault_state
            and not fired
        ):
            fired = True
            raise OSError("injected first-start journal fault")
        original(path, value)

    monkeypatch.setattr(sc, "write_json", write)
    failed = sc.dispatch(args)
    assert failed["state"] == "failed"
    state = State(Config.load(conf).db_path, max_executors=3)
    try:
        pending = state.get_meta("runtime_maintenance")
    finally:
        state.close()
    assert sc.dispatch(args)["state"] == "online"
    journal = json.loads((root / "service-operation.json").read_text())
    assert journal["state"] == "complete"
    if pending:
        assert journal["token"] == pending["token"]
    assert ctl.request("maintenance.status", {})["admission_closed"] is False


def test_completed_service_journal_gets_new_guard_before_later_bootstrap(isolated):
    base, root, conf, ctl, args = isolated
    rt.switch(root, "healthy")
    ctl.install("healthy")
    assert sc.dispatch(args)["state"] == "online"
    first = json.loads((root / "service-operation.json").read_text())
    ctl.stop()
    rt.switch(root, "crashing")
    ctl.install("crashing")
    assert sc.dispatch(args)["error_code"] == "start_timeout"
    second = json.loads((root / "service-operation.json").read_text())
    assert second["operation_id"] != first["operation_id"]
    assert second["token"] != first["token"]
    assert second["replacement"]["release_id"] == "crashing"
    args.service_action = "stop"
    assert sc.dispatch(args)["state"] == "unloaded"
