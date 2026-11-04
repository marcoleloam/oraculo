# T3 capability options compatibility

The host ACK validates the literal capability catalog without normalizing or
rewriting the response. Current T3 advertises `id`, `values` (strings/booleans)
and optional nullable `default`; legacy hosts use `type`, `options[{id}]` and
`currentValue`. The reader in `capability_options.py` supports both forms.

Nonempty, distinct choices and valid typed selections are required. Numeric
values are not booleans. Conflicting hybrid schemas and duplicate option IDs are
rejected. An explicit empty model `options` list remains valid. For a modern catalog identified by `inheritedOptions`, omitted or null model
`options` is preserved literally without inventing choices. In a legacy catalog,
omission is allowed only when both child-task capability flags are false, and
explicit null is rejected. Malformed non-null options always fail validation.
The router continues excluding disabled providers.

The service uses the same choices to send `serviceTier=default` and
`fastMode=false` only when the selected model advertises those values. It does
not infer options from provider names or copy a priority/fast default.

Reproduce locally without a T3 executor, API key, or shared daemon:

```sh
uv sync --frozen
uv run pytest tests/test_host_ack.py tests/test_service.py -q
```

The synthetic current-schema regression exercises CLI `bridge ack`, lossless
ACK decoding and the stdio receiving validator. Service tests use temporary
state/socket fixtures; T3 responses are simulated. These tests do not certify a
live managed launch, durable operational memory, publication, or deployment.

Source changes do not upgrade an installed CLI or an already running service.
Deployment requires the normal authorized maintenance and quiescence gates.
After deployment the original caller must reconcile its original request key
before retrying; an old frame/ACK must never be refreshed or reused as a new host
observation. An independently unhealthy service remains a separate blocker.
