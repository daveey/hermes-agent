# Fairness Phase A fleet roster

`inventory scan` describes named trees, not the fleet. A successful scan of the
responding hosts does not establish that all queue-serving sources responded.
Validate the independent fleet roster before using those inventories:

```sh
scripts/hermes-python scripts/kanban_fairness.py inventory roster \
  --manifest plan.json --output roster-validation.json
```

This command only reads the plan and writes the requested validation file. It
never activates capture, polls a gateway, changes placement, or claims work.
Exit 0 means structurally complete roster evidence; exit 2 means incomplete or
unsupported evidence, not an empty successful inventory. It is not a fairness
verdict. The capture-validation owner must also gate the full report on this
result; running the standalone command does not alter an existing capture.

## Discovery contract

Retain a read-only registry listing including inactive/offline entries, declared
worker pools, and each relevant gateway's board and profile inventories. Record
provenance and observation times. Reconcile those lists against actual serving
process receipts. A registry's `pickup=true` or a recent heartbeat alone does
not prove that a process serves a board/profile. Held or inactive entries need
an explicit disposition supported by evidence, not removal from the discovery
record. A gateway that cannot answer remains unknown. Do not wake retired hosts.

Names/slugs are lookup labels, not proof of database identity. Bind the registry
gateway identity and profile to authoritative `board_uid` and `database_uid`
from source receipts. Never substitute a local task/run/event integer, invent a
UID for an unanswered source, or identify a database by its path alone. Retain
unknown discoveries separately in the source evidence and set discovery
`complete:false` until they are resolved. Enumerate *every* relevant board and
profile on each serving gateway, rather than a Cartesian product of guesses.

## Manifest schema

`manifest.roster` is a version 1 object with:

- `window: {start_ms, end_ms}`: nonnegative integer cuts, end strictly after start.
- `expected_sources`: independently enumerated source identity rows, including
  unavailable sources. Identity is the tuple
  `(source_gateway, profile, board_uid, database_uid)`, all nonempty strings.
- `opening` and `closing`: `{complete, provenance, observed_ms, sources, unknown_sources}`.
  `unknown_sources` must be an explicit empty list for complete evidence; retain
  unresolved registry entries here with their original labels and reasons.
  `observed_ms` equals the corresponding window cut. Each source row carries
  identity plus `process_epoch`, `core_revision`, `config_sha256`. These pins
  describe the loaded serving process/configuration, not just checkout HEAD.
- `membership`: `{complete, provenance, start_ms, end_ms, events}`. This is a
  continuous membership-journal receipt spanning the window, not a conclusion
  inferred from equal opening and closing lists. Without such evidence use
  `complete:false`; unobserved between-poll joins remain unknown.
- `coverage`: source identity and pins plus `status`, `provenance`, `start_ms`,
  `end_ms`, `opening_cut`, `closing_cut`. Status is `ok` only with actual source
  receipts covering the whole served interval. Use `offline` or `unknown` and
  null cuts for unavailable evidence. Cuts are source-epoch-qualified journal
  positions, not bare local sequence integers.

All provenance strings refer to retained evidence artifacts. The checker checks
structure and agreement, not authenticity of operator assertions or existence
of arbitrary provenance references. Discovery must independently establish
scope completeness: omitting a source from *all* declarations cannot be detected
by comparing those declarations alone. An assertion copied from the capture
receipts is not independent discovery.

The checker returns the union of expected, opening, closing and coverage sources,
with each contributing row and nulls for missing sections. It rejects duplicates,
empty rosters, missing identities/pins/provenance, unavailable sources, cut gaps,
and process/revision/configuration changes. Version 1 intentionally rejects any
membership event or opening/closing membership difference as
`membership_changed`; it does not claim to reconcile joined/departed intervals.
Even a transient join that left before closing must appear in membership events.
Changed membership requires reconciliation by the capture owner, never a rerun
that quietly deletes the source. Missing continuity makes the window incomplete.

A read-only discovery collected outside an authorized capture will normally be
INDETERMINATE: no opening/closing capture cuts or continuous membership evidence
exist. Preserve that output and the labeled unknowns; do not manufacture seals,
activation, timestamps, process pins or successful empty captures to make it pass.
