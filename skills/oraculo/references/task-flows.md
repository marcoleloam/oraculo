# Managed task flows

Use `oraculo task --help` for the installed CLI. Payloads are private JSON files,
outside the repository. Use a structured writer, never interpolate a user's brief
into a shell command. These examples contain placeholders, not host identities.

New work uses `oraculo task start --file request.json`:

```json
{
  "kind": "work",
  "key": "stable-original-message-key",
  "title": "Implement the requested change",
  "text": "Self-contained objective, scope, existing approvals, validation and return requirements",
  "cwd": "/workspace/example-project",
  "base": "HEAD"
}
```

Include `project` only when explicitly selected or canonically resolved. Never
invent caller/run/provider IDs. The adapter obtains identity from the host and
chooses the valid transport. A worktree outside its main checkout needs an actual
host/Git binding; a basename or similar title does not establish project scope.

Retain the returned `task_id` and original key. For `status` and `resume`, the
file contains `{"task_id":"returned-id"}`; `list` requires no payload. If the
request is already bound, inspect it before doing anything else. Same key with
different content is a conflict, not permission to choose another key.

Use `cancel` with `{"task_id": ...}` only from the origin, for a task that never
bound a child (lost launch response, unresolved reconciliation). The adapter
rechecks the host catalog first; a visible candidate thread refuses the cancel
and requires `resume`. A cancelled task frees its reservation and is never
relaunched. A task with a bound child is closed through finish, not cancel.
When no daemon answers the socket, `cancel` runs offline under the daemon and
ledger locks and reports `source: offline_ledger`; a live service refuses it.

For `continue`, send `task_id`, `key` (stable key of this steering message) and
`text`. The task and executor remain the existing ones. For a returned native
child, the origin uses `resume`; it does not send a second delegated review to the
backing child thread. New review rounds use the host's task mechanism only through
the authorized lifecycle and available capacity.

Read the complete selected memory artifact and validate its SHA-256 before using
it. `found` and confirmed `empty` are distinct valid reads; `unavailable` blocks.
Keep private context out of source, fixtures and public artifacts. Digest equality
proves transport integrity, not cognitive use.

An assigned managed executor submits `oraculo task finish --file report.json`:

```json
{
  "task_id": "assigned-managed-id",
  "report": {
    "summary": "Implemented the authorized change",
    "commit": "validated-clean-checkout-sha",
    "checks": [{
      "command": "project test command",
      "status": "passed",
      "result": "Final execution: exit 0",
      "proof": "/private/evidence/check-final.log"
    }],
    "decisions": [
      "History: project test command failed with exit 1 (/private/evidence/check-initial.log). Fixed empty-field serialization. Repeated the same command: passed, exit 0 (/private/evidence/check-final.log)."
    ],
    "limitations": ["External integration test was not run."]
  }
}
```

Checks record only the actual final execution of **each verification**: `status`
is `passed` or `failed`; optional check keys are `result`, `proof`, `layer`.
The example assumes the referenced executions and logs really exist. Before
submission, preserve corrected intermediate failures in `decisions` or
`limitations`, including the command, original result/status, correction and
evidence of the rerun. Never omit history or invent a status. If a verification's
final execution still fails, its `failed` check is mandatory. A passing partial
rerun or different command cannot erase another verification that still fails.
Any `failed` in `checks` keeps the report negative even with a later `passed`
for the same command; explicit `FAIL` also remains negative.
Put unexecuted checks in `limitations`, never invent a passed/failed result for
`not_run`. Never retrospectively alter an immutable report or remove a submitted
`failed`. A rejection with
`rejection.persisted=false` leaves the task unchanged: correct the same report
and retry the same ID. Positive reports require the actual full clean HEAD SHA.

For legacy malformed immutable submissions, use the explicit `recover-report`
flow in the project's managed-task contract, only from the real original origin.
It requires a running service advertising `invalid_report_recovery=true`, the
original canonical digest, corrected report and reason, plus fresh terminal and
memory evidence. It preserves the original and never replaces a valid/certified
report. An old daemon is a prerequisite blocker, not authority for local DB edits.

If the parent explicitly owns finish or the child is only native, return the
report natively and do not call managed finish for the parent's task. The origin
checks original run, descendants, checkout and validation before completion.
`validation_submitted` is only submission. Memory after work needs a correlated
confirmed write receipt and commit; pending or sent is insufficient.

A planning-only task must already declare `"deliverable_kind": "plan"` in the
`task start` request; finish cannot reclassify an implementation task. Its
report then adds `deliverable_kind: "plan"` and `approval_state: "pending"`,
plus document/commit and checks.
The visible state is `awaiting_approval`; do not publish or implement from that
report alone. Only the original origin records an existing user approval with
`task approve` and payload `task_id`, `commit` and `evidence` (the nonempty reference
to the user's approval). The follow-on implementation's `task start` includes
`approved_plan: {"task_id": "plan-task-id", "commit": "approved-plan-sha"}`.
Preserve that approval's scope; do not overwrite the immutable planning report or
repeatedly ask for the same approval. Managed mutations require daemon protocol 1;
`managed_service_upgrade_required` is a blocker, not permission to bypass the gate
with a legacy adapter or to upgrade a live service without operational scope.

Publication, review, merge, activation and settlement have distinct receipts and
authorization scopes. Follow the installed project's managed-task contract for
`publish`, `pr-check`, `merge`, `settle` and `unwatch`; checks/review apply to the
exact HEAD. A merge does not prove deployment. The origin remains responsible
after the child returns and after a PR is opened.

Keep long executor test suites in the foreground and await their completion.
Do not use background/nohup or a session that automatically starts another agent
turn when tests finish. A resumed child is not proof of a new managed task.

A native child may have several terminal runs in the same bound thread. Keep the
original `childRunId`; completion requires the entire contiguous history through
the latest run, an immutable report submitted during that latest run or later,
no active/queued work or live descendants, and fresh proof after confirmed memory.
Missing/truncated run history is a blocker, never permission to edit evidence.
The origin uses the existing task ID with `resume`; it does not rebind IDs or
resubmit an old report to refresh its timestamp. Parent-owned finish stays with
the parent after the executor's actual terminal return.
