# Review handoff and prelaunch selected artifacts (unshipped)

This increment extends the existing completion staging helper to authenticated
`kanban_request_review`, before the outer receipt preflight. It retains foreign
card rejection and caller-bound run identity. Empty selection differs from an
absent artifact field; clearing selection never deletes retained attachment rows.

The source resolver now verifies selected artifacts before returning to attach
and spawn. Published grants supply the selection, never stale local selections;
revision0 resume uses the exact retained predecessor journal. Missing metadata or
transport failures refuse host-locally as artifact_unavailable; verification
failures use artifact_mismatch. The current holder also honors these memos:
unavailable retries after 60 seconds at the same revision, mismatch after six
hours. Existing unrelated holder reuse behavior remains unchanged.

Artifact downloads reuse the verified attachment transport and a private Hub
instance whose absolute deadline is capped by the remaining materialization
budget and any existing Hub deadline. The shared sync client is never mutated.
Artifact-phase budget exhaustion maps to artifact_unavailable, including expiry
after successful download. Git/identity operations retain their existing bounds.

Proof: review owner callsite RED1; artifact pickup RED4 after correcting an
invalid fixture API; resolver download/refusal RED4; independent incremental
static review identified deadline classification, then RED1 and fixed. Final
nine suites: 152 passed, zero failed; diff-check passed. Real Git, SQLite and
attachment verification/copy are exercised; download responses are controlled,
not a composed HTTP or actual managed launch proof. No new exhaustive condition
mutants or shipment approval are claimed.

Remaining full-card acceptance: supported initial registration/mid-run selection;
publication run/selection fence after portable returns; ending upload/push shared
absolute deadline; published attach/ending repository identity (N1); old-Board
race-safe pending reconciliation (M3); clone guidance; environment isolation and
lane restriction; move/reclaim/fresh-process proofs; composed real Board/core
artifact HTTP and actual managed launch; exhaustive condition mutations;
independent exact-head shipping review, canonical core-wt PR/CI/landing, installed
rollout and two-worker proof. Protocol advertisement remains1. No PR or deploy.
