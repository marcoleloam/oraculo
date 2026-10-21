---
name: oraculo
description: Manage substantive project work through the Oraculo CLI with durable memory and return to the originating conversation; inspect, continue or recover existing tasks and operate Oraculo when requested. Simple questions, status checks, control events and already delegated work never create a new task.
---

# Oraculo

Use the installed `oraculo` CLI. The service owns transitions, deduplication,
memory, capacity and publication gates; this skill only selects the entrypoint.
Existing explicit authorization remains valid within its scope. Do not ask again
for approved work, and do not turn approval of a plan into permission for actions
outside that approval. Keep the originating conversation responsible for follow-up.

Choose the flow before calling a tool:

- **New substantive request:** check the current request's existing binding with
  `oraculo task list` / `task status`. Resolve actual cwd/project; retain one stable
  message key. Read [task flows](references/task-flows.md), then call `task start`
  once. A failed print or uncertain external effect requires reconciliation of
  that same ID/key, not a new child. A delegated executor uses its assigned ID;
  a native child without a managed ID returns natively to its parent.
- **Question or status:** answer directly, or query `task status/list`, `status`,
  `doctor` or `--help`. Do not call `task start` or begin a memory cycle for a pure
  query. `ORACULO_CONTROL_V1`, memory reports and return notifications are control
  information, never new user requests.
- **Continue active work:** `task continue` with the existing ID, stable steering
  key and relevant message. **Recover an uncertain effect or returned child:**
  `task resume` with the original ID. Consult current status before presenting an
  old notification; an older event cannot replace a newer snapshot.
- **Finish planning or implementation:** read [task flows](references/task-flows.md).
  Planning reports stay awaiting approval. Implementation requires validation and
  confirmed memory; submission by an active executor does not certify its own run
  as terminal. Preserve the references to any approval already granted.
- **Operate the installation/service:** read [operations](references/operations.md).
  Use authorized administrative commands directly, without a gratuitous work task.

For a mutating managed command use the real caller-bound bridge described in
[bridge and evidence](references/bridge-and-evidence.md). If CLI, bridge, memory or
service is unavailable, report the observed blocker and recovery action. Never
replace an unavailable memory read with an empty one or bypass admission by
launching a child yourself.

Present the task title, project, current state, real executor link and workspace
from the receipt. Separate `loaded_skill`, `called_cli` and `confirmed_service`;
unobserved layers stay `not_observed`. A skill file, an agent's claim, CLI exit 0,
and a service receipt prove different things. Do not announce provider support or
global enforcement based on discovery alone.
