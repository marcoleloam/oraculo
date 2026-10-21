import fcntl
import hashlib
import json
import os
import socket
import sqlite3

import pytest

from oraculo.cli import main
from oraculo.service import Singleton


def test_init_preserves_legacy_database_without_migration(service_config, capsys):
    config, path = service_config
    with sqlite3.connect(config.db_path) as db:
        db.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY)")
        db.execute("INSERT INTO schema_migrations VALUES(3)")
        db.execute("CREATE TABLE existing_work(id TEXT)")
        db.execute("INSERT INTO existing_work VALUES('preserve-me')")
    before = config.db_path.read_bytes()
    assert main(["--config", str(path), "init"]) == 1
    assert "init_requires_new_state" in capsys.readouterr().err
    assert config.db_path.read_bytes() == before
    assert not config.vault_root.exists()
    with sqlite3.connect(config.db_path) as db:
        assert db.execute("SELECT version FROM schema_migrations").fetchall() == [(3,)]


@pytest.mark.parametrize("owner", ["daemon", "managed"])
def test_init_refuses_existing_writer_even_without_database(service_config, owner):
    config, path = service_config
    if owner == "daemon":
        with Singleton(config.state_dir):
            assert main(["--config", str(path), "init"]) == 1
    else:
        fd = os.open(
            config.state_dir / "managed-tasks.lock", os.O_CREAT | os.O_RDWR, 0o600
        )
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            assert main(["--config", str(path), "init"]) == 1
        finally:
            os.close(fd)
    assert not config.db_path.exists()
    assert not config.vault_root.exists()


@pytest.mark.parametrize("kind", ["wal", "ledger", "dangling_database", "socket"])
def test_init_refuses_partial_state(service_config, kind):
    config, path = service_config
    listener = None
    if kind == "wal":
        config.db_path.with_name(config.db_path.name + "-wal").write_bytes(b"preserve")
    elif kind == "ledger":
        (config.state_dir / "managed-tasks.sqlite3").write_bytes(b"preserve")
    elif kind == "dangling_database":
        config.db_path.symlink_to(config.state_dir / "missing-database")
    else:
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(str(config.socket_path))
    try:
        assert main(["--config", str(path), "init"]) == 1
        assert not config.db_path.exists()
        assert not config.vault_root.exists()
    finally:
        if listener:
            listener.close()


def test_init_new_state_then_repeat_preserves_database(service_config, capsys):
    config, path = service_config
    assert main(["--config", str(path), "init"]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "initialized"
    assert (config.vault_root / ".git").is_dir()
    before = hashlib.sha256(config.db_path.read_bytes()).hexdigest()
    assert main(["--config", str(path), "init"]) == 1
    assert hashlib.sha256(config.db_path.read_bytes()).hexdigest() == before
    # Both writer locks were released, including on the rejected retry.
    with Singleton(config.state_dir):
        pass
