"""Versioned outcome verification; caller receipts never verify themselves."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import time
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from oraculo.config import StrictModel
from oraculo.protocol import content_hash
from oraculo.state import StateError


class OutcomeContract(StrictModel):
    version: Literal[2] = 2
    kind: Literal["command_artifact", "semantic"] = "command_artifact"
    commit: str = Field(pattern=r"^[a-f0-9]{40}$")
    argv: list[str] = Field(min_length=1, max_length=100)
    artifact: str = Field(min_length=1, max_length=500)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    verifier: Literal["service-command-artifact-v2"] = "service-command-artifact-v2"
    review_task: str | None = None

    @model_validator(mode="after")
    def valid(self):
        path = Path(self.artifact)
        if (
            path.is_absolute()
            or ".." in path.parts
            or str(path) != self.artifact
            or self.artifact == "."
            or "\\" in self.artifact
        ):
            raise ValueError("outcome artifact must be a canonical relative path")
        if any(not a or "\0" in a for a in self.argv):
            raise ValueError("invalid outcome command")
        if self.kind == "semantic" and not self.review_task:
            raise ValueError("semantic criterion requires an independent review task")
        return self


def artifact_digest(root: Path, name: str):
    path = root / name
    if not path.resolve().is_relative_to(root.resolve()) or not path.is_file():
        raise StateError("outcome_artifact_missing_or_outside_checkout")
    # Stream and bound evidence; never load arbitrary files into status/logs.
    if path.stat().st_size > 16 * 1024 * 1024:
        raise StateError("outcome_artifact_limit")
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def clean_source(root: Path, artifact: str):
    # The artifact may be an untracked command output. Tracked modifications,
    # staged files, submodule drift and other untracked source are forbidden.
    raw = subprocess.run(
        ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        cwd=root,
        capture_output=True,
        check=True,
        timeout=10,
    ).stdout
    for item in raw.split(b"\0"):
        if not item:
            continue
        if item[:3] == b"?? " and item[3:].decode("utf-8") == artifact:
            continue
        raise StateError("outcome_source_dirty")

    # Git status can hide assume-unchanged/skip-worktree files. Hash the tracked
    # index entries as well, so a dirty file at the same HEAD never supplies proof.
    index = subprocess.run(
        ["git", "ls-files", "--stage", "-z"],
        cwd=root,
        capture_output=True,
        check=True,
        timeout=10,
    ).stdout
    for entry in index.split(b"\0"):
        if not entry:
            continue
        metadata, name = entry.split(b"\t", 1)
        mode, expected, stage = metadata.split()
        path = root / os.fsdecode(name)
        if stage != b"0" or not path.parent.resolve().is_relative_to(root.resolve()):
            raise StateError("outcome_source_dirty")
        if mode == b"120000" and path.is_symlink():
            data = os.fsencode(os.readlink(path))
            actual = hashlib.sha1(
                b"blob " + str(len(data)).encode() + b"\0" + data
            ).hexdigest()
        elif mode in {b"100644", b"100755"} and stat.S_ISREG(path.lstat().st_mode):
            with path.open("rb") as stream:
                h = hashlib.sha1(
                    b"blob " + str(os.fstat(stream.fileno()).st_size).encode() + b"\0"
                )
                while chunk := stream.read(65536):
                    h.update(chunk)
                actual = h.hexdigest()
        else:
            raise StateError("outcome_source_type_unverifiable")
        if actual.encode() != expected:
            raise StateError("outcome_source_dirty")


class Outcomes:
    def __init__(self, service):
        self.service = service if hasattr(service, "state") else None
        self.state = service.state if self.service else service

    @staticmethod
    def key(attempt_id, criterion_id):
        return "outcome:v2:" + content_hash([attempt_id, criterion_id])

    def contract(self, attempt, criterion_id):
        job = self.state.get_job(attempt["job_id"])
        spec = json.loads(job["payload"]).get("task_spec", {})
        matched = [c for c in spec.get("criteria", []) if c["id"] == criterion_id]
        if len(matched) != 1 or not matched[0].get("outcome"):
            raise StateError("versioned_outcome_contract_required")
        return job, OutcomeContract.model_validate(matched[0]["outcome"])

    def independent_review(self, attempt, contract):
        job = self.state.get_job(attempt["job_id"])
        row = self.state._one(
            "SELECT job_ids FROM objective_plans WHERE objective_id=?",
            (job["objective_id"],),
        )
        jobs = json.loads(row["job_ids"]) if row else {}
        if contract.review_task not in jobs:
            return None
        reviewer = self.state.get_job(jobs[contract.review_task])
        spec = json.loads(reviewer["payload"])["task_spec"]
        if (
            spec["stage"] != "review"
            or spec["write_paths"] != []
            or reviewer["id"] == job["id"]
            or reviewer["state"] != "succeeded"
        ):
            return None
        review = self.state._one(
            "SELECT * FROM attempts WHERE job_id=? ORDER BY number DESC LIMIT 1",
            (reviewer["id"],),
        )
        if (
            not review
            or review["status"] != "succeeded"
            or not review["run_id"]
            or review["thread_id"] == attempt["thread_id"]
            or review["run_id"] == attempt["run_id"]
        ):
            return None
        report = self.state.get_meta(f"execution_report:{review['id']}", {})
        verdict = report.get("evidence", {}).get("review", {})
        if (
            verdict.get("commit") != contract.commit
            or verdict.get("verdict") != "accepted"
            or not isinstance(verdict.get("reason"), str)
            or not verdict["reason"].strip()
        ):
            return None
        # A probabilistic verdict alone cannot pass: all review criteria must
        # themselves have service-produced deterministic evidence at this SHA.
        for c in spec["criteria"]:
            if (
                not c.get("outcome")
                or c["outcome"]["kind"] != "command_artifact"
                or c["outcome"]["commit"] != contract.commit
                or not self.satisfied(review, c["id"])
            ):
                return None
        return {
            "attempt": review["id"],
            "run": review["run_id"],
            "thread": review["thread_id"],
            "commit": contract.commit,
            "verdict_hash": content_hash(verdict),
        }

    async def verify(self, attempt, criterion_id, cwd: Path, request_id: str):
        job, contract = self.contract(attempt, criterion_id)
        if self.state.connection.in_transaction:
            raise StateError("outcome_io_inside_transaction")
        self.state.validate_grant(attempt["id"], attempt["generation"])
        request_key = "outcome:v2:request:" + content_hash([attempt["id"], request_id])
        request_hash = content_hash([attempt["id"], criterion_id, str(cwd)])
        with self.state.transaction():
            intent = self.state.get_meta(request_key)
            if intent:
                if intent["hash"] != request_hash:
                    raise StateError("outcome_request_reused")
                if intent.get("result"):
                    replay = dict(intent["result"], replay=True)
                    if replay["status"] == "passed" and not self.satisfied(
                        attempt, criterion_id
                    ):
                        replay.update(status="pending", reason="evidence_invalidated")
                    return replay
                return dict(
                    status="pending", reason="outcome_intent_uncertain", version=2
                )
            before = self.state.get_meta(self.key(attempt["id"], criterion_id))
            if before and before.get("verification_state") == "intent":
                return dict(
                    status="pending", reason="outcome_intent_uncertain", version=2
                )
            if (
                before
                and before.get("status") == "passed"
                and self.satisfied(attempt, criterion_id)
            ):
                return dict(before, replay=True)
            self.state.require_orchestration_schema()
            self.state.set_meta(request_key, {"hash": request_hash, "state": "intent"})
            before = {
                "verification_state": "intent",
                "request_id": request_id,
                "status": "pending",
            }
            self.state.set_meta(self.key(attempt["id"], criterion_id), before)
        identity = content_hash([contract.model_dump(mode="json"), attempt])
        receipt = dict(
            version=2,
            criterion=criterion_id,
            attempt=attempt["id"],
            generation=attempt["generation"],
            commit=contract.commit,
            contract_hash=content_hash(contract.model_dump(mode="json")),
            verifier=contract.verifier,
            status="pending",
            observed_at=time.time(),
        )
        try:
            clean_source(cwd, contract.artifact)
            head = (
                await self.service.run_command(["git", "rev-parse", "HEAD"], cwd)
            ).strip()
            if head != contract.commit:
                raise StateError("outcome_commit_mismatch")
            command = await self.service.run_command(contract.argv, cwd, capture=True)
            receipt["command"] = command
            if command["exit"] != 0:
                raise StateError("outcome_command_failed")
            digest = artifact_digest(cwd, contract.artifact)
            if digest != contract.sha256:
                raise StateError("outcome_artifact_changed")
            after = (
                await self.service.run_command(["git", "rev-parse", "HEAD"], cwd)
            ).strip()
            if after != head:
                raise StateError("outcome_commit_changed")
            clean_source(cwd, contract.artifact)
            self.state.validate_grant(attempt["id"], attempt["generation"])
            if identity != content_hash(
                [
                    self.contract(attempt, criterion_id)[1].model_dump(mode="json"),
                    self.state.get_attempt(attempt["id"]),
                ]
            ):
                raise StateError("outcome_snapshot_changed")
            receipt.update(
                artifact=contract.artifact, artifact_sha256=digest, cwd=str(cwd)
            )
            if contract.kind == "semantic":
                review = self.independent_review(attempt, contract)
                if review is None:
                    raise StateError("outcome_independent_review_pending")
                receipt["review"] = review
            receipt["status"] = "passed"
        except (OSError, ValueError, TimeoutError, subprocess.SubprocessError):
            # No command output, stderr or exception text is accepted as proof.
            receipt["reason"] = "verification_inconclusive"
        with self.state.transaction():
            if not self.state.compare_meta(
                self.key(attempt["id"], criterion_id), before, receipt
            ):
                raise StateError("outcome_verification_raced")
            self.state.set_meta(
                request_key,
                {"hash": request_hash, "state": "answered", "result": receipt},
            )
            self.state.audit(
                "outcome_verification",
                {
                    "attempt": attempt["id"],
                    "criterion": criterion_id,
                    "status": receipt["status"],
                    "receipt_hash": content_hash(receipt),
                },
            )
        return receipt

    def satisfied(self, attempt, criterion_id, reference=None):
        _, contract = self.contract(attempt, criterion_id)
        r = self.state.get_meta(self.key(attempt["id"], criterion_id))
        if (
            not r
            or r["status"] != "passed"
            or r["commit"] != contract.commit
            or r["contract_hash"] != content_hash(contract.model_dump(mode="json"))
            or (reference is not None and reference != content_hash(r))
        ):
            return False
        try:
            clean_source(Path(r["cwd"]), contract.artifact)
            if artifact_digest(Path(r["cwd"]), contract.artifact) != contract.sha256:
                return False
            # Re-check HEAD at report consumption; a stale receipt cannot pass.
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=r["cwd"],
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            ).stdout.strip()
            if head != contract.commit:
                return False
            if contract.kind == "semantic" and r.get(
                "review"
            ) != self.independent_review(attempt, contract):
                return False
            return True
        except (OSError, ValueError, subprocess.SubprocessError):
            return False
