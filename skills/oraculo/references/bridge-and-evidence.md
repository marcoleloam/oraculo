# Bridge and evidence

The managed adapter uses a caller-bound host bridge. If the host exports
`T3_ACP_MCP_NODE`, use the supported ACP transport. Otherwise use an available,
documented bridge command or native host tools via `--stdio-host --file payload.json`.
In Claude Code without ACP, answer the frames with `oraculo bridge relay` (below).
Never make the daemon impersonate the parent or synthesize a host response.

A harness may expose two T3 servers with the same tool names: a per-thread
injected one (tools prefixed `mcp__t3-code__`) and a generic HTTP one
(`mcp__t3__`). Only the injected server is caller-bound; its
`orchestrator_capabilities` returns `parentThreadId`. A null `parentThreadId`
or "not running inside a T3 thread" from the generic server is not evidence of
running outside a thread. Answer bridge frames with the caller-bound server.

For stdio, keep stdin open and process complete `ORACULO_HOST_CALL_V1` JSON frames.
Frames declare `ack_protocol=ORACULO_HOST_ACK_V2`. Invoke the named native tool
with exactly the supplied arguments once. Save its literal current JSON output
programmatically to a private file (including model options); never summarize,
redigit, edit or reuse a previous snapshot. With the exact frame saved separately:

```sh
oraculo bridge ack --frame /private/current-frame.json \
  --from-file /private/current-tool-result.json > /private/current-ack.json
```

Write that output line programmatically to the still-open adapter stdin. The
helper does no host calls and opens no service/vault/database. It compresses the
whole result reversibly, validates its minimum fields, and binds the ACK to the
request, tool, argument digest and frame time. The receiving transport also
checks consistency across frames. Capture the result file after this frame was
issued; an older mtime is refused. Failure/uncertain delivery requires the
existing operation's reconciliation, not reusing an ACK or creating a task.
Prefer the supported caller-bound ACP transport when available, which relays
actual output directly without model transcription. Do not invent an ACP binary,
credentials or a daemon session impersonating the caller.

## Claude Code without ACP: `bridge relay`

In Claude Code with no `T3_ACP_MCP_NODE`, `bridge relay` is the official path. It
reads the tool's literal output from the session log the harness already writes,
so the model never copies, saves or re-types a result. A closure can need more
than fifteen frames, some above 100 KB; do not relay those by hand.

Keep stdin open through a FIFO and send stdout to a private regular file:

```sh
mkfifo /private/run/in.fifo
( exec 3<> /private/run/in.fifo   # keeps the FIFO open between ACKs
  oraculo --config CONFIG task finish --file /private/run/payload.json \
    --stdio-host < /private/run/in.fifo > /private/run/out.txt 2> /private/run/err.txt
  echo $? > /private/run/exit.txt ) &
```

Start it once, in the background, as one command. The same pattern serves
`start`, `resume`, `progress` and the other bridged actions.

For each new complete `ORACULO_HOST_CALL_V1` line in `out.txt`, in this order:

1. Save that exact line to a frame file.
2. Call the named tool **now**, on the caller-bound `mcp__t3-code__` server, with
   exactly the frame's `arguments`. A call made before the frame appeared, or on
   the generic `mcp__t3__` server, is never accepted.
3. Run the relay and append its line to the FIFO:

```sh
oraculo bridge relay --frame /private/run/frame-N.json \
  --session-log auto >> /private/run/in.fifo
```

`--session-log auto` resolves the log from `CLAUDE_CODE_SESSION_ID` and the
working directory; pass the `.jsonl` path when that is ambiguous. The helper
matches the tool and the exact arguments, requires the call to be dated at or
after the frame's `issued_at`, and uses the earliest such call (it says so on
stderr when there are several). Calls made by a subagent of the session are
found. A result the harness truncated is read from the session's
`tool-results/` directory (`--tool-results-dir` overrides it). It waits up to
`--wait` seconds (default 10) for the harness to record the call.

It prints the same line as `bridge ack` and nothing else on stdout. On
`relay_result_not_found` there is no ACK: call the tool again and repeat the
relay for the same frame. Never answer with a result from an earlier frame, even
when tool and arguments are identical; every frame needs its own fresh call.
When `exit.txt` appears the operation ended; read `out.txt` for the receipt.

The stdio bridge trusts the local operator; it is not cryptographic attestation.
Schema, hashes and consistency catch omissions, corruption and detectable reuse;
they cannot detect every self-consistent fabrication or a rewritten old file.

On an older CLI affected by PTY output backpressure, stdout to a private regular
file with interactive stdin is a recovery workaround. Consume complete frames
by offsets and retain ACK state. Failure to print does not imply failure of an
external operation; recover with status/resume, not a new start. Prefer a release
whose transport checks have passed; do not modify the running global service to
work around a single invocation.

Record these independently, with source, time and correlation:

- `loaded_skill`: observable harness loading/invocation evidence. Discovery in a
  catalog alone is `detected`, not proof the body was loaded. If the host does not
  expose reliable evidence, use `not_observed` even after a successful CLI call.
- `called_cli`: process argv, exit and output evidence. A statement in model prose
  does not prove execution; a nonzero exit may follow a persisted external effect.
- `confirmed_service`: canonical response/receipt from the real isolated or live
  authorized service, with matching request/operation ID. `local_ledger` is an
  offline observation, not a fresh service confirmation. A fake CLI/socket in a
  routing test is simulated evidence and never qualifies as integration support.

Use the newest `snapshot_revision` for current state and event IDs for duplicate
presentation. Retain `occurred_at`, `delivered_at`, `historical` and `replayed` as
history. Refresh task status before presenting any control event as current state:
its snapshot was frozen on first send and may arrive later after a lost ACK.
This includes older cached events whose refresh_required field says false.
A notification never authorizes another task or regresses the current snapshot. Show pending
outbox delivery separately from failed work.
