# Sender final audit: remaining gates (not approval)

Audit source: `c4d622710f234c158dac068280b2e3f23df00e66`, with the additional
condition controls in this commit. This is an implementation evidence inventory,
not an independent exact-head review, activation grant, or D completion.

## Verified additions

The CI creator-empty recovery consumer now preserves every original schema object
while explicitly requiring the new independent recovery-loss singleton and its
unknown-completeness reason. Original certificate/candidate assertions remain.
A strict stream validation refusal is typed `ReplayFault` and isolated by the
real multi-board round; later detached-board traversal and recovery after repair
are tested. This does **not** yet prove a later active strict stream settles.
Legacy programming ValueErrors remain unwrapped.

Four independent local migration bindings are tested using authentic internally
complete foreign receipts. Additional tests cover admission ordering, sender
contention, malformed contracts, domain separation, exact signed payload,
missing readiness fields, malformed independently pinned keys, and valid inclusive
barrier/readback and freshness boundaries. Diagnostic tests cover other-task and
other-Board retirement isolation, stored receipt content, immutable owner input
validation, unbound/unknown-key refusal and caller-transaction ownership.

Five new targeted condition mutants are killed: removing either retirement scope,
omitting signature domain, skipping signature verification, and accepting empty
owner bindings. Each actual child exits 1 at its intended assertion and source
bytes are restored. These are fixture condition proofs, not real delivery or
migration authority. Baseline controls were green on unchanged production; there
is no invented production RED claim.

The diagnostic page-damage/no-fill case is in
`test_kanban_recovery_protocol.py` (the diagnostic receipt_json table entry), not
in the diagnostic producer test file.

## Unresolved sender requirements: D stays open

1. **Normal ACK carrier / throughput (partial progress).** The public sender now
   leaves the predecessor ACK for the successor's frozen envelope rather than
   sending an intermediate standalone control. Exact newly consuming ops replies
   (explicit protocol, consumed=true, sequence, H, ID and digest) can settle without
   a redundant inspect; all incomplete/nonconsuming/unknown/duplicate reply shapes
   still require authoritative inspection. Idle/end-of-turn and recovery ACKs stay.
   ACK readback requires identical slot fields and JSON-equivalent final verdicts,
   allowing only representation differences (Board JSON.stringify versus Python),
   never different verdict content. Nineteen new fixture cases include40 successive
   settlements, uncertain successor/reopen, bad consuming metadata, compact JSON,
   changed verdict refusal and exclusion of current-row reply enrichment.

   The production HTTP sender now pins a private transport, immutable credential
   snapshot and one capability response for the entire sender turn. Capability,
   inspect, session, mutation, recovery and final ACK all use that same transport;
   each mutation still commits exposure before its body leaves. A new public turn
   starts a new snapshot/probe, never a cross-turn or cross-Board cache. Legacy
   sends retain their independent per-mutation pin/probe.

   Seven real-loopback tests exercise20 settled operations with one kept-alive
   connection and exactly capability→inspect→session→20ops→ACK, replacement of the
   outer credential during the initial probe, capability refusal, turn boundaries,
   actual accepted-body/lost-reply→reopen→no-resend recovery, and read-only or open
   transaction exposure refusal. Separate SQLite readers verify each exposure is
   committed, bound to its full wire and matching authenticated identity before
   the HTTP body is processed. This proves the production HTTP request sequence
   against a fixture, **not realistic-latency/lease throughput or real Board
   interoperability**. Two unchanged-source assertions failed first: redundant
   probes and outer-credential replacement during the initial session probe.

   The real receiver's reply
   shape was read from `settleReplay` at hub main f6f535ae7205cdf07239248bf48d165f712f9f11,
   not invented from client expectations. Its current ingress reads top-level
   `op.ack`, whereas the frozen sender stores `op.replay.ack`; this interoperability
   mismatch remains explicitly receiver/integration-owned and unproven. Do not
   unilaterally change frozen wire identity. Fixture request counts are not real
   Board/latency/backlog/lease throughput evidence.
2. **General bounded transport recovery.** Spec lines 384–393 bound automatic
   timeout/gap/ACK-required recovery to eight attempts or five reachable minutes,
   preserve custody offline, stop with one actionable recovery fault and require
   explicit resume. `kanban_replay_existence` currently bounds current-row faults
   only. Ordinary strict OSError/HubError paths interrupt observation and rethrow;
   `SyncWork.finished` caps delay at 60s but does not stop attempts. Prove the
   general public path, restart persistence, offline exclusion and exact custody.
3. **Aggregate fault/storage proof.** Existing callback rollback/page-damage tests
   are not a proof of actual SQLITE_FULL at freeze/exposure/settlement/ACK metadata
   boundaries or hard process death at those boundaries. Add bounded real fault
   controls; never replace a runtime write hook/authorizer or reentrancy change.
4. **State and model/throughput evidence.** Add many-settlement/reopen and seeded
   reference-model sender interleavings, separating constant stream state from
   intentional business/diagnostic obligations. Measure backlog/lease-sensitive
   throughput with request counts, declared thresholds and realistic latency after
   correcting the ACK path. A three-operation fake-peer example is not this proof.
5. **Isolation and remaining condition reconciliation.** Test an active healthy
   strict stream after a faulted stream, observation interruption on strict failure,
   and malformed peer-response behavior at the supported transport boundary. A
   generated count of 1213 syntactic mutants is neither 1213 distinct behavior
   requirements nor a kill claim. Reconcile the remaining meaningful condition
   classes with specific behavioral controls, marking true equivalents honestly.

## Downstream proof boundaries (unchanged)

Receiver `t_01a2053828328f1b` owns real Board serialization, retained verdicts,
collection/atomicity, session/generation identity, complete current/former principal
inventory, actual ingress barriers, drainage and readiness. Integration
`t_01a2053842e6e0eb` owns real receipt interoperability, trusted context wiring,
owner-transcript delivery/readback/dedup, cross-repo gates and rollout/live proof.
Fixture peers do not establish those facts. Accepted A/B1–B5 and local sender
fence/writer2 proofs are not to be repeated. No activation, deploy or held-original
release occurs here. Section 9 rollback remains mandatory. Fresh independent
exact-head review, required CI and canonical ship still follow completed D work.
