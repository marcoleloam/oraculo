# Bridge and evidence

The managed adapter uses a caller-bound host bridge. If the host exports
`T3_ACP_MCP_NODE`, use the supported ACP transport. Otherwise use an available,
documented bridge command or native host tools via `--stdio-host --file payload.json`.
Never make the daemon impersonate the parent or synthesize a host response.

A harness may expose two T3 servers with the same tool names: a per-thread
injected one (tools prefixed `mcp__t3-code__`) and a generic HTTP one
(`mcp__t3__`). Only the injected server is caller-bound; its
`orchestrator_capabilities` returns `parentThreadId`. A null `parentThreadId`
or "not running inside a T3 thread" from the generic server is not evidence of
running outside a thread. Answer bridge frames with the caller-bound server.

For stdio, keep stdin open and process complete `ORACULO_HOST_CALL_V1` JSON frames.
Call the named native tool with exactly the frame's arguments, once; return its
actual `request_id` and `result` in one JSON line on stdin. Do not execute a partial
frame. Tool failure and uncertain delivery require the existing operation's
reconciliation. ACKs, task IDs and deduplication keys must remain correlated.
The stdio bridge trusts the local operator; it is not cryptographic attestation.
The bridge waits up to 600 s for each ACK (`ORACULO_BRIDGE_ACK_TIMEOUT` seconds
overrides it); the daemon relay waits longer. Answer promptly anyway: while a
frame is open, the daemon serializes every other managed operation.

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
