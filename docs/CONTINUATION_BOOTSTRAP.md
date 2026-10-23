# Complete native continuations before upgrading the daemon

`oraculo task recover-continuation-bootstrap` is a narrowly scoped compatibility
command for **01efe9cffd8a1874cb84e5230805055ad4345150**, protocol 1, main and task
ledger schema 4. Install the candidate wheel in a separate environment and run
its CLI against the existing configuration. It does not install, restart,
activate, change capacity, open maintenance, or create tasks/runs.

This fills the pre-upgrade gap: that exact daemon rejects completion when the
latest run differs from the original child run. Maintenance still requires zero
occupied slots. Normal `resume`, report-recovery capability checks, and the
maintenance gate are unchanged. This command is not a force-release mechanism.

## Required proof

Only the real, active **original origin conversation**, through its caller-bound
bridge, may invoke the command. The current origin run may differ from the run
that created the task; the task's original origin/run/memory identity is retained.
An executor or administrator in a different conversation cannot substitute for
that origin. Keep a cooperative window without new runs, pending messages,
descendants, checkout edits, or competing recovery/administrative operations.

The task must be unpublished `native_child`, with its existing executing
reservation, a valid immutable submitted implementation report and pristine
memory (`running/not_requested`). At least two complete terminal runs are
required. The command uses `run_continuity.py` and the existing execution gates:
contiguous ordinals, unique IDs, `runCount` equal to observed history, chronological
runs, original run first, latest run consistent with the thread and native task
status, no active run/question/queue or active descendant. The project catalog,
parent relation and isolated checkout/base ancestry must all agree. The checkout
must be clean and exactly at the full report commit, including negative reports.

The existing submission event must match the report digest and be from the
latest run or no earlier than its start. Modern submission actor/run provenance
must match. A legacy event without actor fields is accepted only with its retained
original timestamp and digest; a new timestamp is never manufactured. Reports
that predate continuation, are malformed, missing, plans, or require repair are
refused. The payload contains **no replacement report**. This command cannot
repair old `not_run` checks, expand abbreviated commits, or reuse a report from
another run. The separate malformed-report bootstrap is not broadened.

A positive report requires completed original/latest execution and the successful
execution gate. Valid negative reports retain their exact checks/verdict and
become `failed`, or `stopped` when the original execution was interrupted/cancelled,
matching normal completion semantics. A positive report with a failed/stopped
execution is rejected; it is not silently reclassified by this recovery.

## Ordering, exclusion and limits

1. Pin an official live runtime receipt: exact release, PID, configuration
   path/hash and protocol; require the daemon lifetime lock, both schema versions
   and open admission. An upgrade journal awaiting maintenance does not itself
   authorize release.
2. Hold the same local bootstrap flock used by report bootstrap. Open a real
   `managed.execute/status` request to that service, receive its initial capability
   frame, call the actual host tool, and withhold the ACK while the socket remains
   open. The old service has acquired its serialized managed-operation barrier
   before that frame. This fixed 840-second window is below its 900-second timeout.
   EOF/disconnect cancels recovery. No ACK is fabricated and no busy flag is reset.
3. Under that barrier, validate all proof and CAS a `continuation_bootstrap`
   intent containing the original payload bytes/hash, report/hash, reservation,
   runtime, actor, complete execution/descendant proof, checkout and memory cycle.
   The submitted report and pending work status remain unchanged. No provisional
   report completion is exposed for old `resume` to certify.
4. Ask the **old service** to publish the original report to its original memory
   cycle. Require a canonical report digest, `write_state=saved`, receipt
   `state=confirmed`, matching cycle operation ID and nonempty memory commit.
5. Re-read origin identity, full run/descendant proof, checkout, service and memory.
   Compare against the pre-memory proof; retries also pin the original intent's
   chain and descendant proof. With ledger and main write transactions held, CAS
   task bytes, memory public snapshot and exact reservation row. Atomically add
   the matching release audit and remove that reservation only.
6. Commit the main audit/release **before** updating the task result and its durable
   event/outbox. Preserve original reports, child/run identity and all history.
   Store `completion_evidence` for the upgraded daemon and flush the origin outbox.

The two SQLite databases and vault are not one distributed transaction. A crash
between release and task completion requires the matching main audit on retry;
absence of a reservation alone is not success. Replay after a lost memory or
notification ACK uses the same task/payload, revalidates live proof and does not
rewrite confirmed memory or duplicate release. A new execution after intent,
changed report/provenance, changed service PID/config, or mismatched audit blocks
automatic replay. There is no adopt, reset, override or offline fallback.

SQLite access is encapsulated in this reviewed CLI module, without schema
migration or operator SQL scripts. The daemon remains the memory publisher.
The barrier excludes managed callers/background completion. It does not fence
T3 globally, Git edits, or all administrative/memory paths; final CAS covers local
concurrent changes. Host reads are sequential and bounded (50 runs per thread,
100 descendants, 1000 project threads). Missing/truncated/incoherent evidence
fails closed. This is a cooperative local operator boundary, not cryptographic
host attestation. A new remote run after the final observation remains outside
local atomicity: maintain the coordinated window through release and activation.

Old daemons, including a subsequent 47f runtime, may expose the new private journal
in ordinary status/list output or exceed response limits. Keep receipts private.
The candidate recovery response excludes it; the candidate's daemon status/list
projection exposes only structural summary fields. Never edit/delete the journal
to make status fit. The command's identical retry is the bounded recovery receipt.

## Origin runbook before activation

The coordinator distributes the reviewed wheel and instructions. Each legitimate
origin executes its own tasks; the coordinator must not impersonate them.

1. Preserve the original submitted report artifact. With official read-only CLI
   calls obtain the task status/history and `oraculo status` runtime receipt.
   Confirm the exact runtime, task identity and report event digest. Do not read
   the operational databases or vault. No preparatory task/finish is needed.
2. Write a private request file, using the existing report digest and fresh runtime
   object verbatim. `expected_report_sha256` is the canonical `content_hash` of the
   submitted report, not a hash of formatted JSON file bytes:

   ```json
   {
     "task_id": "existing-managed-task-id",
     "expected_report_sha256": "digest-from-original-submission-event",
     "reason": "Complete the existing submitted result after proven continuation",
     "expected_service": {
       "pid": 12345,
       "release_id": "01efe9cffd8a1874cb84e5230805055ad4345150",
       "config_path": "/absolute/config.toml",
       "config_sha256": "official-runtime-config-hash",
       "protocol_version": 1
     }
   }
   ```

3. In that origin, run the wheel's CLI with its real caller-bound ACP transport:

   ```sh
   /path/to/recovery-venv/bin/oraculo --config /absolute/config.toml \
     task recover-continuation-bootstrap --file /private/request.json
   ```

   If ACP is unavailable, use `--stdio-host`. Relay each fresh
   `ORACULO_HOST_CALL_V1` through the real named tool with its exact arguments.
   Use `oraculo bridge ack --frame FRAME --from-file LITERAL_RESULT` as described
   in [RUN_CONTINUITY_AND_ACKS.md](RUN_CONTINUITY_AND_ACKS.md), copying its ACK to
   the waiting stdin. Keep stdin open and stdout in a private regular file when
   the host PTY is constrained. Never hand-author/summarize ACKs, reuse old output,
   or substitute another conversation's tools. No report command is executed.
4. Expected successful receipt: `recovery=confirmed`,
   `source=bootstrap_with_exact_live_service`, `admission.state=not_reserved`,
   `memory.write_state=saved`, `memory.write_receipt.state=confirmed`, receipt
   operation ID equal to the original cycle ID, and `pending_returns=0`.
   Positive work has `work_status=succeeded`, `phase=pr_pending`, exit 0.
   Negative work has `work_status=failed|stopped`, `blocked_reason=work_not_successful`,
   exit 1 **with** `recovery=confirmed`. Negative review is never approval.
5. If the response is lost, memory is uncertain, or `pending_returns>0`, keep the
   identical request and retry in the same origin after the previous invocation
   exits. Do not launch another task, resubmit a report, restart the daemon, change
   the expected PID, or remove a reservation. Retain all logs. Intent/service/host
   mismatch requires investigation; elapsed time does not authorize a bypass.
6. Once every origin has confirmed its own receipts, the coordinator observes
   global occupancy using the official CLI. Other reservations remain blockers.
   The coordinator must also observe this implementation executor terminal and
   finish its assigned task; the executor must not finish itself. Only after
   occupied=0 should the coordinator resume the already prepared official runtime
   activation. Its own maintenance/identity checks remain mandatory. Confirm the
   new daemon receipt independently; recovery success is not activation proof.

## Reproducible isolated validation

Set `ORACULO_CONTINUATION_LEGACY_SITE` to an installed copy of the exact 01efe
package. The test fixture checks a pinned digest of every Python source file,
then copies it into a temporary directory. It starts a real old Service/socket
with disposable SQLite/Git/vault; T3/provider and lifecycle observations are
explicit fixtures. The operational config, databases and vault are never used.

```sh
ORACULO_CONTINUATION_LEGACY_SITE=/private/isolated-legacy-site \
  /path/to/python -m pytest -q tests/test_continuation_bootstrap.py
```

Run suites in the foreground. Repeat from an installed candidate wheel with
`PYTHONPATH` pointing at that wheel's site and subprocess imports verified. Tests
include actual CLI process exit/retry with ACK V2, concurrency, negative outcomes,
stale reports, activity/queue/descendants, dirty checkout, incomplete history,
service replacement, memory/ledger/reservation changes and audited release replay.
These proofs do not certify live T3 auth, launchd, scheduler, all providers or the
real origins' eligibility. Operational recovery still requires their fresh proof.
