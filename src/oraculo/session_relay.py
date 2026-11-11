"""Relay a host result from a Claude Code session log, without transcription.

`bridge relay` answers one ``ORACULO_HOST_CALL_V1`` frame with the literal
output of the matching tool call recorded by the harness. The model calls the
tool; this helper finds that call in the session JSONL and builds the ACK, so
no model output is ever copied by hand.

Same trust boundary as `bridge ack`: the local operator, NOT attestation.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

from oraculo.protocol import canonical_json
from oraculo.state import StateError

# Only the per-thread injected server is caller-bound. The generic HTTP server
# exposes the same tool names without the caller's identity.
CALLER_BOUND_PREFIX = "mcp__t3-code__"
GENERIC_PREFIX = "mcp__t3__"
# Harness notices for a result too large to keep inline. The path is only a
# hint for the file name; the file is always read from the tool-results dir.
SAVED_NOTICES = (
    re.compile(r"Output has been saved to (?P<path>[^\n]+?\.[A-Za-z0-9]+)\.?(?:\n|$)"),
    re.compile(r"Full output saved to:? (?P<path>[^\n]+?\.[A-Za-z0-9]+)\.?(?:\n|$)"),
)


def _dated(value) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.timestamp()


def _same(left, right) -> bool:
    try:
        return canonical_json(left) == canonical_json(right)
    except (TypeError, ValueError):
        return False


def _records(path: Path, needles: tuple[str, ...]):
    """Parsed lines holding one of the needles. Unreadable lines are skipped."""
    try:
        stream = path.open(encoding="utf-8", errors="replace")
    except OSError:
        return
    with stream:
        for line in stream:
            if not any(needle in line for needle in needles):
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue  # A line still being written by the harness.
            if not isinstance(entry, dict):
                continue
            message = entry.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            if isinstance(content, list):
                yield entry, [block for block in content if isinstance(block, dict)]


def transcripts(session_log: Path) -> list[Path]:
    """The session log and the transcripts of its subagents."""
    paths = [session_log]
    directory = session_log.with_suffix("")
    if directory.is_dir():
        paths += sorted(
            p for p in directory.rglob("*.jsonl") if p.is_file() and p != session_log
        )
    return paths


def find_calls(frame: dict, paths: list[Path]) -> tuple[list[dict], int]:
    """Matching caller-bound calls, and how many used the generic server.

    Each call has ``id``, ``at`` (earliest dated record of that tool_use id, or
    None) and ``results`` (distinct bodies recorded for it).
    """
    tool = frame["tool"]
    accepted = {tool, CALLER_BOUND_PREFIX + tool}
    calls: dict[str, dict] = {}
    generic = 0
    for path in paths:
        for entry, blocks in _records(path, ('"tool_use"',)):
            moment = _dated(entry.get("timestamp"))
            for block in blocks:
                if block.get("type") != "tool_use" or not isinstance(
                    block.get("id"), str
                ):
                    continue
                if not _same(block.get("input"), frame["arguments"]):
                    continue
                if block.get("name") == GENERIC_PREFIX + tool:
                    generic += 1
                    continue
                if block.get("name") not in accepted:
                    continue
                call = calls.setdefault(
                    block["id"], dict(id=block["id"], at=None, results=[], error=False)
                )
                # The harness repeats an entry, sometimes undated. Keep the
                # earliest dated record: freshness is never assumed.
                if moment is not None and (call["at"] is None or moment < call["at"]):
                    call["at"] = moment
    if not calls:
        return [], generic
    needles = tuple(calls)
    for path in paths:
        for _entry, blocks in _records(path, needles):
            for block in blocks:
                call = calls.get(block.get("tool_use_id"))
                if call is None or block.get("type") != "tool_result":
                    continue
                if block.get("is_error"):
                    call["error"] = True
                    continue
                body = block.get("content")
                if not any(_same(body, known) for known in call["results"]):
                    call["results"].append(body)
    return list(calls.values()), generic


def _blocks(body) -> bytes | None:
    """Bytes for content blocks, as `bridge ack` accepts ``{"content": blocks}``."""
    if not isinstance(body, list) or not all(isinstance(b, dict) for b in body):
        return None
    texts = [b for b in body if b.get("type") == "text"]
    if any(not isinstance(b.get("text"), str) for b in texts) or not texts:
        return None
    if len(body) == 1:
        return texts[0]["text"].encode("utf-8")
    return json.dumps({"content": body}, ensure_ascii=False).encode("utf-8")


def literal_result(body, tool_results: Path, issued_at: float) -> bytes:
    """The tool's own output bytes: inline, or the file the harness saved."""
    if isinstance(body, list):
        raw = _blocks(body)
        if raw is None:
            raise StateError("relay_result_unsupported_content")
        text = raw.decode("utf-8") if len(body) == 1 else None
    elif isinstance(body, str):
        raw, text = body.encode("utf-8"), body
    else:
        raise StateError("relay_result_unsupported_content")
    saved = None
    if text is not None:
        try:
            json.loads(text)
        except ValueError:
            for notice in SAVED_NOTICES:
                saved = notice.search(text)
                if saved:
                    break
            if saved is None:
                raise StateError("relay_result_not_json") from None
    if saved is None:
        return raw
    # The notice names an absolute path. Only its file name is used, inside the
    # tool-results directory, so transcript text cannot point elsewhere.
    name = Path(saved.group("path").strip()).name
    path = tool_results / name
    try:
        if path.is_symlink() or not path.is_file():
            raise OSError
        if path.stat().st_mtime < issued_at:
            raise StateError("native_bridge_stale_result_file")
        data = path.read_bytes()
    except OSError:
        raise StateError("relay_result_file_missing") from None
    try:
        parsed = json.loads(data)
    except ValueError:
        raise StateError("relay_result_not_json") from None
    # Some harness versions save the content blocks instead of the bare output.
    wrapped = _blocks(parsed) if isinstance(parsed, list) else None
    return wrapped if wrapped is not None else data


def select(frame: dict, session_log: Path, tool_results: Path, warn) -> bytes:
    calls, generic = find_calls(frame, transcripts(session_log))
    issued = frame["issued_at"]
    fresh = sorted(
        (c for c in calls if c["at"] is not None and c["at"] >= issued),
        key=lambda c: c["at"],
    )
    answered = [c for c in fresh if c["results"]]
    if not answered:
        stale = sum(1 for c in calls if c["at"] is not None and c["at"] < issued)
        undated = sum(1 for c in calls if c["at"] is None)
        failed = sum(1 for c in fresh if c["error"])
        waiting = len(fresh) - failed
        warn(
            "relay: no usable call of "
            f"{CALLER_BOUND_PREFIX}{frame['tool']} with these exact arguments at or "
            f"after the frame was issued (older calls refused: {stale}; without an "
            f"established time: {undated}; errored: {failed}; still without a "
            f"result: {waiting}; on the non caller-bound {GENERIC_PREFIX} server: "
            f"{generic}). Call the tool again now, then repeat bridge relay."
        )
        raise StateError("relay_result_not_found")
    chosen = answered[0]
    if len(chosen["results"]) > 1:
        raise StateError("relay_result_ambiguous")
    if len(answered) > 1:
        warn(
            f"relay: {len(answered)} fresh matching calls; using the earliest one "
            f"after the frame was issued ({chosen['id']})."
        )
    return literal_result(chosen["results"][0], tool_results, issued)


def projects_root() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(base) if base else Path.home() / ".claude") / "projects"


def resolve_session_log(value: str, *, cwd: Path | None = None) -> Path:
    if value != "auto":
        return Path(value)
    session = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", session):
        raise StateError("relay_session_log_unresolved")
    root = projects_root()
    slug = re.sub(r"[^A-Za-z0-9]", "-", str(cwd or Path.cwd()))
    direct = root / slug / f"{session}.jsonl"
    if direct.is_file():
        return direct
    found = sorted(root.glob(f"*/{session}.jsonl")) if root.is_dir() else []
    if len(found) == 1:
        return found[0]
    raise StateError(
        "relay_session_log_ambiguous" if found else "relay_session_log_unresolved"
    )


def register(actions) -> None:
    relay = actions.add_parser(
        "relay",
        help=(
            "Answer a frame with the literal result of the matching tool call "
            "recorded in a Claude Code session log"
        ),
    )
    relay.add_argument("--frame", type=Path, required=True)
    relay.add_argument(
        "--session-log",
        required=True,
        help=(
            "Session JSONL, or 'auto' to resolve it from CLAUDE_CODE_SESSION_ID "
            "and the working directory"
        ),
    )
    relay.add_argument(
        "--tool-results-dir",
        type=Path,
        help="Default: <session-log without .jsonl>/tool-results",
    )
    relay.add_argument(
        "--wait",
        type=float,
        default=10.0,
        help="Seconds to wait for the harness to record the call (default 10)",
    )


def run(args) -> None:
    from oraculo.host_ack import make_ack, required
    from oraculo.stdio import write_text

    frame = json.loads(args.frame.read_bytes())
    required(
        frame,
        ("marker", "ack_protocol", "request_id", "tool", "arguments", "issued_at"),
    )
    if type(frame["issued_at"]) not in (int, float) or not isinstance(
        frame["tool"], str
    ):
        raise StateError("native_bridge_result_incomplete")
    session_log = resolve_session_log(args.session_log)
    if not session_log.is_file():
        raise StateError("relay_session_log_unresolved")
    tool_results = args.tool_results_dir or session_log.with_suffix("") / "tool-results"
    deadline = time.monotonic() + max(0.0, min(args.wait, 300.0))
    notes: list[str] = []
    while True:
        notes.clear()
        try:
            raw = select(frame, session_log, tool_results, notes.append)
            break
        except StateError as exc:
            # The harness writes the log asynchronously. Only absence is retried.
            if str(exc) not in {"relay_result_not_found", "relay_result_file_missing"}:
                raise
            if time.monotonic() >= deadline:
                for note in notes:
                    print(note, file=sys.stderr)
                raise
            time.sleep(0.25)
    for note in notes:
        print(note, file=sys.stderr)
    try:
        ack = make_ack(frame, raw)
    except json.JSONDecodeError:
        raise StateError("relay_result_not_json") from None
    write_text(canonical_json(ack) + "\n")
