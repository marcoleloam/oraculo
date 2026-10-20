"""Foreground acceptance driver: isolated State + real Service/Reconciler, no daemon.

The T3 parent supplies exact catalog, identities and snapshots through JSON files.
Dispatch and successor selection are performed by Service. The parent adapter only
executes emitted delegation intents and reports observed results; never overrides
job state. No installed service/config/vault is opened by this driver.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from oraculo.cli import make_envelope
from oraculo.config import Config, ProviderConfig
from oraculo.protocol import Envelope
from oraculo.reconcile import Reconciler
from oraculo.service import Service


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "action", choices=["init", "next", "claim", "complete", "status"]
    )
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    args = parser.parse_args()
    data = json.loads(args.input.read_text())
    args.state.mkdir(parents=True, exist_ok=True, mode=0o700)
    settings_path = args.state / "settings.json"
    if args.action == "init":
        if settings_path.exists():
            raise RuntimeError("existing rehearsal; resume instead")
        settings_path.write_text(json.dumps(data))
    settings = json.loads(settings_path.read_text())
    root = Path(settings["workspace"])
    config = Config(
        projects_root=root.parent,
        allowed_projects=[root],
        state_dir=args.state,
        vault_root=args.state.parent / (args.state.name + "-vault"),
        max_executors=2,
        max_correction_rounds=0,
        providers={
            "codex": ProviderConfig(
                instance_id="codex", billing_verified=True, models=[settings["model"]]
            )
        },
        profiles={
            "implementation": ["codex"],
            "tests": ["codex"],
            "architecture": ["codex"],
            "discovery": [],
        },
    )
    service = Service(config, args.state / "unused-config.toml")
    service.catalog = settings["catalog"]
    project_id = settings["project_id"]
    origin = {"thread_id": settings["thread_id"], "run_id": settings["run_id"]}

    async def command(kind, payload, identity=None):
        envelope = make_envelope(kind, project_id, payload)
        envelope["origin"] = identity or origin
        response = await service.handle(Envelope.model_validate(envelope))
        if not response["ok"]:
            raise RuntimeError(response)
        return response["data"]

    class Adapter:
        def __init__(self):
            self.dispatches = []

        async def send(self, thread, message, key, **kwargs):
            self.dispatches.append({"key": key, "body": json.loads(message)})
            return {"runId": settings["run_id"]}

        async def read_thread(self, thread, **kwargs):
            return data["snapshots"][thread]

        async def list_threads(self, project_id, **kwargs):
            return [s["thread"] for s in data["snapshots"].values()]

    try:
        if args.action == "init":
            service.state.register_project(project_id, str(root))
            service.state.set_coordinator(project_id, settings["thread_id"])
            output = await command(
                "plan.submit",
                {"dedupe_key": "ora-cont-003-real-v1", "plan": settings["plan"]},
            )
            service.state.set_meta("rehearsal_objective", output["objective_id"])
        elif args.action == "next":
            adapter = Adapter()
            await service.dispatch(adapter)
            output = {"dispatches": []}
            for entry in adapter.dispatches:
                if entry["body"].get("kind") == "dispatch":
                    granted = await command(
                        "grant.acquire", {"job_id": entry["body"]["job_id"]}
                    )
                    output["dispatches"].append(dict(entry, grant=granted))
        elif args.action == "claim":
            output = await command("grant.claim", data)
        elif args.action == "complete":
            output = {"reports": []}
            for report in data["reports"]:
                attempt = service.state.get_attempt(report["attempt_id"])
                output["reports"].append(
                    await command(
                        "execution.report",
                        report,
                        {
                            "thread_id": attempt["thread_id"],
                            "run_id": attempt["run_id"],
                        },
                    )
                )
            output["reconciliation"] = await Reconciler(
                service.state, Adapter()
            ).reconcile()
            service.after_execution()
            output["plan"] = service.plans.describe(
                service.state.get_meta("rehearsal_objective")
            )
        else:
            output = service.status(project_id)
        print(json.dumps(output, ensure_ascii=False))
    finally:
        service.state.close()


if __name__ == "__main__":
    asyncio.run(main())
