# Operations

Pure queries: `oraculo status`, `doctor`, `--help`, `runtime status`,
`service status`, `skill inspect --provider <provider>`. No work child or memory
cycle is needed. Inspect source and observation time: loaded service is not online,
and local ledger availability is not a live daemon response.

For installation use `skill inspect|install|uninstall`. Select a single destination
from actual harness discovery evidence. A dry run shows files/hashes/conflicts;
installation never silently copies to every candidate path. Use the discovery record below; it is included with the installed skill.
The full acceptance guide is also available in the public repository at
https://github.com/marcoleloam/oraculo/blob/main/docs/SKILLS.md. Preserve local edits and unrelated skills. Codex metadata
in `agents/openai.yaml` is not a universal permission or hook contract.

For authorized runtime changes use `runtime install|upgrade|activate|rollback`
and `service start|stop|restart|uninstall`. Read the installed command's help and
maintenance result; use explicit roots/configuration for tests. Preparation does
not authorize starting the daemon. Activation needs quiescence, closed admission,
confirmed stop and version/socket probes. Unknown process/task state blocks;
never alter capacity or delete state to force an upgrade. Runtime rollback must
respect schema compatibility and preserve memory, receipts and user config.

Preserve the user's existing approval and use its scope. Ask only for a missing
decision that actually blocks the proposed action. Administrative mutations use
the operational memory cycle when required by the service/project contract;
queries and notifications do not. Keep mutation, confirmed memory, merge and
activation outcomes separate, and report recovery actions from actual receipts.

## Discovery evidence

Record an actual harness catalog observation in a private JSON file outside the
project. The following values are placeholders, not proof; replace them with the
observed provider instance, version, source and timestamp:

```json
{
  "schema_version": 1,
  "provider": "codex",
  "provider_instance_id": "observed-instance-id",
  "harness_version": "observed-version",
  "mechanism": "harness_catalog",
  "source": "/private/raw-discovery-evidence.json",
  "observed_at": "2026-01-01T00:00:00Z",
  "roots": ["/observed/skills/root"],
  "skill_sources": []
}
```

`roots` are directories the harness actually considers, without the `oraculo`
suffix. `skill_sources` must include every discovered Oraculo source, including
plugins and compatibility paths. Do not infer roots from an empty directory or
`--version`. Codex/Claude use a real catalog or observable discovery transcript;
Grok can use `grok inspect --json`, mechanism `grok_inspect`, and all matching
`skills[].source.path` entries. A fixture is not production discovery evidence.
Refresh observations for a different cwd, external worktree, version or session.

Pass the private record using `--evidence`, with the observed destination:

```sh
oraculo skill inspect --provider codex --root /observed/skills/root --evidence /private/discovery.json
oraculo skill install --provider codex --root /observed/skills/root --evidence /private/discovery.json --dry-run
```

Apply the approved installation without `--dry-run` only after reviewing the
files and conflicts. Preserve existing ownership and local edits. Claude and
Grok may discover other providers' paths through compatibility: never install a
second copy by guessing. Unknown discovery remains unavailable; a direct CLI
call may still work but does not certify native skill invocation. Separate
`loaded_skill`, `called_cli` and `confirmed_service` observations; a valid JSON
record does not prove any of these execution stages.
