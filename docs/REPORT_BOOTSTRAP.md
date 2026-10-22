# Recover a malformed report before upgrading the daemon

`oraculo task recover-report-bootstrap` is an explicit compatibility command for
release `3d857301cf8de81725645e04c25a17ce4929b6af`, protocol 1, main and task-ledger
schema 4. Run the candidate CLI from a separate installation; the old daemon
continues serving memory. This command neither installs nor activates a release.
`recover-report` retains its newer-daemon capability gate.

## Eligibility and authority

Only the original origin's active caller-bound bridge may recover its own
**native_child**, unpublished implementation. A coordinator, executor, supplied
identity, or approval message cannot stand in for that origin. The child must
have its original run as its current completed run, no pending work or active
descendants, and a clean isolated Git checkout at the report commit descended
from the task base. The live project catalog and both native task status and
thread snapshots must agree. No new task, launch, thread, or run is created.

The initial immutable submission must have failed with
`malformed_validation_check`, with a matching historical submission digest,
retained executing reservation, and pristine bound memory. The **only** repair
moves each `not_run` or `blocked` check verbatim (canonical JSON) to an appended
limitation prefixed `Not executed (original check): ` or
`Blocked validation (original check): ` respectively. A blocked validation is
never asserted to have passed or to have been unexecuted. A passed check using
the unsupported legacy metadata key `detail` is preserved verbatim as canonical
JSON in an appended `Original check with legacy detail: ` limitation; only that
key is removed from its executable check entry. Its command, status and any
existing result/proof stay unchanged. No metadata is promoted into a verdict
or overwrites a result. Other unknown check keys are rejected. At least one
passing check and one `not_run`/`blocked` check are required. All other fields
stay unchanged, except an abbreviated commit of 7–39 lowercase hexadecimal
characters may be expanded by Git in the persisted child checkout. Git must
resolve it without ambiguity to that checkout's clean HEAD. Both fresh host
observations still verify the real child binding, base ancestry and exact HEAD.
The original abbreviation is retained in an appended limitation and the original
report audit. This reads Git objects only; it creates or rewrites no commit.
Failed checks, negative verdicts, false/unknown verdict values, planning reports,
and any other report correction are outside this compatibility command.

If a refused `continue` replaced the visible block with
`terminal_task_requires_explicit_new_revision`, the original malformed-validation
event must still occur after the latest submission in the retained history.
The same original completed run and all other gates remain mandatory. Missing
history, another blocking reason, or any new run prevents this recovery.

## Audited exclusion boundary

The exact release's `ManagedService.execute` acquires `busy` before its first
`orchestrator_capabilities` frame. The command opens a real `managed.execute`
`status` operation, invokes the actual native capability tool, withholds its ACK,
and keeps that socket open for at most 840 seconds (old ACK limit: 900 seconds).
Each of the two full observations uses one successful-execution snapshot for
terminal, descendant and checkout validation, instead of re-reading the same
child for each gate. Both observations remain independent across the memory
write; no evidence is cached across that boundary. The deadline is fixed, not
renewable. The budget includes manual native-tool bridge latency.
No task has been loaded by that held operation. Other caller mutations fail busy.
EOF, timeout, or a changed service identity aborts recovery; nothing force-clears
`busy`. Cooperative origins must avoid other changes to the target during this
bounded procedure. Host and Git observations are sequential, not a remote fence.

**The old background cancellation defect is not assumed fixed.** Cancelling its
`serialized_batch` waiter can clear `busy` while its thread remains alive. In the
exact release, however, `ManagedTasks.reconcile` returns immediately through
`flush_returns` for an already launched native child. That branch never changes
the task payload, report, memory, or reservation. A surviving worker can deliver
or acknowledge outbox entries; those use immutable IDs and independent rows.
SQLite transactions serialize those writes. It cannot finish this recovery.
A `managed_thread` task is rejected precisely because its old background branch
can finish work and release admission. The test runner reproduces the old defect
with a real worker and proves the native restriction; it does not patch the old
service or assert global worker exclusion. Other tasks and their reservations
are unchanged.

The normal exact `serve` path does not cancel `managed_flush`: it closes admission
at the server, cancels its polling loop, then joins the managed worker. There is
no production operation for cancelling that worker. Nevertheless the native-only
boundary above also covers the injected waiter-cancellation case.

The runtime identity is pinned by an official status response and checked again
before effects/release: exact release, live PID, config path/hash and protocol.
The daemon lifetime flock must be held and both existing database schemas must
be 4. The local operator/socket/stdio bridge remains the trust boundary; these
are not cryptographic attestations against a malicious same-user process.

## Durable ordering and replay

1. Validate the caller, original report/hash/history, exact replacement, native
   terminal evidence, checkout, reservation and bound pristine memory.
2. CAS an inert `report_bootstrap` journal into the task with original payload
   bytes/hash, report/hash, replacement/hash, reason, actor, host snapshot,
   service identity, reservation and pre-write cycle. The malformed submission
   stays intact. A process crash or legacy `resume` cannot turn it into success.
3. Ask the **old service** to finish/publish the exact replacement evidence to
   the same memory cycle. Reconstruct its canonical report hash and require
   `write_state=saved`, receipt `state=confirmed`, matching cycle operation ID,
   and a nonempty commit. Memory pending/failed/uncertain never releases capacity.
4. Re-read caller, original/current run, descendants, checkout, service and cycle.
   A concurrent task/report/cycle/reservation/host change cancels before release.
   Take ledger and main SQLite write transactions, CAS the original task bytes,
   compare the complete public cycle to the socket snapshot, and atomically
   record the release audit plus delete **that exact reservation row**.
5. Commit the main release before replacing the ledger's report/result and adding
   its event/outbox. Two WAL databases and the vault are not one transaction.
   A crash between these phases leaves the old malformed submission plus a
   matching release audit and confirmed memory; the identical retry completes
   the ledger without releasing again or rewriting memory. A missing reservation
   without that exact audit blocks. Finally flush the origin's durable outbox.

Use the **same payload and task ID** after timeout, lost response, or lost
notification ACK. After a daemon restart/config change, the pinned journal
refuses automatic rebinding even if the payload is edited to the new identity.
Preserve the journal and reconcile through a separately authorized operation;
there is no `force`, adopt, reset, or admin override.

Status output from an old daemon can include the compatibility journal because
that daemon predates the new field. Keep status/receipts in private files; do not
paste them into public issues. Only structural hashes go into return events.
Large legacy journals can exceed the old CLI's 1 MiB response limit even after
successful recovery. This candidate's daemon exposes only the journal version
and report digests in status/list, while retaining the complete private journal
for retry and audit. That public projection takes effect only after activation;
an old-client read failure is not evidence that the recovery failed.

## Invocation from the legitimate origin

Prepare a private JSON request using the original immutable report artifact and
an official `status` response. `replacement_for(task, report)` is a read-only helper:
it requires the task's original `request.deliverable_kind` (default implementation)
and performs the lossless conversion above. Abbreviated commits require reading
the persisted child checkout's Git metadata. It executes no report command.

```json
{
  "task_id": "the-existing-task-id",
  "expected_report_sha256": "canonical-content_hash-of-original-report",
  "report": {"checks": [{"command": "actually executed command", "status": "passed"}], "limitations": ["Not executed (original check): {\"command\":\"not executed command\",\"status\":\"not_run\"}"], "commit": "validated-commit", "summary": "unchanged original summary"},
  "reason": "Move the original unexecuted checks verbatim to limitations",
  "expected_service": {"pid": 12345, "release_id": "3d857301cf8de81725645e04c25a17ce4929b6af", "config_path": "/absolute/config.toml", "config_sha256": "official-config-hash", "protocol_version": 1}
}
```

```sh
/path/to/candidate/bin/oraculo --config /absolute/config.toml status > /private/service.json
/path/to/candidate/bin/oraculo --config /absolute/config.toml task recover-report-bootstrap \
  --stdio-host --file /private/recovery.json > /private/recovery-receipt.jsonl
```

Keep stdin open; invoke each complete `ORACULO_HOST_CALL_V1` frame's named native
tool exactly and return its actual result and request ID. An actual ACP bridge
may replace `--stdio-host`. Never manufacture an identity/ACK or run these frames
from another origin. The private operational runbook provides the case-specific
paths and payload preparation. No operational recovery is performed by tests.

Acceptance is `recovery=confirmed`, same task/child/run IDs, original audit
preserved, exact replacement in memory, `saved/confirmed`, no target reservation,
and one release audit. Other occupied tasks still prevent maintenance. Only the
existing official maintenance/activation workflow can subsequently update the
service; this command does not bypass its quiescence gate.

## Reproducible tests

The suite takes an explicitly selected installed legacy package, verifies the
SHA-256 of its complete Python source inventory, then copies that package into
a disposable directory. The real old `Service`, socket, SQLite, memory publisher,
vault and Git worktrees run in disposable environments. Only T3 and daemon
lifecycle control are simulated. No live service, launchd or provider is used.

```sh
ORACULO_BOOTSTRAP_LEGACY_SITE=/isolated/legacy/site-packages \
  /path/to/candidate/python -m pytest -q tests/test_report_bootstrap.py
```

The inventory digest is
`7e55111d5d42bb982db6c23d95ef6df0823cc99d0f11f144e5a09f43607a8616`.
Without the explicitly supplied exact package these tests skip, rather than
substituting a simulated old protocol. Run against source and a separately
installed candidate wheel; check `oraculo.report_bootstrap.__file__` to distinguish
the two. Coverage includes original not_run poisoning, wrong caller/service,
terminal/descendant/checkout guards, exact repair, old cancelled worker, real
process crash after memory, real daemon restart, reservation/cycle/task CAS,
timeout, response/ACK loss, and repeated completion without duplicate effects.
