# Managed adapter occupancy and interrupted clients

`managed_adapter_busy_retry_same_key` means another local managed operation owns
the single writer. It does not identify a stuck flag, a capacity shortage, a
particular project, or a continuously blocked interval. Both caller-bound
commands and background reconciliation use this exclusion.

Caller-bound mutations now enter a FIFO queue while another operation owns the
writer. Up to 32 callers can wait, for at most 60 seconds each. `queued_callers`
in the adapter diagnostic reports the current waiting count. Background
reconciliation yields to waiting callers. Timeout or a full queue preserves the
same `managed_adapter_busy_retry_same_key` error and creates no new task or host
effect; retry the original intent. Disconnecting or cancelling a waiter never
clears the active writer. Independent executors continue running concurrently;
only their ledger mutations are serialized.

## Inspect before recovery

Run `oraculo --config CONFIG status` and retain the full JSON receipt and time.
`data.runtime` identifies the daemon; `data.managed_adapter` reports the owner.
`task status` and `task list` also include `adapter` in their service response.
Older daemons omit these fields; installing only a new CLI cannot add them.

- `state=active`, `mode=caller`: an actual connection handler owns the operation.
  `operation_id` identifies this socket request; `task_id`, when supplied by the
  action, identifies the durable task. These are different identities.
- `mode=background`: the executor future owns the operation until its actual
  thread exits. Cancelling its asyncio waiter does not release ownership.
- `phase=host_ack` or `host_call`: `tool`, `host_request_id` (caller only),
  `timeout_seconds` and `phase_age_seconds` describe the current wait. No tool
  arguments, prompts, titles or memory contents appear in this diagnostic.
- `phase=memory`: a memory socket request is pending. `client_eof=true` can
  coexist with legitimate work already sent to the memory service. Disconnect
  is not a reason to clear the lock underneath that work.
- `phase=adapter`: local lifecycle work is executing. Age alone does not prove
  that it is stuck. `client_eof` is a socket observation, not a process heartbeat.
- `owner_unknown` or `owner_finished`: ownership cannot currently establish
  active work. Preserve the snapshot for investigation; there is no force-clear
  API. A completion callback may briefly be pending before an idle snapshot.
- `state=idle`: the adapter is available. This does not release reservations or
  prove that uncertain external work never ran.

The busy socket error remains compatible and includes `error.adapter` and
`retryable=true`. The CLI's error text stays stable; use the read-only status
command for details. These diagnostics are ephemeral observations, not durable
host proofs or authorization to release admission.

## Interrupted transport behavior

The default manual bridge waits 600 seconds for each ACK. The daemon allows
900 seconds including frame backpressure; the caller allows 1200 seconds for
the next service frame. An alive slow manual responder keeps ownership. Ordinary
socket memory requests retain their existing five-second bound. Background host
calls have a separate 60-second bound, with no manual bridge involved; a timeout
stops further host traffic for that batch and leaves durable returns pending.

EOF, malformed/mismatched ACKs and timeout fail the relay explicitly. After the
first failure the same channel cannot issue more calls, including return delivery.
The handler exits and releases its local ownership; task state, launch intent,
reservation, memory cycle and pending outbox remain governed by their existing
lifecycle rules. Closing a client or killing its owned bridge process does not
cancel remote work or establish absence of effects. There is no automatic task
relaunch in this transport layer.

Cancellation of a background waiter retains ownership until its real worker
exits. Shutdown also waits for that worker before closing the store. Host calls
are bounded, but synchronous Git/filesystem code still must finish or reach its
own timeout; the adapter deliberately does not clear a live worker's ownership.

## Supported continuation

1. Preserve the original task ID, stable key, command payload, operation/frame IDs
   and any actual host receipt. Inspect current service status and task status.
2. If the caller is alive and still handling the current frame, let it return the
   correlated ACK. Do not submit a replacement start while it is active. If it
   exited, let EOF/timeout unwind and observe the adapter becoming idle.
3. From the legitimate origin, use the existing caller-bound adapter with
   `task resume --file PAYLOAD` where PAYLOAD is `{"task_id":"ORIGINAL_ID"}`.
   If no task receipt was returned, retry the exact original start intent/key.
   This is an explicit reconciliation request, not permission for a new task.
   Native delegation reuses its persisted host key; uncertain independent
   launches reconcile their existing effects and must not be relaunched.
4. Confirm the same ID and reservation, host binding, canonical memory and pending
   returns through fresh status. A lost memory response can arrive after the
   caller timeout; reuse the same cycle. A lost return ACK reuses its event key.

There is no supported generic command to clear an uncertain lock/reservation or
to restart around maintenance gates. If the installed daemon lacks diagnostics,
retain its runtime identity and the original client evidence. Upgrade only through
the normal reviewed maintenance procedure after its quiescence gates pass. An
unknown external invocation remains fenced until its effects can be established;
an empty catalog or a disconnected client is insufficient evidence.

## Verification boundary

`tests/test_managed_disconnect.py` exercises a real temporary service, Unix
sockets, SQLite, Git and memory, including an abruptly terminated client process.
Host launches and replies are controlled fixtures. It demonstrates that EOF at
the first host ACK already unwound in the prior implementation, and protects the
separate regression where cancelling a background waiter released ownership
before its thread finished. It does not diagnose the cause of any historical
production incident or certify live remote cancellation.
