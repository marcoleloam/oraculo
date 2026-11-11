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

### Interrupted before any effect

An operation records every effect it sends before sending it: `delegate_task`,
`t3_thread_launch`, `t3_thread_send`, pull-request link, watch and unwatch, any
other non-read host tool, a memory `finish`, and any call to the publication
or merge adapter. Reads are `orchestrator_capabilities`, `t3_project_list`,
`t3_thread_list`, `t3_thread_read`, `t3_thread_wait`, `task_status` and
`list_thread_pull_requests`.

If the host channel fails (`managed_host_ack_*`, `managed_host_channel_failed`,
`host_response_uncertain`, EOF, timeout or a transport error on a read) while
the operation has sent **only reads**, nothing could have changed. The adapter
then:

- does not write `blocked`; the task keeps its phase, events and revision;
- appends a local note to the task's `diagnostics` (last 20, shown by
  `task status`), with the action, the failing tool and the reason. This is
  not an event: no outbox row is created and nobody is notified;
- fails the command with the original error code, `retryable=true`,
  `interrupted=true` and a `recovery` object: the same command with the same
  payload (`task_id`, `key`, report).

The CLI does not repeat this error by itself. A hand-relayed bridge would
receive frames it did not ask for; the caller repeats the command.

After the first effect, the same channel failure is uncertainty about that
effect and still blocks, with the original code. `host_response_uncertain` as a
`blocked_reason` therefore always refers to an effect call. When the channel
has failed, the operation does not try to send return notices on it; they stay
unprepared in the outbox and the next operation sends only the newest state.

Every blocked response carries `recovery`: `command` (for example
`oraculo task resume`), `payload` with the task ID, and an `instruction`. The
field is persisted with the block, returned by `task status`, and removed when
the task leaves `blocked`. Correctable rejections that persist nothing carry it
too.

`memory.cycle.*` uses a 125 second socket budget, the same class as rollout and
report verification, not the 5 second budget of ordinary envelopes. If that
budget expires, the task command fails with `memory_cycle_timeout` and
`retryable=true`. The CLI retries the same key. The task is not stored as
`blocked`; a reservation already taken stays so the retry can continue.

## Host proofs inside one operation

Each host call is one frame, and on a manual bridge a model relays each frame.
Within one operation the adapter reads the caller identity
(`orchestrator_capabilities` and the caller's own thread) and the project
catalog (`t3_project_list`) once, and every later check of that operation
reuses them: the project binding (`managed_project_binding_changed`), launch
routing, and the parent binding of a native retry
(`native_retry_parent_binding_changed`). Nothing is kept for the next
operation.

These reads are always made again when their gate runs:

- Live child observation (`_observe`) and report prevalidation: the child thread.
- Uncertain launch reconciliation: the matched child thread, after the list read.
- `orchestrator_capabilities` when revalidating a route before the launch.
- Terminal gates: the child thread, the descendant proof and `task_status`.

Effect calls are never cached.

### Descendant proof

The closing gate must show that the child and everything below it has ended,
and that no descendant ended without success unless a review covers it. The
host list has no parent filter, so there are two proofs:

- **Catalog.** Every thread of the project, then a read of each descendant.
  This is the only proof that names descendants. It is used for a top-level
  child, for recovery, review and PR closure commands, and whenever descendant
  identities matter: the child started a task through the ledger, or the task
  has a recorded review, delivery, completion or supersession decision.
- **Status filter.** Used by `finish`, `resume`, reservation release and
  `publish` of a native child when none of the above applies. One
  `t3_thread_list` asks only for threads whose status is not `completed` and
  not `idle`, newest first, six rows per page. A descendant is created after
  its ancestor, so paging stops at the first row last updated before the child
  was created. Each remaining row is traced through its parents; a parent the
  list does not show costs one small `t3_thread_read`. If any live or
  unsuccessful descendant is found, the gate runs the catalog proof instead and
  blocks exactly as before (`children_still_running`,
  `terminal_descendant_review_required`).

Both proofs are project-scoped and not atomic. The status filter additionally
trusts the host's status index and timestamps: it names no successful
descendant, and it cannot notice a thread whose row says `completed` or `idle`
while it holds a run. A repeated row across pages means the list shifted and
fails closed. For a native child the host's own `task_status` (`workState`,
`hasPendingChildRuns`, `hasPendingQuestion`, latest terminal run) is checked in
every gate as well.

After the memory write, the same proof is read again before the reservation
is released: the child, the descendant proof (catalog or filtered list, as
before the write), `task_status` and the local checkout.

Launch reconciliation and cancel look for one title, so they pass
`titleContains` and still compare the exact title and parent on the rows.

### Frame budget

With a native child that has no descendants, measured by
`tests/test_managed_frames.py` on a fake host with 150 ended threads:

| Operation | Frames |
| --- | --- |
| `start` | capabilities, own thread, projects, `delegate_task`, child thread, `task_status`, return notice (7) |
| `finish` or `resume` by the origin | capabilities, own thread, projects; child thread, filtered list, `task_status`; memory write; child thread, filtered list, `task_status`; return notice (10) |

No argument or result in those sequences exceeds 10 KB in the test. On a real
host `t3_project_list` grows with the number of projects (the host offers no
filter) and `task_status` carries the child's summary.

The launch carries the request by reference. The request text is written to
the private `managed-contexts/` directory as `<sha256>.request.txt` (mode 0600,
UTF-8 bytes of the text only). The task text names the marker, task ID, title,
`request_artifact` (`path`, `sha256`, `bytes`), the memory reference and the
rules; the first rule tells the child to read the whole file and verify the
hash before anything else. Launch arguments are persisted once and replayed
byte for byte, so a task launched by an earlier version keeps its inline
request.

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
before its thread finished. `tests/test_managed_interruption.py` covers the
read-only interruption rule on the engine and on the same real socket service;
`tests/test_managed_frames.py` counts frames and bytes and covers the
descendant proofs. All host behaviour there is a fixture: the status filter,
ordering and timestamps of a real host are assumed, not tested. It does not diagnose the cause of any historical
production incident or certify live remote cancellation.
