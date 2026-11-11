# Managed adapter occupancy and interrupted clients

Operations on different tasks run at the same time. The daemon serializes only
one scope: `task_id` for an action that names an existing task, and
`(origin thread, key)` for `start` after the caller-bound identity read.
A second caller of that same scope receives `managed_scope_busy` at once, with
the in-progress `operation_id` and `scope`, and `retryable=true`. It does not
wait inside the daemon and it is not the generic busy error.

`managed_adapter_busy_retry_same_key` remains only for a real resource limit:
more than 32 admitted caller operations, or the offline ledger lock while the
daemon is alive. It does not identify a stuck flag, a capacity shortage, or a
particular project. The CLI retries a retryable task error a few times with the
same key and prints the final error as a JSON receipt on stdout.

The scope stays occupied for the whole operation, including while that
operation waits for a host ACK, memory, git, or publication. Other scopes keep
running, including that local work. SQLite writes use one `BEGIN IMMEDIATE`
transaction on a connection opened for that operation (WAL, `busy_timeout`
30s) and do not stay open across those waits. A rollback cannot discard
another operation's writes. The daemon loop does not run the operation:
managed work uses a dedicated pool, so memory and read requests keep being
accepted. Background reconciliation takes the same per-task scope, skips a
task whose scope is busy, and does not block callers of other tasks.
Cancelling one caller releases only that caller's scope.

Background reconciliation also closes a native child whose executor already
submitted its report, so the origin does not need a second bridge. The daemon
has no parent identity: it never calls `task_status`, and the completion
evidence records `daemon_reconciled_without_native_task_status`. Every other
proof is the one the caller-bound path uses, and that path still requires
`task_status`. A missing proof leaves the task where it was, is retried with
backoff, and produces one notice to the origin. The switch is
`managed_native_daemon_closure` (default `true`). See
`docs/MANAGED_TASKS.md`, "Fechamento de filho nativo pelo daemon".

`task resume --all-submitted [--project ID]` is one operation without a task
scope of its own. It claims each selected task's scope in turn, releasing the
previous one, and reports a task whose scope is busy as `skipped_busy` instead
of waiting. Identity and the project catalog are read once for the operation;
no thread read is shared between tasks.

The daemon holds `managed-tasks.lock` for its lifetime. Offline cancel and
legacy recovery still use a non-blocking flock against that lock. The daemon
does not flock once per command. `queued_callers` stays 0; there is no
cross-task waiter queue. `operations` lists every in-flight operation.

## Inspect before recovery

Run `oraculo --config CONFIG status` and retain the full JSON receipt and time.
`data.runtime` identifies the daemon; `data.managed_adapter` reports the owner.
`task status` and `task list` also include `adapter` in their service response.
Older daemons omit these fields; installing only a new CLI cannot add them.

- `state=active`, `mode=caller`: an actual connection handler owns that operation.
  `operation_id` identifies this socket request; `task_id`, when supplied by the
  action, identifies the durable task. `scope` is `task` or `intent`. These are
  different identities. `operations` lists every operation, not only the oldest
  one copied to the top level.
- `mode=background`: that task's reconciliation holds its scope until the
  worker finishes the task. Cancelling the asyncio waiter does not release it.
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

The resource-limit error remains `managed_adapter_busy_retry_same_key` with
`error.adapter` and `retryable=true`. Same-scope contention is
`managed_scope_busy` and carries `operation_id`. `service.jsonl` records
`managed_operation_started`, `managed_operation_finished` (with `wait_seconds`
and `duration_seconds`) and `managed_operation_rejected`. These diagnostics are
ephemeral observations, not durable host proofs or authorization to release
admission. No ledger schema migration is required.

## Interrupted transport behavior

The default manual bridge waits 600 seconds for each ACK. The daemon allows
900 seconds including frame backpressure; the caller allows 1200 seconds for
the next service frame. An alive slow manual responder keeps ownership. Ordinary
socket memory requests wait up to 125 seconds (`memory.cycle.*`). Background host
calls have a separate 60-second bound, with no manual bridge involved; a timeout
stops further host traffic for that batch and leaves durable returns pending.

EOF, malformed/mismatched ACKs and timeout fail the relay explicitly. After the
first failure the same channel cannot issue more calls, including return delivery.
The handler exits and releases its local ownership; task state, launch intent,
reservation, memory cycle and pending outbox remain governed by their existing
lifecycle rules. Closing a client or killing its owned bridge process does not
cancel remote work or establish absence of effects. There is no automatic task
relaunch in this transport layer.

`memory.cycle.*` uses a 125 second socket budget, the same class as rollout and
report verification, not the 5 second budget of ordinary envelopes. If that
budget expires, the task command fails with `memory_cycle_timeout` and
`retryable=true`. The CLI retries the same key. The task is not stored as
`blocked`; a reservation already taken stays so the retry can continue.

## Host proofs inside one operation

`start` and `resume` reuse a host result when the same operation asks for the
same proof again. `t3_project_list` and `t3_thread_read` with the same
arguments are one frame, not two. The cache lives only for that operation.

These gates always read the host again, because they need a fresh observation:

- Project binding (`_scope`, `managed_project_binding_changed`): `t3_project_list`.
- Native retry of the parent (`native_retry_parent_binding_changed`): that thread's `t3_thread_read`.
- Live child observation (`_observe`) and report prevalidation (commit matches HEAD): the child thread.
- Uncertain launch reconciliation: the matched child thread, after the catalog read.

These calls are never served from the cache: `orchestrator_capabilities` when
revalidating a route, terminal settlement snapshots, thread catalogs, cancel's
candidate catalog, `task_status`, and effect calls (`delegate_task`,
`t3_thread_launch`, pull-request link and unwatch, `t3_thread_send`).

Cancellation of a background waiter retains that task's scope until its real
worker exits. Shutdown also waits for that worker before closing the store. Host
calls are bounded, but synchronous Git/filesystem code still must finish or
reach its own timeout; the adapter deliberately does not clear a live worker's
scope, and doing so would not release any other task.

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
   When several submitted tasks are waiting, `task resume --all-submitted`
   closes them in one bridge session and returns one outcome per task.
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
