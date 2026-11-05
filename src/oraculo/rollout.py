"""Service-owned destination verification. Never confirms a caller's assertion."""

from __future__ import annotations

import asyncio
import hashlib
import time
from typing import Literal

from pydantic import Field

from oraculo.config import StrictModel
from oraculo.protocol import content_hash
from oraculo.runtime import RuntimeError as RuntimeOperationError
from oraculo.state import StateError


def verifier_binding(target, config_path):
    if target is None:
        return None
    try:
        return content_hash(
            [
                target.model_dump(mode="json"),
                hashlib.sha256(config_path.read_bytes()).hexdigest(),
            ]
        )
    except OSError:
        return None


class RolloutIdentity(StrictModel):
    version: Literal[1] = 1
    source_commit: str = Field(pattern=r"^[a-f0-9]{40}$")
    merge_sha: str = Field(pattern=r"^[a-f0-9]{40}$")
    release_id: str = Field(pattern=r"^[a-zA-Z0-9_.-]{1,100}$")
    artifact_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    environment: str = Field(min_length=1, max_length=200)


class RolloutVerifier:
    def __init__(
        self, probe, *, monotonic=time.monotonic, clock=time.time, sleep=asyncio.sleep
    ):
        self.probe, self.monotonic, self.clock, self.sleep = (
            probe,
            monotonic,
            clock,
            sleep,
        )

    async def observe(self, identity: RolloutIdentity):
        expected = identity.model_dump(mode="json")
        start = self.monotonic()
        samples = []
        try:
            async with asyncio.timeout(120):
                for index in range(2):
                    if index:
                        await self.sleep(5)
                    observed = await self.probe()
                    elapsed = self.monotonic() - start
                    if elapsed >= 120 or (
                        index and elapsed - samples[0]["elapsed"] < 5
                    ):
                        raise StateError("rollout_deadline_or_probe_spacing")
                    if (
                        not isinstance(observed, dict)
                        or observed.get("healthy") is not True
                        or any(observed.get(k) != v for k, v in expected.items())
                    ):
                        raise StateError("rollout_destination_mismatch")
                    samples.append(
                        dict(
                            identity=expected,
                            healthy=True,
                            elapsed=elapsed,
                            observed_at=self.clock(),
                            process={
                                k: observed[k]
                                for k in (
                                    "process_pid",
                                    "config_sha256",
                                    "liveness",
                                    "readiness",
                                    "diagnostic_health",
                                )
                                if k in observed
                            },
                        )
                    )
                    if index and any(
                        samples[0]["process"].get(k) != samples[1]["process"].get(k)
                        for k in ("process_pid", "config_sha256")
                    ):
                        raise StateError("rollout_process_changed")
            return dict(
                state="confirmed",
                verifier="service-rollout-v1",
                identity=expected,
                samples=samples,
                observed_at=self.clock(),
                receipt_hash=content_hash(samples),
                expires_at=self.clock() + 120,
            )
        except (OSError, ValueError, TimeoutError, RuntimeOperationError):
            return dict(
                state="unknown",
                verifier="service-rollout-v1",
                identity=expected,
                samples=samples,
                reason="destination_unverified",
                observed_at=self.clock(),
            )
