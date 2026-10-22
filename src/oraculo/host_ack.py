"""Literal host-result relay with lossless compression and bounded consistency.

This is a local operator trust boundary, NOT an attestation scheme. A malicious
operator can fabricate a self-consistent result. Prefer the caller-bound ACP
transport, which carries tool output without a model transcribing it.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
import zlib

from oraculo.protocol import canonical_json, content_hash
from oraculo.run_continuity import timestamp
from oraculo.state import StateError

ACK_PROTOCOL = "ORACULO_HOST_ACK_V2"
MAX_RESULT_BYTES = 16 * 1024 * 1024


def unwrap(result):
    if not isinstance(result, dict):
        raise StateError("native_bridge_result_invalid")
    if result.get("isError"):
        raise StateError("host_response_uncertain")
    structured = result.get("structuredContent")
    text = None
    if "content" in result:
        try:
            text = json.loads(
                "\n".join(
                    c["text"] for c in result["content"] if c.get("type") == "text"
                )
            )
        except (ValueError, TypeError, KeyError):
            if structured is None:
                raise StateError("native_bridge_result_invalid") from None
    if structured is not None and text is not None and structured != text:
        raise StateError("native_bridge_result_incoherent")
    value = (
        structured if structured is not None else text if text is not None else result
    )
    if not isinstance(value, dict):
        raise StateError("native_bridge_result_invalid")
    return value


def required(value, fields):
    if not isinstance(value, dict) or any(k not in value for k in fields):
        raise StateError("native_bridge_result_incomplete")


def integer(value):
    if type(value) is not int or value < 0:
        raise StateError("native_bridge_result_incoherent")


class HostResults:
    """Minimum consumed fields, plus invariants across this invocation's frames."""

    def __init__(self, *, history=True):
        self.history = history
        self.threads = {}
        self.tasks = {}
        self.caller = None
        self.catalogs = {}

    def validate(self, tool, args, result):
        try:
            return self._validate(tool, args, result)
        except (KeyError, TypeError, AttributeError, IndexError):
            raise StateError("native_bridge_result_incomplete") from None

    def _validate(self, tool, args, result):
        value = unwrap(result)
        if tool == "orchestrator_capabilities":
            required(
                value,
                (
                    "parentThreadId",
                    "inheritedProviderInstanceId",
                    "inheritedModel",
                    "runtimeMode",
                    "interactionMode",
                    "providers",
                    "features",
                ),
            )
            if not isinstance(value["providers"], list) or not isinstance(
                value["features"], dict
            ):
                raise StateError("native_bridge_result_incomplete")
            required(value["features"], ("appOwnedSubagents",))
            if type(value["features"]["appOwnedSubagents"]) is not bool:
                raise StateError("native_bridge_result_incomplete")
            for key in (
                "parentThreadId",
                "inheritedProviderInstanceId",
                "inheritedModel",
            ):
                if value[key] is not None and not isinstance(value[key], str):
                    raise StateError("native_bridge_result_incomplete")
            for provider in value["providers"]:
                required(
                    provider,
                    (
                        "providerInstanceId",
                        "driverKind",
                        "models",
                        "canRunChildTask",
                        "canRunCrossProviderChildTask",
                        "constraints",
                    ),
                )
                if not isinstance(provider["models"], list):
                    raise StateError("native_bridge_result_incomplete")
                for key in ("canRunChildTask", "canRunCrossProviderChildTask"):
                    if type(provider[key]) is not bool:
                        raise StateError("native_bridge_result_incomplete")
                if not isinstance(provider["constraints"], list):
                    raise StateError("native_bridge_result_incomplete")
                for model in provider["models"]:
                    required(model, ("id", "options"))
                    if not isinstance(model["options"], list):
                        raise StateError("native_bridge_model_options_unproven")
                    for option in model["options"]:
                        required(option, ("id", "type"))
                        if option["type"] == "select":
                            required(option, ("options",))
                            if (
                                not isinstance(option["options"], list)
                                or not option["options"]
                            ):
                                raise StateError("native_bridge_model_options_unproven")
                            for choice in option["options"]:
                                required(choice, ("id",))
                            choices = [c["id"] for c in option["options"]]
                            if len(set(choices)) != len(choices) or (
                                option.get("currentValue") is not None
                                and option["currentValue"] not in choices
                            ):
                                raise StateError(
                                    "native_bridge_model_options_incoherent"
                                )
                        elif option["type"] != "boolean":
                            raise StateError("native_bridge_model_options_unproven")
            if self.caller is not None and self.caller != value["parentThreadId"]:
                raise StateError("native_bridge_caller_changed")
            self.caller = value["parentThreadId"]
        elif tool == "t3_thread_read":
            required(
                value, ("thread", "recentRuns", "items", "hasMore", "nextPosition")
            )
            thread = value["thread"]
            required(
                thread,
                (
                    "threadId",
                    "projectId",
                    "parentThreadId",
                    "activeRunId",
                    "latestRunId",
                    "status",
                    "pendingRequestCount",
                    "runCount",
                    "itemCount",
                    "updatedAt",
                    "worktreePath",
                    "branch",
                    "archived",
                    "settled",
                ),
            )
            if thread["threadId"] != args["threadId"]:
                raise StateError("native_bridge_identity_mismatch")
            for key in ("pendingRequestCount", "runCount", "itemCount"):
                integer(thread[key])
            for key in ("archived", "settled"):
                if type(thread[key]) is not bool:
                    raise StateError("native_bridge_result_incomplete")
            if type(value["hasMore"]) is not bool:
                raise StateError("native_bridge_result_incomplete")
            if value["nextPosition"] is not None:
                integer(value["nextPosition"])
            timestamp(thread["updatedAt"])
            if not isinstance(value["recentRuns"], list) or not isinstance(
                value["items"], list
            ):
                raise StateError("native_bridge_result_incomplete")
            ids = set()
            ordinals = set()
            for run in value["recentRuns"]:
                required(
                    run, ("runId", "ordinal", "status", "startedAt", "completedAt")
                )
                integer(run["ordinal"])
                if (
                    run["runId"] in ids
                    or run["ordinal"] in ordinals
                    or not 1 <= run["ordinal"] <= thread["runCount"]
                ):
                    raise StateError("native_bridge_result_incoherent")
                ids.add(run["runId"])
                ordinals.add(run["ordinal"])
                if (
                    run["runId"] == thread["latestRunId"]
                    and run["ordinal"] != thread["runCount"]
                ):
                    raise StateError("native_bridge_result_incoherent")
            if len(ids) > thread["runCount"] or (
                thread["latestRunId"] and thread["latestRunId"] not in ids
            ):
                raise StateError("native_bridge_result_incoherent")
            if thread["activeRunId"] and thread["activeRunId"] != thread["latestRunId"]:
                raise StateError("native_bridge_result_incoherent")
            for item in value["items"]:
                required(
                    item,
                    (
                        "itemId",
                        "sourceThreadId",
                        "runId",
                        "position",
                        "text",
                        "textTruncated",
                    ),
                )
                if (
                    item["sourceThreadId"] == thread["threadId"]
                    and item["position"] >= thread["itemCount"]
                ):
                    raise StateError("native_bridge_result_incoherent")
            previous = self.threads.get(thread["threadId"])
            if previous:
                old = previous["thread"]
                if (
                    timestamp(thread["updatedAt"]) < timestamp(old["updatedAt"])
                    or thread["runCount"] < old["runCount"]
                    or thread["itemCount"] < old["itemCount"]
                ):
                    raise StateError("native_bridge_stale_result")
                if any(
                    thread[k] != old[k]
                    for k in ("projectId", "parentThreadId", "worktreePath", "branch")
                ):
                    raise StateError("native_bridge_identity_changed")
                if thread["updatedAt"] == old["updatedAt"] and any(
                    thread[k] != old[k]
                    for k in (
                        "itemCount",
                        "runCount",
                        "latestRunId",
                        "activeRunId",
                        "status",
                        "pendingRequestCount",
                    )
                ):
                    raise StateError("native_bridge_revision_incoherent")
                for run in value["recentRuns"]:
                    prior = next(
                        (
                            r
                            for r in previous["recentRuns"]
                            if r["runId"] == run["runId"]
                        ),
                        None,
                    )
                    if prior and prior["completedAt"] is not None and run != prior:
                        raise StateError("native_bridge_terminal_run_changed")
            self.threads[thread["threadId"]] = value
        elif tool == "t3_thread_list":
            required(value, ("projectId", "threads", "nextCursor", "total"))
            if value["projectId"] != args.get("projectId", value["projectId"]):
                raise StateError("native_bridge_identity_mismatch")
            integer(value["total"])
            if not isinstance(value["threads"], list):
                raise StateError("native_bridge_result_incomplete")
            for row in value["threads"]:
                required(
                    row,
                    (
                        "threadId",
                        "parentThreadId",
                        "status",
                        "latestRunId",
                        "itemCount",
                        "updatedAt",
                    ),
                )
            if (
                len({r["threadId"] for r in value["threads"]}) != len(value["threads"])
                or len(value["threads"]) > value["total"]
            ):
                raise StateError("native_bridge_result_incoherent")
            if self.history:
                project = value["projectId"]
                if args.get("cursor") is None:
                    self.catalogs[project] = dict(
                        total=value["total"], ids=set(), cursor=None
                    )
                catalog = self.catalogs.get(project)
                if (
                    not catalog
                    or catalog["cursor"] != args.get("cursor")
                    or catalog["total"] != value["total"]
                ):
                    raise StateError("native_bridge_catalog_changed")
                ids = {r["threadId"] for r in value["threads"]}
                if ids & catalog["ids"]:
                    raise StateError("native_bridge_catalog_incoherent")
                catalog["ids"].update(ids)
                catalog["cursor"] = value["nextCursor"]
                if (
                    value["nextCursor"] is None
                    and len(catalog["ids"]) != catalog["total"]
                ):
                    raise StateError("native_bridge_catalog_incomplete")
        elif tool == "t3_project_list":
            required(value, ("projects", "nextCursor"))
            if not isinstance(value["projects"], list):
                raise StateError("native_bridge_result_incomplete")
            for row in value["projects"]:
                required(row, ("id", "workspaceRoot"))
        elif tool == "task_status":
            required(
                value,
                (
                    "taskId",
                    "childThreadId",
                    "childRunId",
                    "status",
                    "workState",
                    "hasPendingChildRuns",
                    "hasPendingQuestion",
                    "latestTerminalRunId",
                    "latestTerminalStatus",
                ),
            )
            if value["taskId"] != args["taskId"]:
                raise StateError("native_bridge_identity_mismatch")
            if (
                type(value["hasPendingChildRuns"]) is not bool
                or type(value["hasPendingQuestion"]) is not bool
            ):
                raise StateError("native_bridge_result_incomplete")
            previous = self.tasks.get(value["taskId"])
            if previous and any(
                previous[k] != value[k] for k in ("childThreadId", "childRunId")
            ):
                raise StateError("native_bridge_identity_changed")
            known = self.threads.get(value["childThreadId"])
            if (
                known
                and value["workState"] == "result_available"
                and not value["hasPendingChildRuns"]
            ):
                thread = known["thread"]
                if thread["activeRunId"] or thread["pendingRequestCount"]:
                    raise StateError("native_bridge_result_incoherent")
                if (
                    thread["latestRunId"] != value["childRunId"]
                    and value["latestTerminalRunId"] != thread["latestRunId"]
                ):
                    raise StateError("native_bridge_result_incoherent")
            self.tasks[value["taskId"]] = value
        elif tool == "t3_thread_send":
            required(value, ("threadId", "messageId"))
            if value["threadId"] != args.get("threadId") or not value["messageId"]:
                raise StateError("native_bridge_identity_mismatch")
        return value


def make_ack(frame, raw):
    """Input bytes must come directly from this frame's actual tool invocation."""
    required(
        frame,
        ("marker", "ack_protocol", "request_id", "tool", "arguments", "issued_at"),
    )
    if (
        frame["marker"] != "ORACULO_HOST_CALL_V1"
        or frame["ack_protocol"] != ACK_PROTOCOL
    ):
        raise StateError("native_bridge_protocol_mismatch")
    if not 0 <= time.time() - frame["issued_at"] <= 600:
        raise StateError("native_bridge_stale_frame")
    if len(raw) > MAX_RESULT_BYTES:
        raise StateError("native_bridge_result_too_large")
    HostResults(history=False).validate(
        frame["tool"], frame["arguments"], json.loads(raw)
    )
    return dict(
        request_id=frame["request_id"],
        ack_protocol=ACK_PROTOCOL,
        tool=frame["tool"],
        arguments_sha256=content_hash(frame["arguments"]),
        issued_at=frame["issued_at"],
        result_sha256=hashlib.sha256(raw).hexdigest(),
        result_zlib=base64.b64encode(zlib.compress(raw)).decode("ascii"),
    )


def decode_ack(frame, ack):
    if not isinstance(ack, dict) or ack.get("request_id") != frame["request_id"]:
        raise StateError("native_bridge_ack_mismatch")
    if (
        ack.get("ack_protocol") != ACK_PROTOCOL
        or ack.get("tool") != frame["tool"]
        or ack.get("arguments_sha256") != content_hash(frame["arguments"])
        or ack.get("issued_at") != frame["issued_at"]
    ):
        raise StateError("native_bridge_ack_correlation_mismatch")
    try:
        data = base64.b64decode(ack["result_zlib"], validate=True)
        decoder = zlib.decompressobj()
        raw = decoder.decompress(data, MAX_RESULT_BYTES + 1)
        if len(raw) > MAX_RESULT_BYTES or not decoder.eof or decoder.unused_data:
            raise ValueError
        if hashlib.sha256(raw).hexdigest() != ack["result_sha256"]:
            raise ValueError
        return json.loads(raw)
    except (ValueError, TypeError, KeyError, zlib.error):
        raise StateError("native_bridge_ack_invalid") from None


def register_parsers(commands):
    from pathlib import Path

    bridge = commands.add_parser("bridge")
    actions = bridge.add_subparsers(dest="bridge_action", required=True)
    ack = actions.add_parser(
        "ack", help="Relay an actual host result file without transcribing it"
    )
    ack.add_argument("--frame", type=Path, required=True)
    ack.add_argument("--from-file", type=Path, required=True)


def run_ack(args):
    frame = json.loads(args.frame.read_bytes())
    # Detect accidental reuse of a file captured before this call. This is not
    # attestation: a hostile local operator can rewrite a file or its metadata.
    if args.from_file.stat().st_mtime < frame["issued_at"]:
        raise StateError("native_bridge_stale_result_file")
    from oraculo.stdio import write_text

    write_text(canonical_json(make_ack(frame, args.from_file.read_bytes())) + "\n")
