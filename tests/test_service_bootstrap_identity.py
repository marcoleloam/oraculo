"""Independent round5 probes: actual isolated launchd, controlled product core."""

import json

import pytest
from test_service_first_start import (
    isolated as isolated,
)
from test_service_first_start import (
    pytestmark,  # noqa: F401
)

from oraculo import runtime as rt
from oraculo import service_control as sc
from oraculo.config import Config
from oraculo.state import State


@pytest.mark.parametrize("action", ["start", "restart"])
def test_config_swap_after_enable_never_bootstraps_unguarded_state(
    isolated, monkeypatch, action
):
    base, root, conf, ctl, args = isolated
    rt.switch(root, "healthy")
    ctl.install("healthy")
    original_config = Config.load(conf)
    new_text = conf.read_text().replace(
        str(original_config.state_dir), str(base / "other-state")
    )
    alt = base / "other.toml"
    alt.write_text(new_text)
    state = State(Config.load(alt).db_path, max_executors=3)
    state.reserve_managed("uncertain-existing-work", "codex")
    state.close()
    command = sc.ServiceController.command
    fired = False

    def race(self, *argv):
        nonlocal fired
        result = command(self, *argv)
        if argv[0] == "enable" and not fired:
            fired = True
            conf.write_text(new_text)
        return result

    monkeypatch.setattr(sc.ServiceController, "command", race)
    args.service_action = action
    result = sc.dispatch(args)
    observed = ctl.status()
    guard = (
        ctl.request("maintenance.status", {}) if observed["state"] == "online" else None
    )
    old = State(original_config.db_path, max_executors=3)
    old_guard = old.get_meta("runtime_maintenance")
    old.close()
    print(
        "BOOTSTRAP_CONFIG_RACE",
        json.dumps(
            dict(
                action=action,
                result=result,
                observed=observed,
                guard=guard,
                old_guard=old_guard,
            )
        ),
    )
    assert not ctl.inspect_launchd()["loaded"], (
        "changed config started real process against unguarded State with active work"
    )


def test_restart_after_offline_activation_and_rollback_uses_fresh_operation(isolated):
    base, root, conf, ctl, args = isolated
    rt.switch(root, "healthy")
    ctl.install("healthy")
    assert sc.dispatch(args)["state"] == "online"
    args.service_action = "stop"
    assert sc.dispatch(args)["state"] == "unloaded"
    with rt.runtime_lock(root):
        assert (
            rt.activate(root, base / "bin", conf, ctl, "crashing")["state"] == "active"
        )
        assert (
            rt.activate(root, base / "bin", conf, ctl, rollback=True)["state"]
            == "active"
        )
    before = json.loads((root / "service-operation.json").read_text())
    runtime = json.loads((root / "operation.json").read_text())
    assert (
        before["state"] == "complete"
        and before["operation_id"] != runtime["operation_id"]
    )
    args.service_action = "restart"
    result = sc.dispatch(args)
    after = json.loads((root / "service-operation.json").read_text())
    state = State(Config.load(conf).db_path, max_executors=3)
    guard = state.get_meta("runtime_maintenance")
    state.close()
    args.service_action = "start"
    retry = sc.dispatch(args)
    print(
        "COMPLETED_RESTART",
        json.dumps(
            dict(
                result=result,
                retry=retry,
                before=before,
                after=after,
                guard=guard,
                loaded=ctl.inspect_launchd()["loaded"],
            )
        ),
    )
    assert result["state"] == "online", (
        "restart reused a superseded complete operation and orphaned its new maintenance token"
    )
    assert after["operation_id"] != before["operation_id"]


def test_config_swap_inside_bootstrap_rejected_before_state_open(isolated, monkeypatch):
    base, root, conf, ctl, args = isolated
    rt.switch(root, "healthy")
    ctl.install("healthy")
    original = conf.read_text()
    old = Config.load(conf)
    new_text = original.replace(str(old.state_dir), str(base / "other-state"))
    alt = base / "other.toml"
    alt.write_text(new_text)
    alternate = Config.load(alt)
    state = State(alternate.db_path, max_executors=3)
    state.reserve_managed("uncertain-existing-work", "codex")
    state.close()
    before = alternate.db_path.read_bytes()
    command = sc.ServiceController.command

    def race(self, *argv):
        if argv[0] == "bootstrap":
            conf.write_text(new_text)
        return command(self, *argv)

    monkeypatch.setattr(sc.ServiceController, "command", race)
    result = sc.dispatch(args)
    assert result["error_code"] == "start_timeout"
    assert alternate.db_path.read_bytes() == before
    assert not alternate.socket_path.exists()
    assert (
        "startup_config_mismatch"
        in (ctl.logs / (ctl.label + ".stderr.log")).read_text()
    )
    # The immutable child binding rejected changed bytes. Restore the recorded
    # config identity before asking official recovery to stop the owned job.
    conf.write_text(original)
    args.service_action = "stop"
    assert sc.dispatch(args)["state"] == "unloaded"
