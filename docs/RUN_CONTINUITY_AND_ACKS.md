# Terminal continuations and literal host ACKs

This change accepts proven continuations of a **native child in its original
thread**, rather than adding a rebind command. Native `childRunId` names the first
delegated run; T3 deliberately keeps it after later runs. Overwriting it would
lose the original execution identity. The task instead stores `completion_evidence`
with original/latest IDs, the complete ordered runs, report digest and submission
provenance. The original child receipt and submitted report remain immutable.

## Completion policy

`runCount` must equal the number of unique `recentRuns`; ordinals must be exactly
1..N, all terminal, and the latest run/status must match the thread. Multi-run
history requires chronological, nonoverlapping start/end timestamps. The original
run must be present and remain the first. Missing counts, gaps, duplicate IDs,
unknown states, truncated history or contradictory native task status block.

The root must have no `activeRunId`, zero `pendingRequestCount`, native
`hasPendingChildRuns=false`, `workState=result_available`, and matching task/thread/
project/parent identities. The complete project catalog is paginated and every
visible descendant is read. Successful work still requires original and latest
runs completed, successful descendants, a positive valid report, and the bound
clean checkout/commit/base. Negative work stays negative.

A new executor submission records its actual caller thread/run. For legacy
submissions, the immutable `validation_submitted` event's report digest and
service timestamp must place submission during the last run or after it; the
preexisting caller-authorization path is the provenance boundary. A report from
before that run is rejected. A malformed report may use the existing audited
`recover-report`, preserving the original event/digest and all negative evidence;
recovery does not refresh the old report's date. A fresh origin-owned first
submission can bind the observed final run; existing reports cannot be relabeled.

After memory is saved/confirmed, the engine reads terminal, native and descendant
proof again, compares the exact proven chain/report, and checks the same final
snapshot's checkout before releasing admission. A change keeps the reservation
and confirmed receipt; retry does not rewrite memory. These observations remain
sequential, not a distributed lock. Use a cooperative window without new runs.

T3 exposes `runLimit` but no run-history cursor. This implementation requests
50 recent runs. More than 50, a missing ordinal, or a host omitting the complete
history blocks with `terminal_run_chain_incomplete`; timeline `hasMore` is **not**
a run-history pagination flag. No invented pagination or timestamp synthesis is
allowed. Descendant coverage remains the host-visible project, bounded to 1,000
catalog threads and 100 descendants by the existing snapshot collector.

## ACK contract and minimum fields

The call marker stays `ORACULO_HOST_CALL_V1`; calls advertise
`ack_protocol=ORACULO_HOST_ACK_V2`. Old handwritten `{request_id,result}` ACKs are
refused by this CLI. The daemon socket protocol remains version 1.

Minimum fields are enforced by `host_ack.HostResults`:

- Capabilities: caller/inherited provider/model, permission/interaction modes,
  features, providers, child permissions, constraints, models and **all model
  options**, including option IDs/types and selectable IDs. Labels/defaults,
  current values, injected options and unknown future fields are retained too.
- Thread reads: identity/parent/project/workspace/branch, active/latest/status,
  pending count, run/item counts, revision, archive/settled state, run IDs/
  ordinals/status/times, items, and pagination metadata.
- Thread lists: project, total, rows (identity/parent/status/latest/item count/
  revision), and cursor. No omitted pages or reused cursor/count inconsistencies.
- Project lists: IDs, workspace roots and cursor.
- Task status: original task/thread/run, status/work state, pending child/question
  flags, latest terminal ID/status. Large summaries are preserved literally.
- Outbox sends: returned thread and message ID must match the request.

Readers already use one timeline item and one character; identity reads request
one run, closure reads 50 to prove completeness. The real host catalog has no
compact capabilities/status/list projection parameter. Do not invent one or
manually delete response fields. The helper compresses the **entire** captured
JSON response with zlib/base64 and verifies its SHA-256 on decode. Duplicate MCP
text/structured representations must agree. IDs, arguments and frame timestamp
are correlated; the receiver also rejects regressing revisions/counts, changed
terminal runs, changed bindings and inconsistent known task/thread observations.

### Programmatic relay

Prefer `CommandTransport` through the host-provided `T3_ACP_MCP_NODE` and optional
`T3_ACP_MCP_ENTRYPOINT`. Do not set those to guessed executables. No ACP transport
was exposed to this implementation session; the native T3 tools were available.

For a native harness without ACP, retain the interactive process's stdin:

```sh
oraculo --config CONFIG task resume --file TASK.json --stdio-host
```

For **each** complete call frame, save it to a private frame file; call the named
native tool with exactly `arguments`; serialize that returned object directly to
a new result file, without having the model reconstruct its fields. Then run:

```sh
oraculo bridge ack --frame /private/current-frame.json \
  --from-file /private/current-result.json > /private/current-ack.json
```

Feed the resulting line to that process's stdin programmatically. In a harness
with JavaScript tool orchestration, keep the returned object in a variable and
use `JSON.stringify(actualToolResult)` with its file-writing tool; likewise pass
the helper's actual stdout variable to the stdin-writing tool. Do not copy a
visible summary or stale tool-call result. The helper refuses a result file
whose mtime predates the frame. Keep files and receipts private. Failure leaves
the current operation uncertain; inspect status and retry the same intent.

Hashes, schema and consistency detect corruption and certain edits/reuse, not
all self-consistent fabrication by the local operator. A copied result with
rewritten metadata can evade them. This is not host attestation. Actual direct
caller-bound transport is the stronger provenance path.

## Reproducible byte measurement and E2E boundary

Run from the candidate source or the installed wheel test environment:

```sh
ORACULO_ACK_MEASUREMENT=/tmp/ack-bytes uv run pytest -q -s \
  tests/test_run_continuity.py -k e2e
```

The test executes a real disposable CLI + daemon/socket + Git + memory + admission
resume to `succeeded/pr_pending`, confirmed memory and released reservation. T3
is an explicit fixture. Every ACK is produced by a separate invocation of the
actual `bridge ack --from-file` command. The same recorded response trace is
serialized with the prior V1 encoder and the actual V2 encoder, counting UTF-8
call-frame plus ACK bytes (including newlines). This measures encoding for an
identical resume trace, **not** a comparison of two production releases' complete
schedules or a production latency claim.

Observed in this candidate's source run (22 frames in each scenario):

- Minimal fixture: 22,347 bytes V1, 25,161 bytes V2: **12.59% increase** due to
  correlation metadata on small replies.
- Full fixture (24 unrelated catalog threads, 32 models with options, large
  status summary): 119,463 bytes V1, 27,018 bytes V2: **77.38% reduction**.

Paths, timestamps and generated IDs can change counts slightly; the test records
per-tool bytes and totals in `.0.json` / `.24.json`. The large scenario is synthetic
and deliberately declared as such, not an operational incident snapshot. The
small-message overhead is a real tradeoff, not a claimed universal saving.

Read-only calls to actual T3 capabilities and the current executor thread also
passed the candidate stdio/file-helper protocol, preserving actual model options.
That is a real transport receipt, **not a real managed resume receipt**. Live
resume/publication/activation and the affected origin's recovery remain pending.

## Publication, activation and origin recovery runbook

The parent owns publication/activation and this executor's finish. Export only
selected generic files to the safe public checkout; never push private history,
context artifacts, task payloads or live receipts. Review the candidate commit,
run source/wheel checks, and prepare the hash-sealed wheelhouse using
[installation instructions](INSTALACAO.md). The wheel contains the updated skill
and prompts; no global skill was modified by this task.

The parent prepares and activates through the official commands only:

```sh
oraculo runtime upgrade --bundle REVIEWED_BUNDLE --dry-run
oraculo runtime upgrade --bundle REVIEWED_BUNDLE
oraculo runtime activate REVIEWED_RELEASE --dry-run
oraculo runtime activate REVIEWED_RELEASE
oraculo runtime status
oraculo service status
oraculo status
```

Activation still requires official maintenance/quiescence. If outstanding affected
reservations prevent activation, **stop at that observed gate** and return its
receipt to the parent. This patch supplies no pre-upgrade bypass, live SQL edit,
force-release or bootstrap relaxation. The parent must resolve that deployment
prerequisite through an authorized supported path; installing only the new CLI
cannot change the old daemon's completion policy.

After activation receipts identify the reviewed release/PID/config/protocol,
install the packaged skill using the existing observed discovery/root binding
(see [skill installation](SKILLS.md)), first with `--dry-run`, then the same
`skill install` command. Do not copy over global skills manually. Existing
sessions need the new literal-ACK guidance before using the new CLI.

Only the **original task origin** performs affected resumes. For each existing ID,
write `{"task_id":"EXISTING_ID"}` into a private payload file using a structured
writer and execute, one at a time:

```sh
oraculo --config CONFIG task status --file TASK.json
oraculo --config CONFIG task resume --file TASK.json --stdio-host
oraculo --config CONFIG task status --file TASK.json
oraculo --config CONFIG task history --file TASK.json
```

Or omit `--stdio-host` when the actual caller-bound ACP transport is present.
Process all V2 frames literally as above; never resume the child thread to obtain
new proof, replace `childRunId`, edit run times, duplicate tasks or submit an old
report again. A complete three-run chain and correctly timed `validation_submitted`
report are enough; no explicit rebind command is needed.

Retain a real receipt for each task: same ID/original child binding, ordered
completion evidence ending at run 3, positive valid report/clean commit,
`succeeded`, `pr_pending` (or the authorized next publication phase), memory
`saved` with `write_receipt.state=confirmed` and matching operation/commit,
`admission.state=not_reserved`, and pending outbox delivery state. A nonzero exit,
incomplete history, live descendant, stale report, busy adapter or uncertain ACK
is a blocker with the existing ID; it never authorizes a fabricated receipt.
No operational recovery, publication or activation is claimed by this runbook.
