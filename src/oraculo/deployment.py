"""Deployment assertions are separate from Git, PRs and task execution."""

import re
from datetime import datetime

from oraculo.state import StateError


def deployment_evidence(payload, merge):
    if (
        not isinstance(merge, dict)
        or merge.get("state") != "merged"
        or not merge.get("merge_sha")
    ):
        raise StateError("deployment_requires_observed_merge")
    if not isinstance(payload, dict) or set(payload) != {
        "release",
        "environment",
        "occurred_at",
        "state",
        "proof",
    }:
        raise StateError("deployment_evidence_invalid")
    if payload["state"] != "reported":
        # No rollout verifier is configured by the current runtime. A caller's
        # claim or attachment cannot grant itself confirmation.
        raise StateError("deployment_confirmation_unavailable")
    if (
        payload["release"] != merge["merge_sha"]
        or not isinstance(payload["environment"], str)
        or not payload["environment"].strip()
        or len(payload["environment"]) > 200
        or not isinstance(payload["occurred_at"], str)
        or not isinstance(payload["proof"], list)
        or not payload["proof"]
        or len(payload["proof"]) > 20
    ):
        raise StateError("deployment_evidence_invalid")
    try:
        timestamp = datetime.fromisoformat(
            payload["occurred_at"].replace("Z", "+00:00")
        )
        if timestamp.utcoffset() is None:
            raise ValueError
    except ValueError:
        raise StateError("deployment_timestamp_invalid") from None
    for proof in payload["proof"]:
        if (
            not isinstance(proof, dict)
            or set(proof) != {"source", "reference", "sha256"}
            or any(
                not isinstance(proof.get(k), str)
                or not proof[k].strip()
                or len(proof[k]) > 2048
                for k in proof
            )
            or not re.fullmatch(r"[a-f0-9]{64}", proof["sha256"])
        ):
            raise StateError("deployment_proof_invalid")
    return {**payload, "merge_url": merge["url"], "verification": "not_observed"}
