<!-- oraculo:cooperative:v1 -->
For substantive project work managed by Oraculo, load the `oraculo` skill and use
its CLI flow. Preserve existing project instructions and the user's approvals
within their recorded scope; do not request the same approval again.
Inspect an existing task binding before `task start`. Status/questions/control
events do not create tasks; steering uses `task continue`, uncertain effects or
returned children use `task resume`. A delegated executor uses its assigned task
or returns natively; it never opens another task for itself.
Show actual task/workspace/return receipts in the origin. Report unavailable
bridge or memory explicitly. These instructions are cooperative, not a global
admission hook. Use the skill as the workflow source; do not duplicate its engine.
<!-- /oraculo:cooperative -->
