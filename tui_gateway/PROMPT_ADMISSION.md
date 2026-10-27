# Exact-session prompt admission (live-owner subset)

Consumer: user-steps decision delivery. This is an extension of the existing
TUI/Desktop JSON-RPC server, **not** `/v1/runs`, and does not advertise
`runs_exact_session_admission`. Connect to the authenticated RPC transport of the
runtime which already owns the Desktop session. Do not spawn a new backend, call
`session.resume`, or import `SessionDB` in the plugin to perform admission.
The existing transport authenticates callers; this adds no credential or listener.

## Request

`prompt.admit`:

```json
{
  "profile": "default",
  "stored_session_id": "EXACT_STORED_ID",
  "operation_id": "PERSISTED_OPERATION_ID",
  "text": "Frozen decision input",
  "payload_digest": "64 lowercase hex characters"
}
```

The digest is SHA-256 over UTF-8 of Python-equivalent:

```python
json.dumps({"stored_session_id": stored_id, "text": text},
           sort_keys=True, separators=(',', ':'), ensure_ascii=False)
```

Profile must be the canonical name, not an alias. IDs and text are not trimmed,
normalized or resolved through titles/compression tips. Input requiring the normal
user-input sanitizer to change it is rejected; sanitize before freezing the payload.
Operation IDs are unique within a profile's state.db and must never be recycled.
Changing the target or digest under the same operation ID is a conflict. There is no
TTL and no pruning of these operation tombstones in this implementation.

`prompt.admission.get` takes the same fields **except text**. It can read receipts
after the live session disappeared or after a runtime restart. Readback requires
both the receipt and its user message UID to remain in the exact target session.
Reinserted or inactive rows are valid; `user_row_id` is the original acceptance
row ID, informational only. A deleted/rewritten-away UID produces an error,
never permission to replay.

Both methods return:

```json
{
  "status": "accepted",
  "profile": "default",
  "stored_session_id": "EXACT_STORED_ID",
  "operation_id": "PERSISTED_OPERATION_ID",
  "payload_digest": "64 lowercase hex characters",
  "user_row_id": 123,
  "message_uid": "durable-message-uid",
  "execution_status": "unknown",
  "dispatch": "unknown"
}
```

`get` returns `status: absent` without row/UID when no operation exists.
Consumers must compare every identity field and require accepted + integer row ID
+ nonempty UID. Acceptance proves a committed user message, **not** model execution,
completion, or authorization beyond the submitted text. Unknown methods mean this
core version is not installed; do not fall back to ordinary submit or `/v1/runs`.

`dispatch: cancelled` is a terminal, positively unstarted admission: its user row
is retained for audit but atomically made inactive with the cancellation receipt,
BEFORE releasing the writer. It grants no authority to subsequent ordinary turns.
The scheduler callback is fenced against a late start; an exception after a worker
has started remains `unknown` and is never treated as cancellation. Receipt reads
serialize with scheduling, so they cannot expose an intermediate acceptance that
later gets cancelled. Legacy `failed` receipts do not prove row retirement.

If cancellation persistence fails, the live writer remains quarantined and RPC
returns an error, not successful delivery. Same-operation readback retries ONLY
the retirement write and releases the quarantine after commit, never schedules.
A process crash before this write commits still has the existing uncertain-crash
semantics below; no automatic replay or permission to assume cancellation.
Consumers may close a positively cancelled operation as **not delivered**, leaving
cards blocked and omitting passive approval context. A replacement request revision
requires fresh permission and a new operation; the cancelled key always remains
terminal.

## Ownership and crash semantics

- New admission requires one exact already-loaded, idle, inline owner in this
  process/profile. It refuses lazy/watch sessions, closing sessions, compression
  rotations, pending queued input/images, unpersisted branch seeds, hosted-group
  fences, compute-host sessions and foreign writer leases. It never takes over a
  detached owner, rebinds transport, creates a missing session, or cold-resumes.
- Resume/history locks protect only the in-memory running/in-flight claim; writer
  lease acquisition and SQLite work happen after releasing both. Same-operation
  retries serialize separately and re-read the receipt before claiming a turn.
  The SQLite transaction additionally checks transcript guards and reopens ended
  sessions using normal queue retirement, reset lineage and auto-unarchive rules.
- The operation receipt and staged canonical user row commit atomically in state.db.
  Only the transaction winner dispatches the existing build/turn runner. The normal
  submit-row adoption path prevents a second user row during agent persistence.
- A retry returns the original verified receipt and **never dispatches again**.
  A crash after commit but before thread start therefore leaves accepted input with
  uncertain execution. There is deliberately no automatic recovery worker. Crash
  auto-continuation is disabled for this admitted turn's marker. Manual reconciliation
  is required for uncertain execution; never invent a new operation ID to retry it.
- Error 4092 is a target/state/digest conflict or unsupported owner; 4090 is writer
  admission refusal; 5035 is retiring; normal validation/profile/storage errors also
  apply. After any lost response or exception, query the same operation first.

## Remaining integration limits

This does not locate or proxy to an owning Desktop backend from the separate
messaging/API gateway. The plugin must route to the existing authenticated owner RPC
transport. Cold/unloaded and busy-owner queue admission are deliberately unsupported;
busy requests can retry the same operation once the owner becomes idle. No completion
receipt, automatic execution reconciliation, deployment, or real user decision was
performed as part of this implementation.
The dispatch winner emits `message.admitted` on the existing owner transport before
model output. Desktop merges only that committed user row into the bound session,
checks connection/profile/stored-session identity, and deduplicates by row ID or
message UID without replacing live assistant output. Cancelled unstarted callbacks
emit no event. Reconnect continues to use canonical transcript hydration. Focused
Python transport/contract and Desktop event tests cover this seam; packaged live
acceptance remains a separate release gate.
