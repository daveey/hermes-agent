---
title: "Kanban: recovering a card with a reserved idempotency key"
description: "Correct a local card whose pickup-stall:/worker-resources-unknown: key the Board refused, with `hermes kanban sync reserved-key`"
---

# Recovering a card with a reserved idempotency key

The Board fences idempotency keys that start with `pickup-stall:` or `worker-resources-unknown:` to the pickup-stall
detector. An ordinary local card that carries such a key is refused whole, every op behind it (comments, runs, events,
links, chat links) is refused `not_found`, and the rejected ops stay visible under `hermes kanban sync status`.
New creates with a reserved key from any other creator now fail immediately. `hermes kanban sync reserved-key`
repairs cards that already exist, one card at a time.

```bash
# 1. Preview (read-only). Prints eligibility, the exact rejected ops it will supersede, and a token.
hermes kanban sync reserved-key TASK_ID --new-key my-new-key --reason "why this card is corrected"

# 2. Apply with the preview's token (compare-and-set).
hermes kanban sync reserved-key TASK_ID --new-key my-new-key --reason "why this card is corrected" \
    --apply --expect TOKEN
```

`--reason` is required and must not be blank. It is bound into the token and stored in the receipt.
Add `--json` for machine output. Re-running the same apply is safe: it continues the receipt.

## What is checked

- The card is local and never acknowledged by the Board (`created_on` and `sync_version` empty), is not a detector
  card, carries a reserved key, has no run, claim, lease or worker, and every outbox row for it and its children is a
  **rejected** upsert/import for an identity that travels with the card. Pending ops, tombstones, rejected
  releases/deletes, rejections for other identities, links where the card is the *parent* and events older than the
  Board's retention make the command **refuse** instead of silently dropping them.
- The hub's enrolled gateway is present and equals this board file's sync gateway, and the file's board id equals
  the board. The token also binds a nonsecret fingerprint of the Board endpoint and gateway name; it never contains
  the hub key. Every later step re-validates it.
- Fresh authenticated Board reads: `never_existed` history evidence for the card and **every** child row, and an
  exhaustive paged scan of the Board's live cards showing nobody holds the replacement key. A legacy Board without
  the routes, an unreachable one, an incomplete scan or a pruned history floor refuse. The whole read interval, up
  to the in-transaction veto and including the wait for the writer lock, must stay within 30 seconds on the
  monotonic clock, or nothing is written.

## What apply writes

One transaction inserts the receipt (reason, endpoint fingerprint, exact bytes of every superseded rejected op),
rewrites only `tasks.idempotency_key`, and enqueues one full `import` op for the corrected card. Apply does not edit,
reset or delete rejected ops. Original event payloads and audit rows remain unchanged; all original ids are preserved.
The normal selective resolver may later retire exactly the proven superseded outbox entries, retaining their complete
original bytes in the receipt ledger.

Each sync round then advances the receipt: once the Board accepted the corrected card, the child rows are queued as
ordinary upserts (`children_queued`, with the replacement op ids recorded). The receipt becomes `complete` only when
every child op was delivered (not merely queued) and the Board holds each child row. A child deleted or tombstoned
locally, or carrying competing pending work, holds the whole child manifest instead of completing a partial recovery.
Recovery child ops never synthesize another task from an incoming link. Normal transient retries retain their
replacement-operation linkage. A Board that refuses a recovery field as `forbidden_field` is explicitly unsupported:
the exact replacement remains rejected and visible, without compatibility downgrade or a false completion. Inspect
that refusal and upgrade/reconcile the service through its supported process; do not reset the rejected operation.

## How the old rejections retire

Only through a `complete` receipt, and only for the exact recorded op ids with unchanged bytes. The original task op
retires when the Board's card is the corrected card created by this gateway and every other rejected wire field matches
the Board and the local card (the key is compared as the replacement key). Child ops go through the normal
field-equality rules with the corrected parent as proof. If a receipt names an op but cannot vouch for it, the op stays
rejected; it never falls back to a looser rule.

Implementation and contract: `hermes_cli/kanban_sync_reserved_key.py`.
