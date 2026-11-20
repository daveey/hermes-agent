# Audited diagnostic prose correction

`hermes kanban --board <board> correct-diagnostics <task-id> --request-file <path>`
quotes one exact diagnostic span in a task body and one comment. This is a narrow
trusted-operator maintenance command, not a delivery receipt or origin waiver.
It does not change origin detection, acceptance criteria, task status, dependencies,
comment identity, comment author, timestamps, or notification bookkeeping.

## Authority and limits

Only the actual independently authorized operator may initiate this command.
Worker-marked calls and delegated-child mutations are refused. Absence of worker
markers is **not authentication**: the recorded `process_context` describes the
process, not certified human or author identity. Keep genuine operator invocation
evidence separately. A worker must not launch a clean remote shell to impersonate
the operator. A relayed permission or supplied reason does not confer machine authority.

Before applying, independently establish that both selected spans reproduce a
diagnostic and do not specify genuine delivery destinations. Quotation is not
semantic proof. Preserve all genuine origin obligations. This command must never
be used to hide an unmet obligation. The planner inserts only double quotes and
rejects ambiguous/missing spans, embedded double quotes, multiline spans, and
spans reaching into an `Acceptance:` suffix. Other acceptance formats still need
operator review; no text heuristic can establish their meaning.

The task must be inactive and open. A synced board must be primary on the card's
owning gateway. The comment must have an existing portable UID and belong to that
exact task. Run on an existing initialized board with the sync schema. The command
skips explicit board initialization, but opening its connection still runs normal
schema setup and may create a missing database. A board without the sync schema
is refused with a SQLite error (for example, `no such column: uid`); this is not a
read-only missing-board probe. There is no force or caller-supplied authority flag.

## Request

Supply a UTF-8 JSON object with exactly these seven fields:

```json
{
  "comment_uid": "existing-comment-uid",
  "expected_body": "Diagnostic error\nAcceptance: unchanged.",
  "expected_comment": "Diagnostic error",
  "body_diagnostic": "error",
  "comment_diagnostic": "error",
  "reason": "Explain the independently verified diagnostic provenance.",
  "request_id": "unique-correction-id"
}
```

Expected strings must match current content byte-for-byte. Field limits in UTF-8
bytes are: body 100000, comment 50000, each diagnostic 2000, reason 2000, request ID
128, comment UID 201; the request file is at most 200000 bytes. Inputs must be
nonempty strings. The task ID is limited to 64 bytes.

The local transaction compares both originals before updating either. It appends
a `diagnostic_corrected` event preserving before/after content, reason, request ID,
comment UID, and observed process context. Existing historical events are retained.
An exact retry returns that audit without writing again; changed request content
or drift since application is refused. A malformed correction audit fails closed.
The result on stdout is the audit JSON, **not** proof of canonical convergence or
genuine operator identity.

## Required verification

Local transaction atomicity is not distributed atomicity. Normal row replication
carries the body, same-UID comment, and audit independently. Read back the exact
body, comment UID/content, and audit on the owner, canonical Board, and relevant
replica after synchronization. Investigate partial acceptance or concurrent edits;
do not manufacture a receipt or report success from local return alone.

Compare the entire original acceptance and every genuine destination. Then run the
installed read-only origin checker freshly, and retain its result alongside the
genuine operator invocation evidence. No proof rerun, lifecycle transition, send,
adoption, or gate exemption is implied by applying this correction.

There is no destructive audit rollback. Any further correction needs a separately
reviewed supported edit retaining the existing audit history.
