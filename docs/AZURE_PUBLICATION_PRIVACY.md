# Azure publication without internal public metadata

New Azure PR creation sends the caller's product title and description unchanged.
It does not append a task marker, task digest, source-project identifier or
credential reference. Existing publication specifications and receipts remain
private. Callers still own sanitization of the supplied product text and Git
history; this change does not claim to sanitize arbitrary user content.

## Correlation and uncertain effects

The managed owner already serializes each task and persists the exact publication
specification and emission intent before POST. The adapter observes the remote
repository GUIDs, source and target refs and exact source commit. Discovery
requires a unique PR across all states; it reads the full PR and validates those
identities before returning its canonical URL and number. The returned receipt
remains bound to the original task's source project and validated commit.

These identity checks already admitted externally created PRs with no internal
marker. Removing automatic marker creation does not remove any identity check.
The legacy conflict check remains: discovery of an older PR with a different
task marker is rejected. Explicit existing-PR association retains its existing
integration/ancestry rules.

After uncertain creation, the owner's persisted `publication_attempted` forces
`reconcile_only`. No visible PR means uncertainty, not permission to POST again.
A changed publication specification is rejected. Multiple candidates, different
repository/ref/SHA or invalid host response fail closed. The existing callback
must complete before POST; failure persists no external effect.

No ledger schema, ACK, managed state transition, execution/memory gate, review,
CI, merge or settlement rule changes. The implementation changes only outbound
Azure description construction and its explanatory comment. GitHub publication
is outside this patch's scope.

## Verification boundary

`test_azure_publication_privacy.py` checks exact body bytes, callback ordering,
lost response without duplicate POST, invisible/ambiguous/stale/wrong-identity
reconciliation, and old-marker compatibility/conflict. The new managed test in
`test_azure_task_closure.py` exercises the real isolated store and memory service:
intent is observable before POST, invisible replay never re-creates, a fresh
adapter recovers the unmarked PR once visible, the original report is unchanged,
and a changed intent remains rejected. REST and T3 are synthetic in these tests.

This is per-task replay protection, not global exactly-once creation across
independent publishers. Concurrent external publishers must be coordinated;
ambiguous results block rather than selecting a PR arbitrarily. Tests do not
certify a live Azure create, review, merge, native tracking or runtime activation.
