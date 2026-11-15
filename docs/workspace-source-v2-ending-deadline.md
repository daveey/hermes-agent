# Ending selected-artifact deadline integration (unshipped)

Both ending preflight paths pass their existing Git `_Deadline` into selected-artifact production. Metadata push and each selected blob use a private Hub with the earlier of the existing Hub deadline and remaining preflight budget; the shared client is unchanged. Budget checks before/after each entry prevent beginning another transfer or returning success after expiry. Launch reuses the same private-client construction.

Independent incremental review found that ordinary `Hub.ops` only sets per-socket timeouts: a continuously trickling metadata response outlives that deadline. The committed local HTTP regression reproduced the failure. `SourceArtifactHub.ops` now uses the existing attachment HTTP `Transfer` watchdog, not an abandoned-thread timeout wrapper. It preserves JSON MIME, refuses redirects, does not replay uncertain metadata writes, and caps response bodies at 25 MiB. Existing attachment calls keep their previous default binary MIME and five-second cap. Source metadata uses the existing client timeout within the shared deadline. Local HTTP tests prove caller termination and peer socket closure for a trickling response, plus success/409/307 controls and exactly one request.

## Evidence

- Initial caller/budget tests: eight failures before wiring. The private-client test's explicit clock was corrected before GREEN (the original `_Deadline` default captures monotonic at definition).
- Review-found real metadata trickle: one assertion failure; three controls passed before transport fix.
- Final canonical nine-file selection: 153 passed, zero failed. Includes attachment HTTP regressions and artifact concurrency. `git diff --check` passed.
- Two targeted mutants killed by assertions: ignoring stricter existing deadline; omitting the post-transfer budget check (single-entry case). Both restored before final run. Not exhaustive per-condition shipment proof.
- Raw outputs: hub repository `docs/spikes/workspace-source-v2/evidence/ending-deadline/`.

## Still owed

This is not a shipment approval, composed Board artifact proof or managed-worker proof. No PR/deployment/protocol advertisement change. Final source-publish Board RPC and Git identity probes retain their existing separate call caps; this increment bounds selected attachment metadata/blob transfer, not every existing preflight phase. Publication run/selection fence after portable returns, authenticated initial registration/mid-run selection, published attach/ending repository identity, old-Board race-safe pending reconciliation, clone guidance, N4 isolation/N6 lanes, move/reclaim/fresh-process/composed real Board/core artifact proof, actual managed launch, exhaustive mutants, independent exact-head shipping review, canonical PR/CI/landing, installed rollout and two-worker proof remain required.
