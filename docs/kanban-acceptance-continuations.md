# Required acceptance continuations

Lane gates continue to hold unsolicited improvements. Finishing a parent, matching its title,
or writing a completion receipt does **not** authorize a child. A required continuation has a
separate explicit executor attestation, accepted only during the parent's owned, genuinely
claimed completion. This attributes the necessity decision to that executor; it is not a
classifier that claims to establish semantic necessity from prose.

## Producer

New `created` events contain the writer-generated `source_body_sha256` of the original body.
The completing executor may supply this optional metadata:

```json
{
  "acceptance_continuations": {
    "version": 1,
    "source_body_sha256": "<copy the original created event's body digest>",
    "items": [{"card": "<created child id>", "criterion": "<verbatim original acceptance criterion>"}]
  }
}
```

The parent body must still match its original creation digest. The criterion must be a literal
nonblank excerpt, at least 16 characters, of that body. The executor explicitly attests that the
child is REQUIRED to finish that original criterion, not an optional improvement. A child can
have been created while the parent was running; it is reevaluated after genuine completion.
Up to 16 distinct children are allowed. An invalid declaration refuses the whole completion
transaction, without ending its run or recording partial authority. Omit the declaration when
completing work that has no eligible required continuation.

The canonical completed event stores the producer-generated run UID, child-created event UID,
and child scope fingerprint. Editing run metadata cannot add or change an attestation.
`completion_receipt`, `verified_cards`, `followup_card`, renderer metadata and done-parent
status remain insufficient. Historical creation without the original-body digest is not
backfilled. Missing portable run/event identities defer; local-only portable-identity bootstrap
is separate from admission. No PR is required: local-only acceptance work uses the same protocol.

## Existing planner

`hermes kanban lanes held --json` adds `continuation` to each held card. It provides
`eligible`, `reason`, `next_actor`, and the source completion identity. The projection is
advisory: apply repeats the checks under its write transaction.

Extend the **existing** lane planner's prompt (do not add a watcher) with this rule:

> Before ordinary breakage/cross-lane admissions, inspect each held card's `continuation`.
> When and only when `continuation.eligible` is true, propose `admit_continuation`, oldest
> first, citing its source parent/completion. Missing/false evidence is not permission to
> infer a continuation from title, prose, age or done-parent status. Retain the returned
> next-actor/reason for deferred continuations; do not create a human ask. Apply one plan once.
> Ordinary improvements stay held. The existing budgets, pauses, never-admit policy and
> independent restrictions remain binding; never fall back to manual admit after refusal.

The action is part of the existing public budgeted apply path:

```json
[{"action":"admit_continuation","card":"<child>","reason":"required original acceptance from <parent>"}]
```

No existing planner may be switched to this action until its installed core exposes the typed
candidate projection. Older cores safely lack that evidence and should leave those cards held.
The change does not alter shares, daily limits, pause states, user-only permissions or scope.

## Revalidation and deferral

Apply requires the authenticated creator dependency, genuine completed parent run and canonical
completion event, unchanged parent body and child scope, satisfied prerequisites, a never-run
child held solely at the lane gate, and no independent ask, containment/quarantine, refusal or
scope/safety/spend restriction. Creation namespace is not execution ownership: synchronized
pool execution can attest hub-created cards on the same board. No cross-board authority is
inferred or fetched. Missing board evidence defers to the existing owner.

The existing lane budget/daily dollar and card limits and never-admit policy apply; budget
usage is read again under the admission lock. Exactly one ordinary budgeted admission event is
appended, carrying the source completion UID in its reason. Retrying after restart cannot admit
the same child again. No original request, creation, completion or admission record is rewritten.

## Verification

Run `./scripts/run_tests.sh tests/hermes_cli/test_kanban_acceptance_continuations.py`.
This exercises the real completion producer and `lanes held` -> public `lanes apply` CLI,
renderer-shaped required verification, pre-completion reevaluation, restart idempotence,
budget exhaustion/stale plan state, unsolicited siblings, forged and missing evidence,
changed scope, independent holds and malformed-history deferral.

Deployment acceptance additionally needs the naturally installed revision, the existing
planner's actual prompt, and a controlled hidden side-effect-free eligible fixture plus a
negative control with cleanup readbacks. Source tests or a successful deployment command alone
are not that live proof.
