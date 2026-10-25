from __future__ import annotations

import contextlib
import json
import os
import plistlib
import shlex
import socket
import sys
import tempfile
import threading
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_runtime_install import config, release

from oraculo import runtime as rt
from oraculo.protocol import MAX_ENVELOPE_BYTES, MAX_RESPONSE_BYTES
from oraculo.service_control import ServiceController


@pytest.fixture
def controller(tmp_path):
    root = tmp_path / "root"
    release(root, "one")
    rt.switch(root, "one")
    conf = config(tmp_path / "config.toml")
    instance = ServiceController(
        root,
        conf,
        label="local.oraculo.test",
        plist_dir=tmp_path / "agents",
        log_dir=tmp_path / "logs",
        timeout=0.2,
        platform="darwin",
    )
    instance.install("one")
    return instance


def result(stdout="", stderr="", code=0):
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=code)


def test_linux_service_explicitly_unsupported(tmp_path):
    instance = ServiceController(tmp_path, tmp_path / "missing", platform="linux")
    assert instance.status()["state"] == "unsupported"
    with pytest.raises(rt.RuntimeError, match="service_unsupported"):
        instance.start()
    assert list(tmp_path.iterdir()) == []


def test_unknown_launchctl_error_not_unloaded(controller):
    controller.runner = lambda *a, **kw: result(stderr="permission denied", code=1)
    assert controller.status()["state"] == "unknown"
    with pytest.raises(rt.RuntimeError, match="launchd_unknown"):
        controller.stop()


def test_missing_job_is_observed_unloaded(controller):
    controller.runner = lambda *a, **kw: result(
        stderr='Could not find service "test" in domain', code=113
    )
    assert controller.status()["state"] == "unloaded"


def test_async_bootout_requires_unloaded_dead_pid_and_absent_socket(
    controller, monkeypatch
):
    calls = []
    states = iter(
        [
            dict(loaded=True, pid=41),
            dict(loaded=True, pid=41),
            dict(loaded=False, pid=None),
            dict(loaded=False, pid=None),
            dict(loaded=False, pid=None),
        ]
    )
    monkeypatch.setattr(controller, "inspect_launchd", lambda: next(states))
    alive = iter([True, False, False])
    monkeypatch.setattr(controller, "alive", lambda pid: next(alive))
    sockets = iter([True, False])
    monkeypatch.setattr(controller, "socket_present", lambda: next(sockets))
    monkeypatch.setattr(
        controller, "command", lambda *args: calls.append(args) or result()
    )
    controller.timeout = 1
    assert controller.stop()["state"] == "unloaded"
    assert calls == [("bootout", controller.target)]


def test_bootout_success_without_termination_times_out(controller, monkeypatch):
    calls = []
    monkeypatch.setattr(
        controller, "inspect_launchd", lambda: dict(loaded=True, pid=41)
    )
    monkeypatch.setattr(
        controller, "command", lambda *args: calls.append(args) or result()
    )
    with pytest.raises(rt.RuntimeError, match="stop_timeout"):
        controller.stop()
    assert calls == [("bootout", controller.target)]


def test_stale_socket_prevents_start(controller, monkeypatch):
    monkeypatch.setattr(
        controller, "inspect_launchd", lambda: dict(loaded=False, pid=None)
    )
    monkeypatch.setattr(controller, "socket_present", lambda: True)
    with pytest.raises(rt.RuntimeError, match="stop_unconfirmed"):
        controller.start()


@pytest.mark.parametrize(
    "field,bad",
    [
        ("pid", 999),
        ("release_id", "old"),
        ("config_sha256", "bad"),
        ("config_path", "/wrong"),
        ("protocol_version", 99),
    ],
)
def test_socket_identity_must_match_launchd_and_config(
    controller, monkeypatch, field, bad
):
    monkeypatch.setattr(
        controller, "inspect_launchd", lambda: dict(loaded=True, pid=41)
    )
    identity = dict(
        pid=41,
        release_id="one",
        config_sha256=rt.digest(controller.config),
        config_path=str(controller.config),
        protocol_version=1,
    )
    identity[field] = bad
    monkeypatch.setattr(
        controller, "request", lambda *args: dict(health="online", runtime=identity)
    )
    assert controller.status()["state"] == "unknown"


def test_start_bootstraps_once_and_waits_for_correlated_socket(controller, monkeypatch):
    calls = []
    observations = iter(
        [
            dict(state="unloaded"),
            dict(state="starting"),
            dict(state="online", pid=42, runtime={"release_id": "one"}),
        ]
    )
    monkeypatch.setattr(controller, "status", lambda: next(observations))
    monkeypatch.setattr(controller, "alive", lambda pid: False)
    monkeypatch.setattr(
        controller, "command", lambda *args: calls.append(args) or result()
    )
    assert controller.start(previous_pid=41)["pid"] == 42
    assert calls == [
        ("enable", controller.target),
        ("bootstrap", controller.domain, str(controller.plist)),
    ]


def test_foreign_plist_preserved(controller):
    original = plistlib.dumps(
        {"Label": controller.label, "ProgramArguments": ["/bin/echo"]}
    )
    controller.plist.write_bytes(original)
    with pytest.raises(rt.RuntimeError, match="plist_conflict"):
        controller.install("one")
    assert controller.plist.read_bytes() == original


@pytest.mark.parametrize("payload_size", [0, MAX_ENVELOPE_BYTES + 100, MAX_RESPONSE_BYTES])
def test_socket_response_correlation_and_full_json(controller, tmp_path, payload_size):
    controller.timeout = 5
    data = {"quiescent": True, "records": "x" * payload_size}
    # Short AF_UNIX path independent of pytest's long temporary tree.
    with tempfile.TemporaryDirectory(prefix="ora-sock-", dir="/tmp") as directory:
        conf = controller.config
        conf.write_text(conf.read_text().replace(str(conf.parent / "state"), directory))
        sock_path = Path(directory) / "control.sock"
        with socket.socket(socket.AF_UNIX) as server:
            server.bind(str(sock_path))
            server.listen()

            def reply():
                connection, _ = server.accept()
                with connection:
                    request = json.loads(connection.makefile("rb").readline())
                    with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                        connection.sendall(
                            json.dumps(
                                dict(
                                    request_id=request["request_id"],
                                    ok=True,
                                    data=data,
                                )
                            ).encode()
                            + b"\n"
                        )

            thread = threading.Thread(target=reply)
            thread.start()
            if payload_size == MAX_RESPONSE_BYTES:
                with pytest.raises(rt.RuntimeError, match="invalid_socket_response"):
                    controller.request("maintenance.status", {})
            else:
                assert controller.request("maintenance.status", {}) == data
            thread.join(timeout=1)
            assert not thread.is_alive()


@pytest.mark.skipif(
    sys.platform != "darwin" or os.environ.get("ORACULO_TEST_LAUNCHD") != "1",
    reason="Requires explicit isolated macOS GUI launchd test opt-in",
)
def test_real_isolated_launchd_start_stop_restart():
    """Real launchd/PID/socket, synthetic daemon; never the production label/config."""
    with tempfile.TemporaryDirectory(prefix="ora-ld-", dir="/tmp") as directory:
        base = Path(directory)
        root = base / "runtime spaces é"
        target = release(root, "fixture")
        conf = config(base / "config.toml")
        (base / "state").mkdir()
        script = base / "daemon.py"
        script.write_text("""import hashlib,json,os,signal,socket,sys,time,tomllib
from pathlib import Path
config=Path(os.environ['ORACULO_CONFIG_PATH'])
sock=Path(tomllib.loads(config.read_text())['state_dir'])/'control.sock'
def stop(*args):
    time.sleep(0.15)
    sock.unlink(missing_ok=True)
    raise SystemExit(0)
signal.signal(signal.SIGTERM,stop)
with socket.socket(socket.AF_UNIX) as server:
    server.bind(str(sock)); server.listen()
    while True:
        client,_=server.accept()
        with client:
            raw=client.makefile('rb').readline(); request=json.loads(raw)
            data={'health':'online','runtime':{'pid':os.getpid(),'release_id':os.environ['ORACULO_RELEASE_ID'],'config_path':str(config),'config_sha256':hashlib.sha256(config.read_bytes()).hexdigest(),'protocol_version':1}}
            client.sendall(json.dumps({'request_id':request['request_id'],'ok':True,'data':data}).encode()+b'\\n')
""")
        python = target / "venv/bin/python"
        python.unlink()
        python.write_text(
            "#!/bin/sh\nexec "
            + shlex.quote(sys.executable)
            + " "
            + shlex.quote(str(script))
            + ' "$@"\n'
        )
        python.chmod(0o755)
        value = json.loads((target / "manifest.json").read_text())
        value.update(
            interpreter_sha256=rt.digest(python),
            files=rt.inventory(target, symlinks=True),
        )
        rt.write_json(target / "manifest.json", value)
        rt.switch(root, "fixture")
        ctl = ServiceController(
            root,
            conf,
            label="local.oraculo.test." + uuid.uuid4().hex,
            plist_dir=base / "agents",
            log_dir=base / "logs",
            timeout=8,
        )
        ctl.install("fixture")
        try:
            initial = ctl.start()
            old_pid = initial["pid"]
            assert initial["state"] == "online"
            stopped = ctl.stop()
            assert stopped["state"] == "unloaded" and not ctl.alive(old_pid)
            restarted = ctl.start(previous_pid=old_pid)
            assert restarted["pid"] != old_pid and restarted["state"] == "online"
        finally:
            ctl.stop()


PRODUCT_DAEMON = """import asyncio,os,signal,socket
from pathlib import Path
from oraculo.config import Config
from oraculo.service import Service,Singleton
from oraculo.protocol import MAX_ENVELOPE_BYTES
async def main():
    path=Path(os.environ['ORACULO_CONFIG_PATH']); config=Config.load(path)
    with Singleton(config.state_dir):
        if os.environ['ORACULO_RELEASE_ID']=='crashing':
            raise SystemExit(17)
        service=Service(config,path); service.health='auth_required' if os.environ['ORACULO_RELEASE_ID']=='degraded' else 'online'
        async def unhealthy(reader,writer):
            await reader.readline(); writer.close(); await writer.wait_closed()
        handler=unhealthy if os.environ['ORACULO_RELEASE_ID']=='unhealthy' else service.connection
        server=await asyncio.start_unix_server(handler,path=str(config.socket_path),limit=MAX_ENVELOPE_BYTES+1)
        for sig in (signal.SIGTERM,signal.SIGINT):
            asyncio.get_running_loop().add_signal_handler(sig,service.stop.set)
        try:
            async with server: await service.stop.wait()
        finally:
            service.state.close(); config.socket_path.unlink(missing_ok=True)
asyncio.run(main())
"""


def product_release(root, name, script):
    target = release(root, name)
    python = target / "venv/bin/python"
    python.unlink()
    python.write_text(
        "#!/bin/sh\nexport PYTHONPATH="
        + shlex.quote(str(Path(__file__).resolve().parents[1] / "src"))
        + "\nexec "
        + shlex.quote(sys.executable)
        + " -B "
        + shlex.quote(str(script))
        + "\n"
    )
    python.chmod(0o755)
    value = json.loads((target / "manifest.json").read_text())
    value.update(
        interpreter_sha256=rt.digest(python), files=rt.inventory(target, symlinks=True)
    )
    rt.write_json(target / "manifest.json", value)
    return target


def recovery_journal(controller):
    journal = dict(
        operation_id="test-maintenance",
        token="durable-token",
        state="recovery_required",
        target="one",
        previous="old",
        previous_pid=None,
        config_sha256=rt.config_hash(controller.config),
        replacement=controller.recovery_identity("one"),
        replacement_maintenance=dict(
            operation_id="test-maintenance", token="durable-token"
        ),
    )
    rt.write_json(controller.root / "operation.json", journal)
    return journal


def loaded_identity(controller, **changes):
    expected = controller.plist_value("one")["ProgramArguments"]
    return dict(
        loaded=True,
        pid=None,
        path=str(controller.plist),
        program=expected[0],
        arguments=expected,
        environment=controller.plist_value("one")["EnvironmentVariables"],
        working_directory=controller.plist_value("one")["WorkingDirectory"],
        **changes,
    )


@pytest.mark.parametrize(
    "fault",
    [
        "token",
        "target",
        "config",
        "plist",
        "foreign",
        "program",
        "arguments",
        "path",
        "unknown",
        "socket_identity",
    ],
)
def test_recovery_stop_refuses_unproven_identity(controller, monkeypatch, fault):
    journal = recovery_journal(controller)
    loaded = loaded_identity(controller)
    status = "loaded"
    if fault == "token":
        journal["token"] = None
    elif fault == "target":
        journal["target"] = "foreign"
    elif fault == "config":
        controller.config.write_text(
            controller.config.read_text() + "\npoll_idle_seconds=7\n"
        )
    elif fault in {"plist", "foreign"}:
        value = controller.owned_plist()
        value["KeepAlive" if fault == "plist" else "OraculoManagedRoot"] = False
        controller.plist.write_bytes(plistlib.dumps(value))
    elif fault in {"program", "path", "arguments"}:
        loaded[fault] = "/foreign"
    elif fault == "unknown":
        loaded["loaded"] = None
    else:
        status = "unknown"
    monkeypatch.setattr(controller, "status", lambda: dict(state=status))
    monkeypatch.setattr(controller, "inspect_launchd", lambda: loaded)
    calls = []
    monkeypatch.setattr(
        controller, "command", lambda *args: calls.append(args) or result()
    )
    with pytest.raises(rt.RuntimeError):
        controller.recovery_stop(journal)
    assert not calls


def test_recovery_stop_records_and_checks_old_pids(controller, monkeypatch):
    journal = recovery_journal(controller)
    journal["recovery_pids"] = [789]
    monkeypatch.setattr(controller, "status", lambda: dict(state="unloaded"))
    monkeypatch.setattr(
        controller, "inspect_launchd", lambda: dict(loaded=False, pid=None)
    )
    monkeypatch.setattr(controller, "alive", lambda pid: pid == 789)
    with pytest.raises(rt.RuntimeError, match="stop_timeout"):
        controller.recovery_stop(journal)
    assert json.loads((controller.root / "operation.json").read_text())[
        "recovery_pids"
    ] == [789]


@pytest.mark.skipif(
    sys.platform != "darwin" or os.environ.get("ORACULO_TEST_LAUNCHD") != "1",
    reason="Requires explicit isolated macOS GUI launchd test opt-in",
)
@pytest.mark.parametrize("fault", ["unhealthy", "crashing", "degraded"])
def test_real_launchd_failed_replacement_recovers_and_rolls_back(fault, monkeypatch):
    """Real launchd/PID/socket + product handler/State/maintenance, no remote loop."""
    from oraculo import service_control as sc
    from oraculo.config import Config
    from oraculo.state import State

    with tempfile.TemporaryDirectory(prefix="ora-recover-", dir="/tmp") as directory:
        base = Path(directory)
        root, bin_dir = base / "runtime spaces é", base / "bin"
        conf = config(base / "config.toml")
        script = base / "serve.py"
        script.write_text(PRODUCT_DAEMON)
        for name in ("healthy", fault):
            product_release(root, name, script)
        rt.switch(root, "healthy")
        ctl = ServiceController(
            root,
            conf,
            label="local.oraculo.test." + uuid.uuid4().hex,
            plist_dir=base / "agents",
            log_dir=base / "logs",
            timeout=2,
        )
        ctl.install("healthy")
        try:
            initial = ctl.start()
            old_pid = initial["pid"]
            before = ctl.request("status.get", {})["capacity"]
            with pytest.raises(rt.RuntimeError, match="start_timeout"):
                with rt.runtime_lock(root):
                    rt.activate(root, bin_dir, conf, ctl, fault)
            journal = json.loads((root / "operation.json").read_text())
            assert ctl.inspect_launchd()["loaded"] is True
            assert ctl.status()["state"] in {"loaded", "starting", "failed"}
            assert not ctl.alive(old_pid)
            # Exercise both advertised entrypoints against genuinely loaded failures.
            if fault == "unhealthy":
                args = SimpleNamespace(
                    root=root,
                    config=conf,
                    label=ctl.label,
                    plist_dir=ctl.plist.parent,
                    log_dir=ctl.logs,
                    timeout=2,
                    dry_run=False,
                    service_action="stop",
                )
                stopped = sc.dispatch(args)
                assert stopped["state"] == "unloaded", stopped
                assert stopped["operation_id"] == journal["operation_id"]
                assert not Config.load(conf).socket_path.exists()
            if fault == "crashing":
                install = ctl.install

                def fail_install(release_id):
                    raise rt.RuntimeError("injected_rollback_install")

                monkeypatch.setattr(ctl, "install", fail_install)
                with pytest.raises(rt.RuntimeError, match="injected_rollback_install"):
                    with rt.runtime_lock(root):
                        rt.activate(root, bin_dir, conf, ctl, rollback=True)
                assert rt.current_release(root) == "healthy"
                assert ctl.inspect_launchd()["loaded"] is False
                assert (
                    json.loads((root / "operation.json").read_text())["state"]
                    == "recovery_required"
                )
                monkeypatch.setattr(ctl, "install", install)
            with rt.runtime_lock(root):
                active = rt.activate(root, bin_dir, conf, ctl, rollback=True)
            assert active["state"] == "active"
            final = ctl.status()
            assert final["state"] == "online" and final["pid"] != old_pid
            assert final["runtime"]["release_id"] == "healthy"
            finished = json.loads((root / "operation.json").read_text())
            assert finished["operation_id"] == journal["operation_id"]
            assert (
                finished["token"] == journal["token"]
                and finished["state"] == "complete"
            )
            assert ctl.request("status.get", {})["capacity"] == before
            assert ctl.request("maintenance.status", {})["admission_closed"] is False
        finally:
            ctl.stop()
        state = State(Config.load(conf).db_path, max_executors=3)
        try:
            assert state.capacity()["effective"] == 3
        finally:
            state.close()


@pytest.mark.skipif(
    sys.platform != "darwin" or os.environ.get("ORACULO_TEST_LAUNCHD") != "1",
    reason="Requires explicit isolated macOS GUI launchd test opt-in",
)
def test_real_migration_failed_first_start_stop_then_corrected_release():
    """Real offline guard, backup, launchd, unhealthy recovery and forward repair."""
    import subprocess

    from oraculo import service_control as sc
    from oraculo.config import Config
    from oraculo.runtime_migration import migrate
    from oraculo.state import State

    with tempfile.TemporaryDirectory(prefix="ora-migrate-", dir="/tmp") as directory:
        base = Path(directory)
        root, candidate = base / "legacy", base / "candidate"
        bin_dir = base / "bin"
        conf = config(base / "config.toml")
        state = State(Config.load(conf).db_path, max_executors=3)
        state.set_capacity(3)
        state.close()
        old = root / "releases/legacy"
        (old / ".venv/bin").mkdir(parents=True)
        (old / ".venv/bin/python").write_text("unverified legacy interpreter")
        (root / "current").symlink_to("releases/legacy")
        script = base / "serve.py"
        script.write_text(PRODUCT_DAEMON)
        product_release(candidate, "crashing", script)
        product_release(root, "healthy", script)
        ctl = ServiceController(
            root,
            conf,
            label="local.oraculo.test." + uuid.uuid4().hex,
            plist_dir=base / "agents",
            log_dir=base / "logs",
            timeout=2,
        )
        raw = plistlib.dumps(
            dict(
                Label=ctl.label,
                ProgramArguments=[
                    str(old / ".venv/bin/python"),
                    "-m",
                    "oraculo",
                    "--config",
                    str(ctl.config),
                    "serve",
                ],
                WorkingDirectory=str(old),
            )
        )
        rt.atomic_write(ctl.plist, raw)
        child = subprocess.Popen(["/usr/bin/true"])
        child.wait(timeout=2)
        args = SimpleNamespace(
            root=root,
            bin_dir=bin_dir,
            config=conf,
            label=ctl.label,
            plist_dir=ctl.plist.parent,
            log_dir=ctl.logs,
            timeout=2,
            dry_run=False,
            service_action="start",
        )
        with rt.runtime_lock(root):
            migrated = migrate(
                root,
                bin_dir,
                conf,
                ctl,
                candidate,
                "crashing",
                "releases/legacy",
                rt.digest(ctl.plist),
                child.pid,
            )
        assert migrated["state"] == "migrated"
        assert ctl.inspect_launchd()["loaded"] is False
        try:
            failed = sc.dispatch(args)
            assert failed["error_code"] == "start_timeout", failed
            assert ctl.inspect_launchd()["loaded"] is True
            args.service_action = "stop"
            stopped = sc.dispatch(args)
            assert stopped["state"] == "unloaded", stopped
            assert stopped["operation_id"] == migrated["operation_id"]
            with rt.runtime_lock(root):
                repaired = rt.activate(root, bin_dir, conf, ctl, "healthy")
            assert repaired["state"] == "active"
            assert repaired["operation_id"] == migrated["operation_id"]
            assert repaired["service_started"] is False
            args.service_action = "start"
            started = sc.dispatch(args)
            assert started["state"] == "online", started
            assert started["runtime"]["release_id"] == "healthy"
            assert ctl.request("status.get", {})["capacity"]["requested"] == 3
            assert ctl.request("maintenance.status", {})["admission_closed"] is False
            assert (
                old / ".venv/bin/python"
            ).read_text() == "unverified legacy interpreter"
        finally:
            ctl.stop()


@pytest.mark.parametrize(
    "fault", ["unreachable", "reopened", "work_active", "different_token"]
)
def test_lost_end_ack_requires_current_maintenance_proof_before_bootout(
    controller, monkeypatch, fault
):
    journal = recovery_journal(controller)
    journal["maintenance_end_attempted"] = True
    guard = dict(
        operation_id=journal["operation_id"],
        token=journal["token"],
        admission_closed=True,
        quiescent=True,
    )
    if fault == "reopened":
        guard["admission_closed"] = False
    elif fault == "work_active":
        guard["quiescent"] = False
    elif fault == "different_token":
        guard["token"] = "different"

    def request(kind, payload):
        assert kind == "maintenance.status"
        if fault == "unreachable":
            raise TimeoutError("unreachable maintenance after lost end ACK")
        return guard

    monkeypatch.setattr(controller, "request", request)
    calls = []
    monkeypatch.setattr(
        controller, "command", lambda *args: calls.append(args) or result()
    )
    with pytest.raises(
        rt.RuntimeError, match="maintenance_unproven|maintenance_token_mismatch"
    ):
        controller.recovery_stop(journal)
    assert not calls
