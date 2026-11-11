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

For an unsuccessful terminal review descendant, use `diagnose-review`
on the existing task first. `adjudicate-review` requires an actual technical human
review, verified test artifacts and two human host messages bound to its digest,
exact scope, immutable report and final HEAD. Keep message IDs outside the review
artifact to avoid a circular hash. An original negative host-only interruption
also needs an explicit human disposition of each original check, run, scope, log
and confirmed failed-memory digest; a technical FAIL or functional failure stays
blocked. Generic implementation authorization and self-declared human/approved
JSON do not qualify. The host exposes message role/source, not individual human
identity; independence is human-attested, not automatically verified. Preserve
the original failed child/report/memory; never route declined content to another
model/provider. See `docs/TERMINAL_REVIEW_ADJUDICATION.md` in the project. These
commands require `terminal_review_adjudication=true` from the live service.

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

After a native child submits, the service closes the task by itself and sends
one final notice; the evidence then records
`daemon_reconciled_without_native_task_status`. If a notice says automatic
closure is pending, or several submitted tasks wait, the origin runs
`oraculo task resume --all-submitted [--project ID]` (no `--file`): one bridge
session, one outcome per task. Publication of a native task stays with the origin.

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

Azure Repos and existing integration PRs use the same assigned task IDs; never
restart accepted tasks to reconcile an external merge. The remote selects the
adapter. Use an explicit publication binding, reconcile_only=true and pr_id or
pr_url for an externally published PR. Keep source_commit equal to report.commit;
integration={head_sha,contains:[commits]} declares the integration HEAD and every
commit to verify locally by Git ancestry. Every declared object must exist and
be an ancestor; a list, cherry-pick or matching tree is insufficient. The clean
publication checkout's remote/repo/branch/base/head must match the REST receipt.
Repeat the same payload after uncertain ACK; it cannot create or merge again.
An already completed PR is observed as pr_reconciled. Then use pr-check with
policy.observe_only=true (merge_authorized=false) to observe the merge and run
the official memory/settlement gates; settle resumes after a preserved merge
receipt. Full contract and sanitized payloads: docs/MANAGED_TASKS.md.

Azure observation uses REST Git 7.1 and Policy 7.1-preview.1, independently of T3
native tracking/watch. The adapter does not enable unverified Azure native
linking/watch: its local limitation record is not a host ACK. Keep all other
native linked PRs gated; do not fake a GitHub URL or install a polling loop.
Active Azure PRs need an explicit later pr-check. Source reports, runs, negative
results and per-task authorization/memory remain immutable even with shared PRs.

Azure PATs belong only in the selected publication checkout's ignored .env as a
literal SYS__AZDO_PAT assignment, or an already configured explicit Keychain
service/account reference. Never source .env, print or copy a PAT into payloads,
logs, URLs, command arguments or Git. T3 OAuth credentials are unrelated. No host
secret-reference capability is assumed. HTTP redirects are refused entirely.

Deployment is optional and separate: task deployment records a reported claim
with full merge release SHA, environment, timezone timestamp and private proof
references/digests. A PR/Git receipt is not rollout proof. No rollout verifier is
configured, so confirmed is refused rather than inferred from a supplied claim.
Do not block task settlement on missing deployment evidence.

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

Implementation delivery and quality review are distinct. A pre-execution
`task_role=quality_review` or `functional_tests` on a validation task requires
`review_target` bound to the original implementation/report/request/HEAD. An
optional quality review without an opinion does not require human approval to
save verified implementation memory/release its reservation. Mandatory functional
negatives, unknown role/scope, live work and incomplete history still block.
Product review runs in the official `Product review (exact HEAD)` CI check;
remote required checks/reviews and unresolved quality findings gate integration.
No provider refusal is waived and no redispatch is authorized by delivery.
Use the same task ID with diagnose-review/resume after the official runtime
upgrade; never retrofit roles or rewrite negative reports. See
`docs/AUTOMATED_REVIEW_ACCEPTANCE.md` for precise recovery limits.

For legacy delivery blocked by an unclassified interrupted native descendant,
use `task diagnose-delivery` from the real original origin. It validates canonical
reports and original operational receipts, returning eligibility and concrete
missing evidence. An empty implementation_checks before that gate is unknown,
not a test failure. With adequate proof, `task recover-delivery` appends an explicit
`post_execution_delivery_policy_v1` automated origin decision via a private digest-
bound artifact. See `docs/MANAGED_TASKS.md` for the exact contract. Never backfill
role/target metadata or claim a human/final-HEAD review. A host label or rationale
alone cannot waive functional/technical negatives. Reuse the same intent on lost
response; old daemons must advertise `legacy_delivery_recovery=true`. Memory and
fresh execution/evidence gates remain mandatory. Recovery does not authorize
provider redispatch, publication, merge or activation; pending auto-publication
is suspended and external lifecycle steps retain separate receipts.

An original legacy host proof can be the official stdio control stream rather
than a task_status JSON receipt. Preserve its bytes and digest. Only the strict
known stream grammar with matching canonical event history is supported; it is
explicitly historical control evidence, not a terminal response. Terminal
corroboration must be obtained now by the caller-bound service for the original
execution and labelled post-execution. Never extract a JSON substring, substitute
a fresh log for the old check, or claim a running launch receipt was terminal.
Neither transport grammar nor current terminality proves semantic operational
scope; original technical/functional negatives remain blocking.
