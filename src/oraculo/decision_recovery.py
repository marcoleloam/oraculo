"""Conservative accounting acknowledgements, separate from immutable decisions.

No acknowledgement changes an entry, its result, charge, call count or replay key.
All writes and blocker derivation share the State SQLite writer transaction.
"""

from __future__ import annotations

import math
import re
from datetime import UTC, datetime

from pydantic import Field

from oraculo.config import DecisionsTariff, StrictModel
from oraculo.protocol import canonical_json, content_hash
from oraculo.state import DedupeConflict, StateError

PREFIX = "decisions:v1:"
ACK = PREFIX + "upper_ack:"
REF = r"^ref:[a-zA-Z0-9_.-]{1,100}$"
DIGEST = r"^[a-f0-9]{64}$"
REASONS = {
    "transport",
    "deadline",
    "crash_or_deadline",
    "cancelled",
    "snapshot_changed",
    "disabled",
    "configuration_changed",
    "clock_regressed",
    "expired",
    "http_401",
    "http_403",
    "http_429",
    "http_500",
    "http_502",
    "http_503",
    "http_504",
    "http_error",
    *(
        "credential_" + r
        for r in (
            "timeout",
            "access_denied_or_missing",
            "invalid_value",
            "backend_unavailable",
            "invalid_reference",
            "unsupported_platform",
            "cancelled",
        )
    ),
}


class UpperAcknowledgement(StrictModel):
    key: str = Field(pattern=DIGEST)
    expected_entry_digest: str = Field(pattern=DIGEST)
    idempotency_key: str = Field(pattern=REF)
    actor_reference: str = Field(pattern=REF)
    reason: str = Field(pattern=r"^conservatively_accounted_upper_bound$")
    evidence_reference: str = Field(pattern=REF)
    evidence_sha256: str = Field(pattern=DIGEST)


def cost(tokens, tariff):
    return (tokens * tariff.input_nanousd * tariff.multiplier_milli + 999999) // 1000000


def validate_entry(engine, entry):
    """Validate stored finite accounting, not the current mutable tariff/config."""
    from oraculo.decisions import TAXONOMY, Snapshot

    try:
        if not isinstance(entry, dict):
            raise ValueError()
        if entry["state"] not in {"intent", "sent", "unknown", "answered"}:
            raise ValueError()
        if entry.get("stage", "unknown") not in {
            "before_http",
            "possible_send",
            "response",
            "unknown",
        }:
            raise ValueError()
        if entry.get("reason") is not None and entry["reason"] not in REASONS | {
            "schema",
            "usage",
            "refusal",
            "persistence_uncertain",
            "advice_only",
            "shadow_only",
            "model_abstained",
        }:
            raise ValueError()
        for name in ("key", "request_hash", "config_hash"):
            if not isinstance(entry[name], str) or not re.fullmatch(
                DIGEST, entry[name]
            ):
                raise ValueError()
        Snapshot.model_validate(entry["snapshot"])
        if (
            content_hash(
                [
                    TAXONOMY,
                    entry["snapshot"],
                    entry["request_hash"],
                    entry["config_hash"],
                ]
            )
            != entry["key"]
        ):
            raise ValueError()
        tariff = DecisionsTariff.model_validate(entry["tariff"])
        # Require explicit finite output contract; Pydantic defaults do not count.
        if (
            entry["tariff"].get("output_nanousd") != 0
            or type(entry["tariff"].get("output_nanousd")) is not int
        ):
            raise ValueError()
        for field in ("token_bound", "reserved", "charge"):
            if type(entry[field]) is not int or entry[field] < 0:
                raise ValueError()
        if (
            not tariff.framing_tokens
            < entry["token_bound"]
            <= 16384 + tariff.framing_tokens
        ):
            raise ValueError()
        if (
            entry["reserved"] != cost(entry["token_bound"], tariff)
            or not 0 < entry["reserved"] <= 100000
        ):
            raise ValueError()
        if entry["charge"] > entry["reserved"] or (
            entry["state"] != "answered" and entry["charge"] != entry["reserved"]
        ):
            raise ValueError()
        for field in ("created_at", "deadline", "expires_at"):
            if type(entry[field]) not in {int, float} or not math.isfinite(
                entry[field]
            ):
                raise ValueError()
        if (
            not 0 < entry["deadline"] - entry["created_at"] <= 3.001
            or entry["expires_at"] < entry["deadline"]
        ):
            raise ValueError()
        day = datetime.fromtimestamp(entry["created_at"], UTC).date().isoformat()
        end = datetime.fromtimestamp(entry["deadline"], UTC).date().isoformat()
        if entry["day"] != day or entry.get("days", [day]) != sorted({day, end}):
            raise ValueError()
        logical = content_hash(
            [entry["snapshot"]["project"], entry["snapshot"]["task"]]
        )
        if entry.get("logical_task", logical) != logical or entry["run"] != logical:
            raise ValueError()
        if entry["state"] == "answered":
            usage = entry["result"]["usage"]
            output_tokens = usage["output_tokens"]
            # The stored tariff prices output at zero. A real completion still
            # reports output_tokens; that must not disagree with validate_response.
            if (
                type(usage["input_tokens"]) is not int
                or not 0 <= usage["input_tokens"] <= entry["token_bound"]
                or type(output_tokens) is not int
                or output_tokens < 0
            ):
                raise ValueError()
            details = usage.get("output_tokens_details")
            if details is not None and (
                not isinstance(details, dict)
                or type(details.get("reasoning_tokens")) is not int
                or not 0 <= details["reasoning_tokens"] <= output_tokens
            ):
                raise ValueError()
            if "total_tokens" in usage and (
                type(usage["total_tokens"]) is not int
                or usage["total_tokens"] != usage["input_tokens"] + output_tokens
            ):
                raise ValueError()
            if entry["charge"] != cost(usage["input_tokens"], tariff):
                raise ValueError()
        elif entry.get("result") and (
            not isinstance(entry["result"], dict)
            or "usage" in entry["result"]
            or entry["result"].get("state") != "unknown"
        ):
            raise ValueError()
        return tariff
    except (KeyError, TypeError, ValueError, OverflowError):
        raise StateError("decision_accounting_corrupt") from None


def eligible(engine, entry):
    validate_entry(engine, entry)
    if entry["state"] != "unknown" or entry.get("reason") not in REASONS:
        return False
    if entry.get("result") and (
        entry["result"].get("state") != "unknown"
        or entry["result"].get("reason") != entry["reason"]
    ):
        return False
    # Integrity and byte/framing bounds of the original sanitized request.
    reference = entry.get("request_artifact", {})
    if (
        not engine.artifact_valid(reference)
        or reference.get("sha256") != entry["request_hash"]
    ):
        return False
    try:
        import hashlib
        import json
        from pathlib import Path

        path = Path(reference["reference"])
        expected = (
            engine.state.path.parent / "decisions" / entry["key"] / "request.json"
        )
        if path != expected or path.is_symlink():
            return False
        with path.open("rb") as stream:
            raw = stream.read(16385)
        if len(raw) > 16384 or hashlib.sha256(raw).hexdigest() != entry["request_hash"]:
            return False
        request = json.loads(raw)
        return (
            content_hash(request) == entry["request_hash"]
            and len(canonical_json(request).encode())
            + entry["tariff"]["framing_tokens"]
            == entry["token_bound"]
        )
    except (OSError, KeyError, TypeError, ValueError):
        return False


def ack_valid(engine, entry, record):
    try:
        submission = UpperAcknowledgement.model_validate(
            record["submission"]
        ).model_dump()
        return (
            record["state"] == "accounted_upper_bound"
            and submission["key"] == entry["key"]
            and submission["expected_entry_digest"] == content_hash(entry)
            and record["submission_digest"] == content_hash(submission)
            and record["retained_charge_microusd"] == entry["reserved"]
            and record["origin"] == "local_operator"
            and type(record["acknowledged_at"]) in {int, float}
            and math.isfinite(record["acknowledged_at"])
            and engine.state.connection.execute(
                "SELECT 1 FROM audit_events WHERE kind=? AND payload=? LIMIT 1",
                ("decisions_unknown_upper_acknowledged", canonical_json(record)),
            ).fetchone()
            is not None
            and eligible(engine, entry)
        )
    except (KeyError, TypeError, ValueError):
        return False


def validate_book(book, entries):
    if book == {} and not entries:
        return
    if (
        not isinstance(book, dict)
        or type(book.get("unknown_cost")) is not bool
        or not isinstance(book.get("failures"), list)
        or len(book["failures"]) > 20
        or any(type(v) is not bool for v in book["failures"])
        or type(book.get("consecutive")) is not int
        or book["consecutive"] < 0
        or type(book.get("open_until")) not in {int, float}
        or not math.isfinite(book["open_until"])
        or book["open_until"] < 0
    ):
        raise StateError("decision_accounting_corrupt")


def unresolved(engine, entries, book):
    validate_book(book, entries)
    keys = []
    for entry in entries:
        validate_entry(engine, entry)
        if entry["state"] in {"intent", "sent"} and entry["deadline"] <= engine.clock():
            keys.append(entry["key"])
        if entry["state"] == "unknown":
            record = engine.state.get_meta(ACK + entry["key"])
            if not record or not ack_valid(engine, entry, record):
                keys.append(entry["key"])
    # A flag with no matching unknown is uncertain storage, not permission to
    # clear an arbitrary flag. Failed persistence never becomes acknowledgeable.
    if book.get("unknown_cost") and not any(e["state"] == "unknown" for e in entries):
        raise StateError("decision_accounting_corrupt")
    return keys


def financial_status(engine, project):
    """Read-only: no credential, HTTP, expiry mutation or schema promotion."""
    try:
        entries = engine.entries()
        book = engine.state.get_meta(PREFIX + "book", {})
        pending = unresolved(engine, entries, book)
        now = engine.clock()
        day = datetime.fromtimestamp(now, UTC).date().isoformat()
        selected = [e for e in entries if e["snapshot"]["project"] == project]
        rows = []
        for e in selected:
            rows.append(
                dict(
                    key=e["key"],
                    entry_digest=content_hash(e),
                    state=e["state"],
                    reason=e.get("reason"),
                    stage=e.get("stage", "unknown"),
                    reserved_microusd=e["reserved"],
                    charge_microusd=e["charge"],
                    logical_task=e.get("logical_task", e["run"]),
                    day=e["day"],
                    acknowledged=e["state"] == "unknown" and e["key"] not in pending,
                    acknowledge_eligible=eligible(engine, e),
                )
            )
        return dict(
            state="blocked" if pending else "ready",
            reason="unknown_cost" if pending else "accounting_ready",
            unknown_cost=bool(pending),
            unresolved_count=len(pending),
            calls_total=len(entries),
            charge_total_microusd=sum(e["charge"] for e in entries),
            utc_day=day,
            daily_charge_microusd=sum(
                e["charge"] for e in entries if day in e.get("days", [e["day"]])
            ),
            entries=rows,
        )
    except (ValueError, KeyError, TypeError):
        return dict(
            state="blocked", reason="decision_accounting_corrupt", unknown_cost=True
        )


def acknowledge(engine, project, payload):
    try:
        submission = UpperAcknowledgement.model_validate(payload).model_dump()
    except ValueError:
        raise StateError("decision_acknowledgement_invalid") from None
    key = submission["key"]
    with engine.state.transaction():
        entry = engine.state.get_meta(PREFIX + "entry:" + key)
        if not entry or entry.get("snapshot", {}).get("project") != project:
            raise StateError("decision_entry_project_mismatch")
        validate_entry(engine, entry)
        if content_hash(entry) != submission["expected_entry_digest"]:
            raise StateError("decision_entry_digest_stale")
        book = engine.state.get_meta(PREFIX + "book", {})
        entries = engine.entries()
        unresolved(engine, entries, book)  # corruption in ANY entry blocks recovery
        previous = engine.state.get_meta(ACK + key)
        if previous:
            if previous.get("submission") != submission or not ack_valid(
                engine, entry, previous
            ):
                raise DedupeConflict("decision_acknowledgement_conflict")
            return dict(previous, replay=True)
        if not eligible(engine, entry):
            raise StateError("decision_unknown_not_acknowledgeable")
        identity = PREFIX + "ack_id:" + submission["idempotency_key"]
        if engine.state.get_meta(identity) is not None:
            raise DedupeConflict("decision_acknowledgement_identity_reused")
        # Only this operation promotes main state. Legacy v4/v7 and managed
        # ledger v5 are untouched by status/preparation/ordinary decisions.
        engine.state.require_decision_recovery_schema()
        record = dict(
            state="accounted_upper_bound",
            submission=submission,
            submission_digest=content_hash(submission),
            origin="local_operator",
            retained_charge_microusd=entry["reserved"],
            acknowledged_at=engine.clock(),
        )
        if not engine.state.compare_meta(ACK + key, None, record):
            raise StateError("decision_acknowledgement_cas_lost")
        engine.state.set_meta(identity, key)
        engine.state.audit("decisions_unknown_upper_acknowledged", record)
        # Derived from every unknown across projects, never from one operator
        # supplied boolean. Original entries/results/events remain byte-identical.
        book["unknown_cost"] = bool(unresolved(engine, entries, book))
        engine.state.set_meta(PREFIX + "book", book)
    return dict(record, replay=False)
