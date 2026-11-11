# Qualified historical terminal carry recovery

A retired task owner is not a live publisher. Its disappearance must not turn an
unverified terminal task into completed work. Sometimes its original, preserved
owner SQLite database still supplies the existing completion verifier's evidence.
`hermes_cli.kanban_terminal_carry.register` is the explicit operator API for this
case. It creates a local repair receipt, not a task completion or origin binding.

## Invariants

- Verify recovery provenance and the SHA256 of a self-consistent owner database
  first. Never substitute a Board replica: local event IDs are reassigned there.
- Supply the exact qualified historical card from the origin snapshot. Its key,
  owner gateway/profile/board/task, origin gateway/profile/session and shepherd
  relation must agree. NULL is never a default-profile wildcard.
- Registration requires a human session in the selected origin's state database,
  an unambiguous qualified creation subscription, an exact shepherd link, and
  the original `observe_owner_task` / `_verified_terminal` result. The closing
  run also proves the owner profile. Status or an existing released flag alone
  cannot register a receipt.
- Compare current Board task generation fields and portable completion UID to
  that original owner proof. No remapping of numeric event IDs is allowed.
- Write one receipt per exact identity under the origin profile home's
  `kanban/terminal-carry/*.json`. A duplicate identity is ambiguous and releases
  nothing. Use a workspace path first for an admission-only dry run.
- The ordinary retained-snapshot carry path recomputes evidence from the
  hash-pinned owner database and requires a fresh matching Board shadow. Fresh
  owner inventory takes precedence. A newer generation, missing/corrupt proof,
  stale Board shadow or wrong identity retains an unknown obligation.
- A verified historical row remains **stale**, with `terminal_carry_verified`.
  It is retained rather than erased: proof loss resets it to unknown. This is
  completion evidence, not a fresh heartbeat or retroactive workload proof.
- The existing chat-link `reconcile` publishes qualified release changes through
  normal capture/sync. Neither registration nor projection edits tasks, links,
  origin bindings or events directly. Verify exact installed `session_tasks`
  projections after normal sync, including unrelated open controls.

This API is within the trusted local-home administrative boundary, like other
local recovery APIs. A file hash binds evidence bytes; it does not authenticate
who recovered or authored them. Independently establish snapshot provenance
before registration. Keep evidence private and durable; deleting it intentionally
withdraws the release evidence. Empty WAL sidecars are harmless; nonempty WALs
are refused. Immutable SQLite reads avoid creating sidecars on preserved files.

## Cause and prevention

Problem: a qualified shepherd link to a completed remote task can remain an
unknown obligation after its owner is retired.

Why 1: the Board reader requires an exact-profile released sibling; a NULL-profile
created link cannot release an explicit-profile shepherd (`_read_replica_links`).
Why 2: the retained origin snapshot still supplies an unreleased shepherd, so
`_ledger_links` and normal `reconcile` write it back unreleased.
Why 3: `_legacy_remote_links` intentionally retains missing-owner inventory rather
than guessing that disappearance means completion.
Root (happened): there was no checked offline reconciliation path for a retained
qualified obligation whose original owner completion evidence remained available.
Root (not caught): existing terminal-release coverage exercised fresh owner
observations and alias folding, not retired owners plus preserved authoritative
completion evidence. The two new integration tests fail without the carry seam.
Recurrence: prior owner-created-link releases and inherited-creator healing address
different link kinds; neither resolves a retained qualified shepherd obligation.

Countermeasure/proof: exact-identity registration plus revalidated historical carry
uses the established completion verifier without weakening the ordinary reader.
`tests/hermes_cli/test_kanban_terminal_carry.py` covers missing/invalid proof,
NULL/wrong-profile identity, missing owner runs/events, goal-mode without receipts,
changed completion generation, corrupt registration, stale/current Board state,
both snapshot carry entry points, and standard release capture/reopen restoration.
