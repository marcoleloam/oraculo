# Explicit Azure Keychain envelope

This candidate extends the raw-PAT Keychain reference with an opt-in format:

```json
{
  "keychain_service": "existing-item",
  "keychain_account": "operator@example.test",
  "format": "pat-envelope-v1",
  "organization": "example",
  "scopes": ["vso.build", "vso.code_write"]
}
```

The Keychain value must be a JSON object with exactly `token`,
`authorization_id` (GUID), `owner`, `organization`, `scope` (space separated),
and `valid_to` (future ISO timestamp with timezone). Duplicate keys, unknown
fields, missing fields, duplicate/unexpected scopes and invalid metadata fail
closed. Owner must equal the explicit account and organization must equal the
reference. The selected Git remote's organization is checked before reading
the item. Reference validation also runs before persistence of publication intent.

The old two-field reference still reads a raw PAT; it never guesses JSON format.
No Keychain write, namespace discovery, parent `.env` search or secret in the
task payload is introduced. Only the token reaches the existing in-memory HTTP
transport. Expiry is checked when constructing the client; this patch does not
add token renewal or extend the existing transport's lifetime checks.

This patch requires the Azure Repos publisher and does not activate a release or
change managed completion, ACK, publication, review or merge gates. Upgrade
requires the normal quiescent maintenance workflow.

The subsequent privacy patch preserves the product description exactly and
keeps correlation in the existing managed intent/receipt. See
[Azure publication privacy](AZURE_PUBLICATION_PRIVACY.md) for tests and limits.
Both patches require review and maintenance before activation. An existing-PR
observation mode is not permission to bypass the authorized publishing path.
