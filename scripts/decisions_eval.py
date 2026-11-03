#!/usr/bin/env python3
"""Offline instrumented shadow exercise. No API, calibration or production claim."""

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
import time
from pathlib import Path

from oraculo.config import Config, DecisionsConfig, DecisionsTariff, ProviderConfig
from oraculo.decisions import CAUSES, Decisions, Snapshot
from oraculo.protocol import content_hash
from oraculo.routing import Router
from oraculo.state import State


class FixtureTransport:
    def __init__(self):
        self.calls = 0

    async def post(self, path, body, **kwargs):
        self.calls += 1
        features = json.loads(body["input"])
        label = next((key for key, value in features.items() if value), "unknown")
        return 200, {
            "model": "gpt-6-luna",
            "answers": [
                {
                    "type": "choice",
                    "name": "ci_cause",
                    "choice": label,
                    "confidence": 1,
                    "probabilities": [
                        {"value": c, "probability": int(c == label)} for c in CAUSES
                    ],
                }
            ],
            "usage": {
                "input_tokens": 32,
                "output_tokens": 0,
                "total_tokens": 32,
                "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
                "output_tokens_details": {"reasoning_tokens": 0},
            },
        }


async def evaluate(root):
    state = State(root / "state.sqlite3")
    try:
        config = Config(
            projects_root=root / "projects",
            vault_root=root / "vault",
            state_dir=root / "state",
            providers={
                "codex": ProviderConfig(
                    instance_id="codex", billing_verified=True, models=["fixture-model"]
                )
            },
        )
        catalog = {
            "providers": [{"providerInstanceId": "codex", "models": ["fixture-model"]}]
        }
        t = FixtureTransport()
        advisor = Decisions(
            state,
            DecisionsConfig(
                enabled=True,
                mode="shadow",
                daily_budget_microusd=1000000,
                run_budget_microusd=1000000,
                tariff=DecisionsTariff(
                    version="fixture-v1",
                    authorization_reference="ref:fixture-approval",
                    bound_reference="ref:fixture-bound",
                    input_nanousd=1000,
                    multiplier_milli=1000,
                    framing_tokens=1024,
                ),
            ),
            transport=t,
        )
        rows = []
        diagnostics = dict(
            code="assertion",
            environment="runner",
            external="network",
            context_limit="context overflow",
            concurrency="concurrency",
            account_quota="quota",
            unknown="",
        )
        for index, label in enumerate(CAUSES):
            router = Router(config, state)
            baseline = router.select("implementation", catalog)
            snap = Snapshot(
                project="fixture",
                repository="example/repo",
                head="a" * 40,
                source="a" * 40,
                task="fixture",
                attempt="fixture",
                generation=0,
                event=str(index),
                ci_run="fixture-run",
                ci_attempt=1,
                revision=index,
                state_hash=content_hash(state.capacity()),
                policy_hash=content_hash(config.model_dump(mode="json")),
                catalog_hash=content_hash(catalog),
            )
            before = content_hash(
                [
                    state.capacity(),
                    state.list_jobs(),
                    list(state.connection.execute("SELECT * FROM operations")),
                ]
            )
            started = time.perf_counter()
            answer = await advisor.advise(
                snap,
                diagnostics[label],
                lambda: snap.model_dump(mode="json"),
            )
            elapsed = (time.perf_counter() - started) * 1000
            await advisor.advise(
                snap,
                diagnostics[label],
                lambda: snap.model_dump(mode="json"),
            )
            after = content_hash(
                [
                    state.capacity(),
                    state.list_jobs(),
                    list(state.connection.execute("SELECT * FROM operations")),
                ]
            )
            effective = router.select("implementation", catalog)
            assert baseline == effective and before == after
            rows.append(
                dict(
                    label=label,
                    recommendation=answer["recommendation"],
                    unchanged=True,
                    latency_ms=elapsed,
                    fallback_marked=label == "unknown",
                    fallback_executed=False,
                )
            )
        return dict(
            version=1,
            fixture=True,
            api_calls=0,
            simulated_sends=t.calls,
            replay_sends=0,
            rows=rows,
            state_mutations=0,
            route_changes=0,
            production_shadow=False,
            calibration_certified=False,
            hybrid_fallback_executed=False,
        )
    finally:
        state.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="oraculo-shadow-") as temp:
        result = asyncio.run(evaluate(Path(temp)))
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
