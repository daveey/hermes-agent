# Scratch source registration: incremental safety fixes

This is still a spike, not a production entry point or shipment approval.

Registration now serializes the initial observation with a fresh local lease/run check and immutable identity comparison in the existing source-run journal. Git reads and Board RPC remain outside the transaction. Concurrent registration cannot replace the winning observation. Same-identity retries retain its original head/state. The existing journal writer accepts an explicit nested transaction only at this composition site; its default still refuses accidental nesting.

Successor recovery requires the grant predecessor to equal custody.retained_run_uid, in addition to the existing task, run, holder, revision and protocol checks. Lease expiry must be finite; NaN/infinity are not live lease evidence.

Registration accepts the task's assigned branch_name (including project/task branches), falling back to wt/task-id only when no branch is assigned. The source must still be a Git linked worktree listed by a configured workspace_source clone. Ordinary clones are intentionally refused: configure the existing clone using the workspace-source clone registry, create a linked worktree on the assigned branch, then explicitly register that path. No unrelated trees are auto-adopted.

## Verification

Canonical scripts/run_tests.sh:
- Four new predecessor/expiry controls failed by DID NOT RAISE before fixes.
- Two observation-race controls failed before the transaction fix (competing connection winner and lease expired during Git observation).
- Assigned project branch acceptance failed before branch support; a different assigned branch remains refused.
- Final selection: registration, registration_races, launch, publish, receipt: 146 passed, zero failed; git diff --check passed.

## Remaining delivery gates

The dispatcher now consumes the exact grant envelope (status, current lease, source_predecessor) copied by HubLeaseClient into SourceSnapshot; replica snapshots have no envelope. A protocol-2 premanifest successor resolves only through the exact journal predecessor, rechecks it immediately before attaching, and never mistakes scratch cwd for its source. Scratch_external dispatch retains scratch cwd; attach observes and journals source.path and selects protocol from custody/manifest, preserving v1 routing. Unknown attach outcomes remain pending and become typed uncertain AttachRefused errors. Protocol advertisement remains 1 deliberately: production activation must wait for the full artifact lifecycle.

Verification of this increment: six initial RED controls; seven-suite regression 224 passed; after additional envelope/lease/uncertainty/published-source controls, six-suite final 173 passed. These are overlapping selections, not additive totals. Tests use real Git and the real lease adapter/resolver/attach composition, but a fake Board transport; they are NOT composed HTTP or worker-spawn proof.

No CLI registration, artifact producer/download integration, old-Board refusal reconciliation, or live deployment. Ending/move/reclaim paths, composed artifact proof, harness isolation/lane restriction, per-condition mutation tests, exact-head independent shipment review, CI/landing and installed two-worker proof remain owed. Retained predecessor checks expect the newer Board contract on the companion spike branch. M3 reconciliation must distinguish a first definite refusal from a prior unanswered attempt; blindly deleting pending rows on refusal can erase evidence of an earlier accepted attach or race another replay.

## Persisted premanifest identity

Initial scratch registration now uses the existing publisher's registered-clone proof (GitHub repository ID/name, origin rewrite checks, configured clone/common-dir) and stores repository ID, full name, resolved common-dir and source kind in the existing local run journal. Same-run replay and exact-predecessor recovery revalidate that identity; replacing a worktree at the same path with the same branch and commit from another configured clone is refused. The serialized observation comparison includes this identity, so a competing writer cannot replace it. Successors inherit their predecessor's identity and reject conflicting successor observations.

The additive journal migration leaves old observations intact with unknown identity, never inventing historical identity from today's filesystem. Missing v2 identity fails closed; v1 reads retain their old defaults. No deployed protocol-2 observations exist from this unshipped spike. Any intentionally retained spike fixture needs explicit recreation or the audited Board recovery route, not silent adoption.

This increment covers initial/premanifest registration and successor recovery, not yet published-manifest attach or ending-side identity validation. Those must use the same journal field before protocol advertisement becomes 2. Canonical tests use real Git/common-dir/origin checks and a stub only for external GitHub reads and Board transport; they do not establish network or managed-worker proof.
