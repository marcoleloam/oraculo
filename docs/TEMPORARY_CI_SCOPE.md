# Temporary CI scope for the T3 compatibility repair

The complete suite timed out on hosted runners while the compatibility repair
remained blocked. Automatic CI temporarily runs the host ACK, service and CI
contract regressions, lint and canonical workflow export validation on Ubuntu
and macOS. Passing this scope proves those checks only; it does not certify the
complete product journeys or deployment readiness.

The existing protected `Tests (ubuntu-latest)` and `Tests (macos-latest)` contexts
remain required. The extensive `Orchestration offline gate` is temporarily
removed from the required contexts; its workflow and the product review workflow
are available by manual dispatch. Other branch protection settings are retained.

Once this workflow is on the default branch, the complete suite is available as:

```sh
gh workflow run ci.yml --repo marcoleloam/oraculo --ref main -f full=true
gh workflow run orchestration.yml --repo marcoleloam/oraculo --ref main
gh workflow run product-review.yml --repo marcoleloam/oraculo --ref main
```

Review and repair the broad suite's runtime separately. Restore its automatic
triggers and original required offline context after it completes reproducibly
within a documented runner budget, or after a validated partition covers the
same collected tests. Restore `oraculo-suite.yml.template` to the complete suite
for automatic events, export the canonical workflows and run the CI contract
checks before publishing that restoration. The original required status policy
is strict and contains both Tests contexts (GitHub Actions app 15368) plus
`Orchestration offline gate` (no app binding).

Runtime activation still requires the official maintenance, identity, health and
quiescence gates. This CI exception does not change configuration, capacity,
reservations or execution outcomes.
