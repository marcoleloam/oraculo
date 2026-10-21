import asyncio
import json
import tempfile
from pathlib import Path

import pytest

from oraculo.config import Config, ProviderConfig
from oraculo.protocol import MAX_ENVELOPE_BYTES
from oraculo.service import Service


@pytest.fixture
def service_config(tmp_path):
    with tempfile.TemporaryDirectory(prefix="oraculo-", dir="/tmp") as short:
        config = Config(
            projects_root=tmp_path / "projects",
            vault_root=tmp_path / "vault",
            state_dir=Path(short),
            providers={
                "codex": ProviderConfig(
                    instance_id="codex", billing_verified=True, models=["current"]
                )
            },
        )
        (config.projects_root / "alpha").mkdir(parents=True)
        path = tmp_path / "config.toml"
        path.write_text(
            "\n".join(
                f"{key} = {json.dumps(str(value))}"
                for key, value in {
                    "projects_root": config.projects_root,
                    "vault_root": config.vault_root,
                    "state_dir": config.state_dir,
                }.items()
            )
        )
        yield config, path


@pytest.fixture
async def running_service(service_config):
    config, path = service_config
    service = Service(config, path)
    service.publisher.initialize()
    service.publisher.initialize_project("alpha")
    service.state.register_project("alpha", str(config.projects_root / "alpha"))
    service.state.set_coordinator("alpha", "coordinator")
    service.catalog = {
        "providers": [{"providerInstanceId": "codex", "models": ["current"]}]
    }
    service.health = "disconnected"
    server = await asyncio.start_unix_server(
        service.connection, path=str(config.socket_path), limit=MAX_ENVELOPE_BYTES + 1
    )
    async with server:
        yield service
    if service.managed_service is not None:
        service.managed_service.close()
    service.state.close()
