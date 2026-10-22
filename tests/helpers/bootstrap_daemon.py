"""Disposable exact-package Service runner; host and lifecycle are test controls."""

import asyncio
import json
import sys
from pathlib import Path

import oraculo.service
from oraculo.config import Config
from oraculo.protocol import MAX_ENVELOPE_BYTES
from oraculo.service import Service, Singleton


async def main():
    path = Path(sys.argv[1])
    config = Config.load(path)
    with Singleton(config.state_dir):
        service = Service(config, path)
        service.publisher.initialize()
        service.publisher.initialize_project("alpha")
        service.state.register_project("alpha", str(config.projects_root / "alpha"))
        service.catalog = {
            "providers": [{"providerInstanceId": "codex", "models": ["current"]}]
        }
        service.health = "disconnected"
        entered, release = asyncio.Event(), asyncio.Event()

        class Host:
            async def call(self, name, args):
                assert name == "t3_thread_send", name
                entered.set()
                await release.wait()
                return dict(
                    threadId=args["threadId"], messageId=args["clientRequestId"]
                )

        server = await asyncio.start_unix_server(
            service.connection,
            path=str(config.socket_path),
            limit=MAX_ENVELOPE_BYTES + 1,
        )
        print(
            json.dumps({"ready": True, "service_module": oraculo.service.__file__}),
            flush=True,
        )
        try:
            async with server:
                while line := await asyncio.to_thread(sys.stdin.readline):
                    action = json.loads(line)["action"]
                    if action == "stop":
                        break
                    if action == "background":
                        await service.reconcile_managed_tasks(Host())
                        await asyncio.wait_for(entered.wait(), 3)
                    elif action == "cancel-background":
                        service.managed_flush.cancel()
                        await asyncio.gather(
                            service.managed_flush, return_exceptions=True
                        )
                    elif action == "release-background":
                        release.set()
                    else:
                        raise AssertionError(action)
                    print(
                        json.dumps(
                            {
                                "busy": service.managed_service.busy,
                                "worker_entered": entered.is_set(),
                            }
                        ),
                        flush=True,
                    )
        finally:
            release.set()
            if service.managed_flush:
                await asyncio.gather(service.managed_flush, return_exceptions=True)
            # Let the intentionally orphaned legacy worker finish before closing.
            await asyncio.get_running_loop().shutdown_default_executor()
            if service.managed_service:
                service.managed_service.close()
            service.state.close()


asyncio.run(main())
