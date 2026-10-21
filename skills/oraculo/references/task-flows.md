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
    "checks": [{"command": "project test command", "status": "passed"}],
    "decisions": [],
    "limitations": []
  }
}
```

If the parent explicitly owns finish or the child is only native, return the
report natively and do not call managed finish for the parent's task. The origin
checks original run, descendants, checkout and validation before completion.
`validation_submitted` is only submission. Memory after work needs a correlated
confirmed write receipt and commit; pending or sent is insufficient.

For a planning-only report, add `deliverable_kind: "plan"` and
`approval_state: "pending"` inside `report`, plus document/commit and checks.
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
