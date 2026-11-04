"""Strict historical control artifacts, distinct from current host corroboration.

A stdout request frame is not a host response or a terminal receipt. No message
text, task title or provider refusal is interpreted as a semantic verdict.
"""

import json
import math
import time

from oraculo.host_ack import ACK_PROTOCOL, unwrap
from oraculo.protocol import canonical_json, content_hash
from oraculo.state import StateError

FRAME_FIELDS = {
    "marker",
    "request_id",
    "ack_protocol",
    "tool",
    "arguments",
    "issued_at",
}
RECEIPT_FIELDS = {
    "taskId",
    "childNodeId",
    "childThreadId",
    "childRunId",
    "status",
    "summary",
    "workState",
    "hasPendingChildRuns",
    "hasPendingQuestion",
    "providerInstanceId",
    "model",
    "resultContextTransferId",
    "latestTerminalRunId",
    "latestTerminalStatus",
    "latestTerminalSummary",
    "latestTerminalResultContextTransferId",
    "waitTimedOut",
}


def strict_json(raw):
    def pairs(rows):
        result = {}
        for key, value in rows:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def constant(_):
        raise ValueError("nonfinite JSON")

    return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)


def native_receipt(raw, binding, status):
    observation = unwrap(strict_json(raw))
    if (
        set(observation) - RECEIPT_FIELDS
        or any(
            observation.get(k) != binding[k]
            for k in ("taskId", "childThreadId", "childRunId")
        )
        or observation.get("status") != status
        or observation.get("workState") != "result_available"
        or observation.get("hasPendingChildRuns") is not False
        or observation.get("hasPendingQuestion") is not False
        or observation.get("latestTerminalRunId", binding["childRunId"])
        not in {None, binding["childRunId"]}
        or observation.get("latestTerminalStatus", status) not in {None, status}
    ):
        raise StateError("legacy_delivery_original_host_receipt_required")
    return dict(
        kind="native_task_status_receipt",
        historical_terminal_receipt=True,
        observation=observation,
    )


def control_stream(raw, child, allowed_status_fields):
    """Known stdout grammar: five read frames, control sends, final status JSON.

    No ACK/result is emitted on stdout by the official bridge. Parse the entire
    stream; accept neither arbitrary prefixes/suffixes nor extracted JSON.
    """
    try:
        lines = raw.decode("utf-8").splitlines()
        frames, offset = [], 0
        for line in lines:
            if line == "{":
                break
            frame = strict_json(line)
            if (
                not isinstance(frame, dict)
                or set(frame) != FRAME_FIELDS
                or frame["marker"] != "ORACULO_HOST_CALL_V1"
                or frame["ack_protocol"] != ACK_PROTOCOL
                or canonical_json(frame) != line
                or not isinstance(frame["request_id"], str)
                or len(frame["request_id"]) != 32
                or any(c not in "0123456789abcdef" for c in frame["request_id"])
                or type(frame["issued_at"]) not in (float, int)
                or not math.isfinite(frame["issued_at"])
                or not 0 < frame["issued_at"] <= time.time()
            ):
                raise ValueError
            frames.append(frame)
            offset += 1
            if offset > 133:
                raise ValueError
        if (
            len(frames) < 5
            or offset == len(lines)
            or len({f["request_id"] for f in frames}) != len(frames)
            or [f["issued_at"] for f in frames]
            != sorted(f["issued_at"] for f in frames)
        ):
            raise ValueError
        snapshot = strict_json("\n".join(lines[offset:]))
        if (
            not isinstance(snapshot, dict)
            or set(snapshot) - allowed_status_fields
            or snapshot.get("source") != "service"
            or type(snapshot.get("protocol_version")) is not int
            or snapshot["protocol_version"] != 1
            or snapshot.get("id") != child["id"]
            or snapshot.get("transport_mode") != "native_child"
            or snapshot.get("origin") != child["origin"]
            or snapshot.get("parent") != child["parent"]
            or snapshot.get("project") != child["project"]
            or snapshot.get("child") != child["child"]
            or snapshot.get("base_sha") != child["base_sha"]
            or any(
                snapshot.get(k) != child.get(k)
                for k in (
                    "child_workspace",
                    "parent_workspace",
                    "source_commit",
                    "source_dirty",
                    "unselected_paths",
                    "expected_branch",
                    "launch_attempted",
                )
            )
            or snapshot.get("phase") != "validating"
            or snapshot.get("work_status") != "pending"
            or snapshot.get("history_truncated") is not False
            or not isinstance(snapshot.get("events"), list)
            or type(snapshot.get("snapshot_revision")) is not int
            or not 0 < snapshot["snapshot_revision"] <= len(child["events"])
            or snapshot["snapshot_revision"] != len(snapshot["events"])
            or snapshot.get("event_count") != len(snapshot["events"])
            or snapshot["events"] != child["events"][: len(snapshot["events"])]
            or snapshot.get("observed")
            != dict(
                status="failed", workState="result_available", hasPendingChildRuns=False
            )
            or type(snapshot.get("observed_at")) not in (float, int)
            or not math.isfinite(snapshot["observed_at"])
            or not frames[4]["issued_at"] <= snapshot["observed_at"] <= time.time()
            or not isinstance(snapshot.get("memory"), dict)
            or any(
                snapshot["memory"].get(k) != child["memory"].get(k)
                for k in ("id", "identity", "project_id")
            )
            or snapshot["memory"].get("work_status") != "running"
            or snapshot["memory"].get("write_state") != "not_requested"
            or snapshot["memory"].get("write_receipt") is not None
        ):
            raise ValueError
        caller, subject = child["origin"]["thread_id"], child["child"]["childThreadId"]

        def read(thread):
            return dict(
                threadId=thread, view="activity", limit=1, runLimit=1, maxCharsPerItem=1
            )

        expected = [
            ("orchestrator_capabilities", {}),
            ("t3_thread_read", read(caller)),
            ("t3_project_list", dict(limit=100)),
            ("t3_thread_read", read(subject)),
            ("task_status", dict(taskId=child["child"]["taskId"])),
        ]
        if [(f["tool"], f["arguments"]) for f in frames[:5]] != expected:
            raise ValueError
        events = {e["id"]: e for e in snapshot["events"]}
        for frame in frames[5:]:
            args = frame["arguments"]
            if (
                frame["tool"] != "t3_thread_send"
                or not isinstance(args, dict)
                or set(args) != {"threadId", "message", "mode", "clientRequestId"}
                or args["threadId"] != caller
                or args["mode"] != "auto"
            ):
                raise ValueError
            # This legacy format emitted pure JSON control envelopes. Readable
            # HTML or a hidden JSON substring is not accepted by this grammar.
            message = strict_json(args["message"])
            if (
                not isinstance(message, dict)
                or message.get("marker") != "ORACULO_CONTROL_V1"
                or message.get("task_id") != child["id"]
            ):
                raise ValueError
            event = events.get(message.get("event_id"))
            if (
                event is None
                or args["clientRequestId"] != child["id"] + ":" + event["id"]
                or any(
                    message.get(k) != event[v]
                    for k, v in {
                        "sequence": "sequence",
                        "phase": "phase",
                        "proof": "proof",
                        "occurred_at": "at",
                    }.items()
                )
            ):
                raise ValueError
        return dict(
            kind="historical_managed_stdio_stream_v1",
            historical_terminal_receipt=False,
            interpretation="control_requests_and_managed_observation; not host_response_or_terminal_receipt",
            frame_count=len(frames),
            frame_sha256=[content_hash(f) for f in frames],
            managed_snapshot_sha256=content_hash(snapshot),
            historical_snapshot_revision=snapshot["snapshot_revision"],
            canonical_event_prefix_sha256=content_hash(snapshot["events"]),
            historical_launch_status=(
                snapshot["child"].get("launch_receipt") or {}
            ).get("status"),
            historical_managed_observation=snapshot["observed"],
        )
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise StateError("legacy_delivery_historical_transport_unproven") from None


def host_artifact(raw, child, allowed_status_fields, status):
    # Only a canonical first request frame selects the stream grammar. Native
    # receipts consume the entire JSON document. Neither path searches text.
    try:
        try:
            first = strict_json(raw.splitlines()[0])
        except (ValueError, UnicodeError):
            first = None
        if isinstance(first, dict) and first.get("marker") == "ORACULO_HOST_CALL_V1":
            return control_stream(raw, child, allowed_status_fields)
        return native_receipt(raw, child["child"], status)
    except StateError:
        raise
    except (ValueError, TypeError, UnicodeError, IndexError):
        raise StateError("legacy_delivery_original_host_receipt_required") from None
