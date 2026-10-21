"""Stopped restart must use the same guarded bootstrap as start."""

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
from oraculo.service_control import ServiceController
from oraculo.state import State, StateError


@pytest.mark.skipif(
    sys.platform != "darwin" or os.environ.get("ORACULO_TEST_LAUNCHD") != "1",
    reason="Requires explicit isolated macOS GUI launchd test opt-in",
)
def test_real_stopped_restart_checks_recorded_config_before_bootstrap():
    with tempfile.TemporaryDirectory(prefix="ora-r3-restart-", dir="/tmp") as directory:
        base = Path(directory).resolve()
        root = base / "runtime"
        conf = config(base / "config.toml")
        script = base / "serve.py"
        script.write_text(PRODUCT_DAEMON)
        product_release(root, "healthy", script)
        rt.switch(root, "healthy")
        ctl = ServiceController(
            root,
            conf,
            label="local.oraculo.test." + uuid.uuid4().hex,
            plist_dir=base / "agents",
            log_dir=base / "logs",
            timeout=3,
        )
        ctl.install("healthy")
        args = SimpleNamespace(
            root=root,
            config=conf,
            label=ctl.label,
            plist_dir=ctl.plist.parent,
            log_dir=ctl.logs,
            timeout=3,
            dry_run=False,
            service_action="stop",
        )
        try:
            assert ctl.start()["state"] == "online"
            assert sc.dispatch(args)["state"] == "unloaded"
            old_config = Config.load(conf)
            old_journal = json.loads((root / "service-operation.json").read_text())
            new_dir = base / "other-state"
            conf.write_text(
                conf.read_text().replace(str(old_config.state_dir), str(new_dir))
            )
            new_config = Config.load(conf)
            state = State(new_config.db_path, max_executors=3)
            state.reserve_managed("uncertain-work", "codex")
            state.close()
            args.service_action = "start"
            assert sc.dispatch(args)["error_code"] == "config_changed"
            assert ctl.inspect_launchd()["loaded"] is False
            args.service_action = "restart"
            result = sc.dispatch(args)
            observed = ctl.status()
            current = (
                ctl.request("maintenance.status", {})
                if observed["state"] == "online"
                else None
            )
            print(
                "OBSERVED restart result",
                result,
                "actual process",
                observed,
                "new State guard",
                current,
                "old journal",
                old_journal,
            )
            assert ctl.inspect_launchd()["loaded"] is False, (
                "restart bootstrapped changed config despite pending maintenance identity and uncertain work"
            )
        finally:
            ctl.stop()


@pytest.mark.skipif(
    sys.platform != "darwin" or os.environ.get("ORACULO_TEST_LAUNCHD") != "1",
    reason="Requires explicit isolated macOS GUI launchd test opt-in",
)
def test_real_stopped_restart_records_recovery_before_first_bootstrap():
    with tempfile.TemporaryDirectory(prefix="ora-r3-first-", dir="/tmp") as directory:
        base = Path(directory).resolve()
        root = base / "runtime"
        conf = config(base / "config.toml")
        script = base / "serve.py"
        script.write_text(PRODUCT_DAEMON)
        product_release(root, "crashing", script)
        rt.switch(root, "crashing")
        ctl = ServiceController(
            root,
            conf,
            label="local.oraculo.test." + uuid.uuid4().hex,
            plist_dir=base / "agents",
            log_dir=base / "logs",
            timeout=1,
        )
        ctl.install("crashing")
        state = State(Config.load(conf).db_path, max_executors=3)
        guard = maintenance.begin(state, "migration-completed")
        state.close()
        # Exactly the service journal that successful offline migration leaves.
        rt.write_json(
            root / "service-operation.json",
            dict(
                operation_id=guard["operation_id"],
                token=guard["token"],
                state="stopped",
                config_sha256=rt.config_hash(conf),
            ),
        )
        args = SimpleNamespace(
            root=root,
            config=conf,
            label=ctl.label,
            plist_dir=ctl.plist.parent,
            log_dir=ctl.logs,
            timeout=1,
            dry_run=False,
            service_action="restart",
        )
        try:
            failure = sc.dispatch(args)
            assert failure["error_code"] == "start_timeout"
            assert ctl.inspect_launchd()["loaded"] is True
            args.service_action = "stop"
            recovery = sc.dispatch(args)
            print(
                "OBSERVED failed first restart",
                failure,
                "official stop",
                recovery,
                "journal",
                json.loads((root / "service-operation.json").read_text()),
            )
            assert recovery["state"] == "unloaded", (
                "restart omitted recovery identity before bootstrap, stranding loaded failed first start"
            )
        finally:
            ctl.stop()


def test_completed_token_survives_reopen_and_blocks_pending_memory(tmp_path):
    db = tmp_path / "state.db"
    state = State(db, max_executors=3)
    token = maintenance.begin(state, "recover")["token"]
    maintenance.end(state, "recover", token)
    state.close()
    state = State(db, max_executors=3)
    try:
        state.connection.execute("CREATE TABLE memory_cycles(payload TEXT)")
        state.connection.execute(
            "INSERT INTO memory_cycles VALUES(?)",
            (json.dumps({"write_state": "pending"}),),
        )
        with pytest.raises(StateError, match="work_active_or_uncertain"):
            maintenance.begin(state, "recover")
        assert state.get_meta("runtime_maintenance") is None
        state.connection.execute(
            "UPDATE memory_cycles SET payload=?",
            (json.dumps({"write_state": "saved"}),),
        )
        assert maintenance.begin(state, "recover")["token"] == token
        maintenance.end(state, "recover", token)
    finally:
        state.close()
