# Explicit administrative actionability recovery

`hermes kanban --board <board> record-actionability` records an operator's actual
administrative obligation. It does **not** promote, unblock, resume, retry, satisfy
a dependency, approve spend, clear a safety refusal, or attest live eligibility.
The unchanged `stranded` report remains a diagnostic, not an admission gate.

## Supported custody

Run on the task's primary home board, using that home's authenticated gateway.
The command reads the complete canonical execution inventory and all canonical
link pages, then each parent inventory, at one unchanged Board head. It refuses
incomplete, changed, missing, foreign-home and unbounded observations. It does
not infer canonical event order/version from replica IDs.

Supported accountable owners are the exact assignee or a canonically linked,
raw-nonterminal parent (never the task itself). Chat ownership and private
stop/renewal attestation are not guessed: any stop/restriction/renewal history
currently makes this entry unavailable, including historical covered stops.
This conservative boundary deliberately excludes the original refused
investigation. Use its existing authorized decision mechanism, never this entry
as release permission. Records without a real lifecycle source also remain
unavailable; there is no synthetic legacy first-observed clock.

## Preview, then digest confirmation

Prepare a JSON object with exactly these fields, using **actual recorded** facts:

- `task_id`: the exact task identifier
- `owner`: `{"kind":"assignee","refs":["<exact assignee>"]}` or
  `{"kind":"task","refs":["<linked open parent>"]}`
- `action`: an existing diagnostic action, e.g. `repair_lifecycle_contract`
- `instruction`: the next administrative action, not permission to run held work
- `prerequisite`: the actual blocker
- `trigger`: the actual event/action that resolves or reconsiders it
- `evidence`: recorded evidence references, at most eight
- `deadline_at`: an explicitly agreed fixed Unix-seconds deadline

The deadline must follow the real source clock and be within the canonical
14-day upper bound. It may already be overdue when supported by an actual
recorded obligation. Never backdate a historical commitment or manufacture
`now + TTL` merely to remove a violation.

```
hermes kanban --board <board> record-actionability --operation preview --request-file obligation.json
```

Preview freezes all request bytes in local custody and returns `op_id`,
`request_sha256` and the complete request. It does not put a command in the
outbox. Repeating the same preview returns the same bytes; a different deadline,
instruction or evidence under the same semantic identity is a conflict, not a
renewal. There is intentionally no discard/reset of frozen identities. Do not
change a trigger or owner merely to evade an immutable commitment or rejection.

After inspecting the full request, create a confirmation JSON object containing
**only** the returned `op_id` and `request_sha256`:

```
hermes kanban --board <board> record-actionability --operation apply --request-file confirmation.json
```

Before first issuance, apply independently rereads canonical premises and compares
them with the frozen request. A changed premise marks the preview stale without
issuing it; it does not replace its bytes. Reservation and the preview's reserved
state commit together. The existing sync sender may then deliver the command.
The command also obtains an independently verified canonical receipt; an ACK
alone is not confirmation. Canonical ingress performs the final atomic source,
owner, clock and dependency CAS, including races after preflight.

After reply loss, interruption or restart, repeat **apply with the original
confirmation**. Reserved requests replay their exact identity/bytes rather than
rebuilding an obligation. A definitive canonical rejection remains rejected;
canonical `deferred` prerequisites and storage failures remain distinct from
that rejection. Comments and restarts never renew the deadline.

## Overdue administrative escalation

Only after root confirmation and the original canonical deadline:

```
hermes kanban --board <board> record-actionability --operation escalate --request-file confirmation.json
```

This explicit action reuses the root payload verbatim, verifies canonical root
custody and the original clock before reservation, and records one linked
administrative notice/receipt. There is no recurring sweeper and no orphan
escalation queued before root confirmation. `recorded` proves notice emission,
**not** receipt by a person or execution authority. `route_unavailable` remains
an overdue residual requiring actual owner recovery.

## Diagnostic composition and limits

The reader fills only absent diagnostic deadlines from confirmed matching home
custody and selects the earliest fixed matching obligation without renewal.
Existing source deadlines, logical contract identity, action, blocker, release
policy and UNKNOWN eligibility remain source-derived. A missing owner can be
filled only by confirmed matching still-linked open-parent custody; conflicting
confirmed owners remain unresolved. A recorded escalation clears only the
overdue diagnostic, only for the matching deadline and current owner, with exact
confirmed request/event/notice linkage. Damaged or ACK-only custody cannot mask
an overdue obligation. Foreign replicas without local confirmation stay unknown.

This entry is not evidence of a zero-stranded production result. Shipping,
installed ancestry, complete unchanged-report pagination, actual per-row
obligations and independent QA are separate acceptance steps. Unsupported source
mapping/authority contexts and null historical deadlines must remain explicit
residuals with an executable owner, not invented metadata.
