# workspace_source v2: published-attach and ending repository identity (N1)

Spike increment for t_01a1d0b341560679. Not shipped; protocol advertisement remains 1.

## What changed

- `kanban_source_launch.attach`: a protocol-2 attach of a published manifest proves the
  registered clone (`registration.source_identity`, the publisher's `prove_origin`) and
  requires its repository id, full name and source kind to equal the manifest's, BEFORE the
  pending observation is recorded or the Board attach RPC is sent. The identity is persisted on
  the observation. A protocol-2 attach without a manifest is a typed `source_identity_refused`
  (previously it would have raised `KeyError`).
- `kanban_source_publish.validate_run_identity`: v1 observations (protocol 1, manifest v1) skip;
  v2 observations fail closed when the persisted identity is missing or differs from the current
  clone proof, or when the current identity differs from the manifest.
- Ending preflight validates identity before the artifact/unchanged shortcut, so an unchanged
  v2 ending cannot bypass it. The direct publisher validates against the clone it already
  proved, before any push.
- A successful v2 publication persists the publisher's identity (and protocol 2) on the
  observation, so an artifact-driven upgrade carries identity into the next ending.

## Verification

- RED on 334cf1b: 15 new tests failed (`identity-red.log`).
- GREEN: all 27 `test_kanban_source_*` files + `test_kanban_workspace_source.py`: 500 passed, 0 failed.
- Mutants (assertion-killed, then restored and rerun 16/16):
  preflight validation removed -> 5 failures; attach manifest match removed -> 2 failures.

## Known limits

- The preflight proves origin once for validation and `publish` proves again (two clone
  proofs on a changed v2 ending). Each proof carries its own call caps; not yet bound to the
  shared preflight deadline.
- Exact repository full-name comparison fails closed on a GitHub rename; rename policy is
  owed to shipment review.
- Not a composed HTTP or managed-launch proof.
