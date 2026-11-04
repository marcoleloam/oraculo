# CI scope after the compatibility repair

The temporary focused compatibility scope from PR30 is historical. PR31 restores
complete automatic suites on pull requests and main pushes, plus the orchestration
offline gate and exact-HEAD product review. Focused compatibility tests remain in
the complete suite. No passing focused run certifies the broader product.

The Intel macOS runner pool, stable Tests check names, verbose test progress and
120-minute macOS budget from PR30 are retained. Ubuntu retains its 30-minute job.
Product evaluation retains a 40-minute subprocess budget within its 45-minute job.
Previous cancellations and timeout failures remain historical negative evidence.

All four checks are required by the PR31 managed merge request, independently of
whether remote branch protection requires a smaller subset. This document does
not change GitHub rules or claim that remote required contexts were restored.

Runtime activation still requires official maintenance, identity, health and fresh
quiescence gates. CI results do not change capacity, reservations or past outcomes.
