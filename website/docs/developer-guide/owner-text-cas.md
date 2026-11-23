# Owner-local text compare-and-swap

`hermes kanban --board default edit-text TASK_ID --request-file request.json`
provides an audited text-only edit for a trusted operator at the owning home.
It is not an authentication mechanism, worker escape hatch, release command,
or a remote courier operation. Worker and delegated-child invocations refuse.
The Python entry is `hermes_cli.kanban_text_edit.edit_text(conn, task_id, request)`.
No dashboard, REST, tool, or distributed delivery interface is added.

```json
{
  "request_id": "operator-request-1",
  "reason": "Replace a superseded future routing instruction",
  "expected": {"title": "Existing title", "body": "Exact existing body"},
  "replacement": {"title": "Existing title", "body": "Exact new body"}
}
```

Both title/body pairs are mandatory. No trimming or Unicode normalization occurs;
null body differs from empty body. Unknown keys, duplicate keys, nonfinite JSON,
invalid UTF-8, no-op requests, and invalid model-neutral replacement text refuse.
Limits in UTF-8 bytes: file 500000, title 4000, each body 100000, reason 2000.
Title/reason must be nonblank. Request IDs are 1–128 ASCII letters, digits,
underscores or hyphens. Expected text is evidence, not new policy input.

The existing edit transaction checks existence, strict primary-home identity,
inactive custody, prior request receipts, then both exact preimages. Allowed
states are todo, ready, blocked, scheduled and triage. Running/review/terminal
cards, any current/open run, claim fields, worker PID, or unexpired/unknown-expiry
lease (including an own-home lease) refuse. This does not clear stale custody.
Status, holds, dependencies, run history, admission and leases remain unchanged.

A successful edit emits only `text_cas_edited`, not the ordinary `edited` event:
a text correction must not reset no-progress or diagnostic/decomposition gates.
The payload contains before/after, request ID, reason and explicitly non-author
process context. Original history is not restamped. On an unsynced board only the
new receipt receives a stable UID, adding the existing nullable UID column in
the same transaction when needed. Audit failure rolls back text and schema.
Normal legacy edits retain their existing behavior.

The result contains `ok`, `disposition`, request identity and, for applied/replayed
requests, an event UID. Identical local receipt plus current replacement returns
`replayed` without writing or notifying. Changed request content returns
`request_id_conflict`; changed current text returns `replay_state_drift`.
Foreign-origin or malformed receipts cannot authorize replay. Missing receipt
plus stale text refuses; it never reconstructs history. Pre-receipt refusals
report `receipt_checked: false`. A storage/postcommit response failure returns
`unknown`: retain the exact request and read target/history before deciding
anything. Never mint a replacement identity or automatically resubmit an unknown
outcome. A committed local receipt does not prove sync convergence.

CLI exit codes: 0 applied/replayed, 2 invalid input or worker refusal, otherwise 1.
Publication and independent exact-head review of this interface do not authorize
application to production cards; that remains a separate scoped operator action.
