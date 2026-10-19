import json
import os
import subprocess
import sys

from oraculo.cli import main
from oraculo.service import Singleton
from oraculo.state import State

CHILD = """
import asyncio
import sys
from pathlib import Path
from oraculo.config import Config
from oraculo.service import Service, serve
async def offline(self):
    self.health = 'disconnected'
    await self.stop.wait()
Service.loop = offline
path = Path(sys.argv[1])
asyncio.run(serve(Config.load(path), path))
"""


def test_singleton_restart_preserves_state_and_socket_permissions(service_config):
    import socket
    import time

    config, path = service_config

    def start():
        return subprocess.Popen(
            [sys.executable, "-c", CHILD, str(path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def wait_status(process):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise AssertionError(process.communicate())
            try:
                with socket.socket(socket.AF_UNIX) as client:
                    client.settimeout(1)
                    client.connect(str(config.socket_path))
                    client.sendall(
                        b'{"schema_version":1,"request_id":"status","kind":"status.get","project_id":"*","origin":{},"payload":{}}\n'
                    )
                    return json.loads(client.recv(65536))
            except OSError:
                time.sleep(0.02)
        raise AssertionError("daemon socket not ready")

    state = State(config.db_path)
    state.register_project("alpha", str(config.projects_root / "alpha"))
    objective = state.submit_objective("alpha", "Durable pending objective")
    state.enqueue_job(objective["id"], "implementation", {})
    state.close()
    first = start()
    try:
        assert wait_status(first)["ok"]
        assert os.stat(config.socket_path).st_mode & 0o777 == 0o600
        assert os.stat(config.state_dir).st_mode & 0o777 == 0o700
        duplicate = subprocess.run(
            [sys.executable, "-c", CHILD, str(path)], capture_output=True, timeout=10
        )
        assert duplicate.returncode != 0
        assert b"BlockingIOError" in duplicate.stderr
        assert wait_status(first)["ok"]
    finally:
        first.terminate()
        first.communicate(timeout=10)
    assert not config.socket_path.exists()
    second = start()
    try:
        assert wait_status(second)["ok"]
        status = wait_status(second)
        assert status["data"]["objectives"][0]["id"] == objective["id"]
        assert len(status["data"]["jobs"]) == 1
        assert config.db_path.exists()
    finally:
        second.terminate()
        second.communicate(timeout=10)
    assert not config.socket_path.exists()


def test_auth_login_does_not_run_while_daemon_lock_active(
    service_config, monkeypatch, capsys
):
    config, path = service_config
    calls = []

    async def fake_login(url):
        calls.append(url)

    monkeypatch.setattr("oraculo.t3.auth.login", fake_login)
    with Singleton(config.state_dir):
        assert main(["--config", str(path), "auth", "login"]) == 1
    assert not calls
    assert "BlockingIOError" in capsys.readouterr().err
    assert main(["--config", str(path), "auth", "login"]) == 0
    assert len(calls) == 1
