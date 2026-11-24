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

Creator renewal318157 narrows final D to clock/production-HTTP recovery proof,
malformed-response classes, then independent exact-head review and canonical
landing. Realistic latency/backlog/lease throughput is owned by integration
`t_01a2053842e6e0eb` against the real Board, not a sender completion gate.
Full reconciliation of1213 generated variants is excluded, not claimed complete.

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
2. **General bounded transport recovery (local slice implemented; D not closed).**
   The public strict sender now retains one durable journal/generation/sequence/
   operation-bound transport fault after reachable OSError/HubError failures. Eight
   failed reachable attempts or300 observed reachable seconds pause automatic I/O;
   the status fault includes the exact identity, reason and explicit `replay-resume`
   repair command. Retry mutation deadlines grow exponentially to60s. An unpaused
   stream may inspect/recover a known verdict during backoff, but may not resend
   an uncertain mutation before its deadline. Mutation timeout/gap/ACK-required
   errors trigger immediate authenticated inspect without changing custody; the
   following serialized turn performs normal exact-verdict recording and ACK.

   Only intervals inside a turn after a successful authenticated exchange are
   charged. No baseline crosses turns/restarts; wholly offline attempts preserve
   the fault, attempt count, age and issued bytes unchanged. This intentionally
   does not infer continuous reachability during scheduler gaps. The fault survives
   reopen, logs once on pause and requires identity-checked explicit resume; the
   existing current-row hold takes precedence when both holds are present. Clearing
   a read hold never silently clears a separate transport hold. A successful exact
   settlement/ACK boundary retires a superseded recovery episode before new work.
   Rebuild never fills this mutable fault state from backup.

   New public-path controls cover8-attempt stop/reopen/resume,300-second stop,
   deduplicated logging, offline custody, inspection-only backoff,409 gap/ACK-required,
   wrong identities/reasons, caller-transaction refusal and actual CLI dispatch.
   Existing precommit-retry fixtures advance a controlled clock beyond the new
   deadline rather than disabling admission or sleeping. The seeded model now
   explicitly resumes a paused one-shot fault to continue exploring later history;
   it no longer assumes infinite automatic retries.

   Clock boundary correction uses monotonic elapsed samples and deadlines, with a
   persisted random process-clock identity. A foreign process/boot or legacy
   wall-clock row rearms exactly one bounded delay under the existing writer;
   neither offline age nor attempts reset. Repeated calls within that clock owner
   retain the deadline. Paused faults never rearm or perform replay I/O. Wall-clock
   rollback and forward jumps reproduced actual assertion failures (elapsed0 or300
   instead of100); monotonic sampling makes both pass. A foreign-clock reload and
   SQLite reopen reproduced the stale1002-versus12 deadline before reanchoring.
   Legacy-table migration preserves attempts/age/identity; this is a simulated
   interpreter-clock replacement, not a measured host reboot.

   Seven production Hub/loopback HTTP cases cover those clocks,8-attempt and300s
   limits, active healthy strict-stream settlement after a paused earlier stream,
   wrong-identity resume and later same-intent progress. Fault-metadata pre-COMMIT
   injection rolls back the eighth update, preserves fault7 and exact custody on
   reopen, then writable repair durably pauses at8. A retry appends only its new
   session exposure; old exposure history and frozen identity remain. This test
   injects at the existing transaction context in disposable fixtures, not through
   a production hook/authorizer; it does not promise progress while storage remains
   unwritable. This local slice is not real Board interoperability/final D approval.
3. **Aggregate fault/storage proof (local boundaries now exercised).** Three new
   test scenarios run in both DELETE and WAL journal modes with real SQLite
   `max_page_count` exhaustion and assert `SQLITE_FULL`, not
   an injected exception or host disk exhaustion: journal issue, authenticated
   exposure append, and retained-verdict/ACK-tuple storage after actual source
   deletion. Reopen verifies exact old custody; after lifting the connection-local
   page cap, the same issue/exposure/settlement can complete. The settlement test
   explicitly verifies the source deletion ran and rolled back with the failed
   metadata write. These are journal/exposure API proofs against minimal storage,
   not full sender/Board integration.

   Four independent-process SIGKILL tests exercise the actual public sender and
   production HTTP transport at capability, accepted session, accepted operation
   before reply, and accepted ACK before reply. Reopening preserves the exact
   journal/source/exposure image. The fixture receiver sees each original operation
   once across recovery; the remaining independent lifecycle event then settles.
   Four additional process-death controls cover uncommitted issue, source DELETE
   inside the settlement callback, completed-but-uncommitted settlement, and
   committed settlement. Pending bytes, source custody and the ACK identity tuple
   are all-old or all-new on reopen. These do not simulate power loss/fsync failure
   or prove deployed Board atomicity. No production hook, sqlite authorizer or
   reentrancy change was added. Baseline production passes these controls; no
   production RED is claimed. Three targeted premature-commit/exposure-omission
   mutants fail at their intended assertions, with original source restored.
4. **State and model evidence (local campaign now exercised).** Four fixed seeds
   drive an independent integer-prefix receiver oracle through56 scheduled faults
   or clean turns per seed, with a real SQLite reopen and full journal/source
   custody comparison after each attempted turn. Each seed reaches all eight
   capability/inspect/session/operation/ACK boundary cuts; accepted operation IDs,
   exact canonical digest, predecessor ACK and W=1 prefix invariants are checked
   independently of production reconcile/recover. A successful drain leaves no
   pending sources and equal local settled/remote H/A, and an idle reopened turn
   cannot resend. A singleton/encoded-state bound rejects per-settlement journal
   accumulation while intentionally excluding business obligations and test oracle
   history. Baseline production is GREEN, not a new production RED. Three model
   mutants (omitted uncertain-verdict recovery, idle ACK, bounded state) are killed
   at assertions. This is neither deployed receiver correctness nor a proof of all
   possible interleavings.

   **Integration-owned throughput remains open:** measure backlog/lease-sensitive throughput with
   request counts, declared thresholds and realistic latency. Constant-state and
   many-settlement correctness are not throughput or lease-renewal acceptance.
5. **Active-stream isolation now exercised; remaining condition reconciliation.**
   Four real multi-board dispatch cases place a faulted strict stream before a
   second active strict shadow stream. Capability, inspect, session and transport
   faults preserve the first stream's exact source/journal custody while the
   second sends, settles, ACKs and drains all its work through production rounds.
   The failed board's durable observation token changes independently of the
   successful board's process-global epoch, and repair later progresses its same
   original intent. An interruption-omission mutant initially survived a weak
   global-epoch assertion: the healthy board itself rotates that epoch. The final
   board-local durable-token assertion kills it. Empty feed/receiver fixtures are
   not real Board interoperability. No sender/round/journal function is replaced.

   Malformed peer-response behavior at the supported transport boundary remains
   the next sender gate. Residual unreconciled generated classes include defensive
   identity-race/schema-probe/error-format combinations; full clause enumeration is
   explicitly out of renewed D scope, not labeled equivalent or killed. A
   generated count of1213 syntactic mutants is neither1213 distinct behavior
   requirements nor a kill claim. Mark true equivalents honestly.

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
