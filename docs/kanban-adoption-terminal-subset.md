# Explicit adoption authority and rejected-retain terminal settlement

This is subset 1 of the hold-decision recovery work. It does not implement hold
notices, terminal-run reservations, parent-held signals, or authority-less-create
policy. The larger recovery-source PR remains separate and open.

## Changes and boundaries

- Authenticated interactive `origin-adopt --local` records an unreleased created
  chat link and `creator_chats_resolved` provenance transactionally after existing
  conflict checks. Ordinary reconciliation preserves the adopted profile's link.
  Observer subscriptions, worker sessions, foreign cards, conflicting owners and
  released ownership do not gain authority. Task session anchors are not changed.
- The read-only `kanban_adoption_authority` dependency contains only gateway and
  released-upstream readers; no creation ingress or authority-less-create rule is
  extracted from the recovery source.
- `renew-scope --mandate-comment` accepts a portable comment UID or local numeric
  ID. Frozen renewal evidence continues using UIDs and digests; writer, gateway,
  episode, stale-mandate and safety checks remain. Renewal is not promotion.
- Exact-card `show` may read an enrolled primary board's read-only replica when
  its live database lacks that card. List and execution admission are unchanged.
- Only a matching, definitively rejected `decision_pending` retain with reason
  `missing_decision_episode` may reserve an ordinary `released` run outcome.
  The original retain request and rejection remain immutable. Unknown delivery,
  other rejection reasons and existing failure-recovery custody do not qualify.
  A release ACK alone is insufficient: exact canonical task and run evidence must
  independently confirm the end. Successor leases and pending writes are fenced
  during projection. This is not retained acceptance, dependency satisfaction,
  scope release, an unused grant or a spend refund. Actual work remains counted.

## Root-cause chain for this subset

1. An ownerless legacy card cannot renew scope: the real `resolve_actor` reader
   rejects it with `not_scope_authority` (adoption regression baseline).
2. Before this change explicit adoption writes a decision subscription, not the
   created link consumed by that authority reader. Adoption therefore reports
   success while renewal still refuses (same regression's actual CLI composition).
3. A link without durable resolution provenance can be released by ordinary chat
   reconciliation; default/named-profile and same-session-ID collision tests run
   reconciliation twice and then exercise the renewal reader.
4. Numeric mandate IDs are replica-local. The UID tests remap the local ID while
   retaining the portable event UID and prove both references freeze that UID.
5. A missing-episode rejected retain cannot become retained custody. Previously
   its immutable rejection also prevented a truthful terminal tool result. The
   fallback tests reproduce that refusal and require a separate released operation
   plus exact canonical confirmation, without replacing the rejected command.

The original historical retain rejection's exact reason is unknown. These tests
prove the supported missing-decision-episode class, not that historical cause.

## Verification

Canonical baseline regression run: three files, 20 passed and 25 failed (five
adoption, eight portable-mandate, twelve terminal-fallback failures). After
extraction, four regression files passed all 76 tests, with no file retries.
The adoption suite includes named-profile reconciliation and default-session-ID
collision controls; the fallback suite includes unknown delivery, immutable
custody, ACK-only refusal, real tool adapter and separate-connection successor
lease coverage. Primary/replica show tests retain read-only and admission checks.

Affected consumers, full package checks, exact-head required CI, independent
subset-only review, canonical landing and installed-hub before/after proof are
additional gates; local tests alone do not establish delivery. Historical incident
cards must not be used for the live proof or released by this subset.
