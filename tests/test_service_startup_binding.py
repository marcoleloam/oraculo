"""The launchd config binding is checked before creating any daemon State."""

import hashlib

import pytest
from test_runtime_install import config as write_config

from oraculo.config import Config
from oraculo.service import Service, serve
from oraculo.state import StateError


@pytest.mark.parametrize("race", ["wrong_expected", "file_replaced", "missing_binding"])
async def test_startup_binding_rejects_before_creating_state(
    tmp_path, monkeypatch, race
):
    path = write_config(tmp_path / "config.toml")
    loaded = Config.load(path)
    expected = hashlib.sha256(path.read_bytes()).hexdigest()
    if race == "wrong_expected":
        expected = "0" * 64
    elif race == "file_replaced":
        path.write_text(path.read_text() + "\n# replaced after parse\n")
    else:
        expected = ""
    monkeypatch.setenv("ORACULO_EXPECTED_CONFIG_SHA256", expected)
    assert not loaded.state_dir.exists()
    with pytest.raises(StateError, match="startup_config_mismatch"):
        Service(loaded, path)
    with pytest.raises(StateError, match="startup_config_mismatch"):
        await serve(loaded, path)
    assert not loaded.state_dir.exists()
