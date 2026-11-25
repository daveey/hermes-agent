# Daily CI flake intake

## Catalog confinement repair receipt — November 22–23, 2026

Canonical repair owner: `t_01a2117dc0f39dbd`; intake owners:
`t_01a2055174191bc9` (Nov22), `t_01a205ca02b80d5b` (Nov23).
The rejected request `placement:worker-a2:faeb7513044182dce903f37a`
created no repair card; the owning chat established the canonical owner above.
Intake completion closes triage only, not repair or landing acceptance.

| Occurrence / exact test in `tests/ci/test_plugin_catalog_ci_confinement.py` | Original first-attempt evidence | Disposition |
|---|---|---|
| 98 / `test_dot_named_self_updaters_are_scanned` | Run `45074930667`, job `137370140500`, lines 1212–1247: nonzero exit and self-updater rejection pass; the negative `PASS` substring assertion matches `tmp.toJPASSLFh` in the diagnostic path. | Test oracle flake, not a product bypass. Match actual `✅ PASS: ` result records; audit sibling positive and negative assertions. Force the incident path for every dot-name case. |
| 110 / `test_a_syncs_own_scanner_cannot_wave_a_payload_through` | Run `45133478598`, job `137554235239`, lines 1778–1811: `_delta_fixture` local clone exits 128 copying `.git/objects/bitmap-ref-tips_ajB7Sr` (ENOENT), before scanner assertions. | Fixture setup race at local object-directory copying. Use `--no-local` for fixture clones so upload-pack transfers reachable objects, ignoring transient maintenance files. The historical log does not identify the maintenance process; detached maintenance is consistent with the filename, not directly observed. |

Both original logs were retrieved with `gh run view RUN --job JOB --log`.
A read-only query of the producer journal on November 25 found **only one
retained occurrence per exact test**, IDs 98 and 110: there is no second retained
occurrence to retrieve for either test. This is not a claim of complete CI history.
The Nov23 evidence bundle covers occurrences 99–331 and contains only 110 for
this file; occurrence 114 concerns another test, not a second catalog failure.
The remaining intake backlog is outside this repair's two exact tests.

Deterministic controls retain real rejection, trusted-checkout and injected-scanner
assertions. The clone regression places an unreadable transient object-store entry
in the source: explicit local copying fails, while the fixture clone succeeds with
identical HEAD and plugin bytes. This controls the failure boundary, not the
historical maintenance schedule. Mutations restoring local copying or the broad
substring assertion must fail. No tests are deleted/xfail-ed, and no retry policy,
production scanner, timeout or security admission rule is relaxed. Repair is not
considered landed until independent exact-head review and required CI pass through
`hermes-core-wt ship`; PR and final proof receipts belong on the canonical owner
and both intake threads.

The producer retains metadata-only occurrences in a SQLite journal beside its JSON cursor:
`ci-flaky-cards.json` → `ci-flaky-cards.intake.sqlite3`. The journal, not the bounded cursor
or the card thread, is backlog truth. Its sibling `.intake.lock` serializes producers and
contains an initialization marker. Production additionally requires a UUID stored in SQLite
and in a separately retained identity anchor. Never delete these files to reset a stuck poll.

## Declared core producer receipts (v1)

The production CLI opts in only for `softmax-universe/daveey-hermes` (and its
`daveey/hermes` alias), declaring `{"version":1,"repository":"hermes-core","area":"ci","kind":"ci-flake"}`.
The declaration belongs to this producer, not a title classifier. Other repositories,
legacy `file_cards` callers, and exact-test owner matching keep their existing routes.

Reusable state-adapter contract:

1. Inspect `hermes kanban --help` for `selfwork-producer` before any submission.
   Absence (including pre-self-work runtimes) permits the old undeclared path only
   for work not already frozen. `hermes kanban selfwork-producer --operation capabilities`
   returns `{"self_work_producer":1,"self_work_producer_placement":1}`. A well-formed unsupported version likewise
   permits the old path before submission; errors/malformed capability replies do not.
2. `hermes kanban selfwork-producer --operation prepare --request-file -` accepts
   JSON with required `title`, `body`, `assignee`, `idempotency_key`, and `self_work`,
   plus optional `runs_on` and `requires` string lists. No other create fields are allowed.
   It performs no submission and returns `{version:1,sender,home,body,request_id}`.
   `home` is `{gateway,profile,board}`; `body.create` freezes those input fields,
   home board and tenant. Persist this entire snapshot atomically before step 3.
   The producer is headless: no ambient chat binding or worker creator is adopted.
   Worker tasks must continue through their origin-forwarding API instead.
3. `hermes kanban selfwork-producer --operation advance --request-file -` accepts
   that **unchanged snapshot**, sends only through the configured courier and reads
   the sender's durable `sent` ledger. It returns either
   `{version:1,status:"pending",request_id,owner:null}` or
   `{version:1,status:"completed",request_id,disposition:"create"|"append",owner:{gateway,profile,board,task_id}}`.
   A caught transport/storage/validation error returns pending with `error` and may
   omit request_id. Exit 0 alone is not delivery. Never downgrade any attempted
   declared submission, regenerate it after midnight, or retry via local creation.

Placement-aware adapters must require both integer capability values to be `1`
(not booleans); the older `{"self_work_producer":1}` does not advertise placement.
Only before preparation or any declared attempt may an adapter choose its legacy
route for a well-formed unsupported capability reply. Once prepared, retain the
snapshot and retry only `advance` on a compatible runtime; never strip placement,
regenerate a request, or fall back after errors. Minimal v1 snapshots remain valid,
and consumers not needing placement may continue checking just the base capability.

The optional lists use existing create label/capability validation, including empty
lists; null, scalar strings and non-string members are rejected. Preparation and
snapshot validation do not normalize, sort, deduplicate or insert omitted fields.
The trusted receiver still applies normal create normalization and placement gates:
explicit labels (even `runs_on: []`) need a synced intake board and eligible
capabilities, not merely a producer capability advertisement. That advertisement
is local-runtime support, not a health check of the remote intake. Receiver create
refusals may surface only as `self_work_dispatch_pending`; persistent pending needs
operator diagnosis of the intake's sync/placement gates, never a legacy fallback.
Do not re-prepare an existing key with changed placement: courier/intake replay
binds the original payload. Normalize legacy comma-separated or JSON-list CLI
syntax into a list before preparation, not inside a saved snapshot. Label whitespace
is validated using create normalization but retained in the snapshot itself.
Same-area append preserves the existing owner's
placement and requirements; requested placement applies only to a newly created task.

State adapters must also account for these existing consumer differences:

- `created_by` is not caller authority: the authenticated gateway supplies it. Put
  producer attribution in the body rather than forwarding a legacy `--created-by`.
- The owner is the full gateway/profile/board/task tuple, not necessarily a local
  default-board task. Local `show` or SQLite lookups are not cross-home readback.
- Appends can return an owner with another idempotency key. A successful append
  already includes the submitted body; do not send a second legacy comment.
- Freeze each evidence batch and key before transport, and mark only that batch
  consumed after a completed receipt. Pending is not success even with exit zero.
- CLI `--runs-on` strings must be converted to the equivalent string list before
  preparation. Keep caller-selected labels and `requires` intact.

Completion requires a matching frozen envelope, terminal courier `done` state and
an authoritative `self_work_accepted` owner receipt (`ok`, `handoff_complete` true,
`ownership_pending` false, exact request ID and create/append consistency). Append
also requires its positive comment ID. The owner's primary idempotency key need
not match the finding: many findings legitimately append to one owner. A queue ack,
rejection, unknown or malformed result stays pending. Replay after loss of courier
cache uses the same identity and the home's durable intake journal. Never edit a
saved snapshot to repair a refusal: reconcile its cause at the owning service.

CI persists the snapshot in `pages.producer_request` with `attempted=1` in one
transaction, before transport. Each immutable manifest page is included in the
submitted body and its digest suffixes the daily idempotency key. This freezes the
occurrence payload across later manifest growth and day boundaries. The intake home
continues to enforce daily area batching and its existing admission policies.
Created pages are present in the owner's body; appended pages in the receipt-bound
comment, so a second local comment is neither necessary nor safe. Occurrence owner
readback is stored as `gateway/profile/board/task_id`, never an unqualified local ID.
Previously attempted legacy pages retain their old recovery route rather than
being resubmitted under a new declared identity. Retain the journal during rollback;
pending declared pages require a runtime supporting v1 and cannot downgrade.

## One-time bootstrap

Before deploying the new producer, run the new script once with the existing cursor path:

```sh
python scripts/ci/flaky_kanban.py --state /absolute/path/ci-flaky-cards.json --initialize-intake
```

This only initializes the journal; it never polls or calls Board. A legacy JSON cursor is
preserved. Existing queue/anchor/initialized-lock state refuses initialization. Normal polling
never auto-creates a journal, even if every state file has disappeared. By default the anchor
lives under `$HERMES_HOME/intake-identities/<sha256-of-absolute-queue-path>.uuid`
(`~/.hermes` when HERMES_HOME is unset), separately from the queue directory. An explicit
`--intake-identity /independently/retained/path.uuid` overrides it and must be passed on every
poll. Back up that anchor independently and retain its configured path. Moving the queue
changes the default anchor key; pass the original explicit anchor rather than reinitializing.
A crash during bootstrap can leave partial state: stop and reconcile the files, never blindly
rerun initialization or delete evidence. The deployment owner must finish bootstrap before
claiming the naturally scheduled producer is working.

New unowned occurrences are grouped under
`ci-flaky-intake:<owner/repo>:<UTC-observation-day>`. Existing exact-test/job owners retain
ownership; confirmed owner comments do not create intake duplicates. Matching foreign and
cached foreign owners require their canonical gateway/profile/board scope heartbeat to be
fresh (the shared scope-heartbeat helper honors the owner's published collection period).
Missing, stale, future, ambiguous or unknown proof leaves the occurrence queued for retry;
no sleeping Mac is woken to satisfy this check. Individual task timestamps are not liveness.
A closed foreign owner with a visible exact-job citation owns that occurrence (skipped, as
before). Foreign candidates use the same precedence as local discovery: open exact-job owner,
closed exact-job owner, then open exact-test owner. Candidate ordering in the replica cannot
change that precedence; equal ranks use the stable owner reference. An unreadable or stale
matching candidate never establishes absence and cannot hide a known positive owner elsewhere;
with no positive owner the occurrence stays queued for retry.

**Ownership coverage contract (no-match).** Admission inventories this board plus every
hermes-hub replica card on a fresh scope. That is not a certificate of global absence: the
replica clips titles/bodies/comments, and unsynced legacy profile boards (for example
per-profile `bb-tuner`/`ci-sweeper` boards, MacBook-only boards) are not inventoried. The
baseline per-test producer had the same exposure through the create-overlap guard. A
no-match occurrence is therefore admitted to the daily intake rather than held, and the intake
card owns final deduplication during triage. A closed foreign candidate whose history is
incomplete (published total differs from the visible tail, or any field is at the clipping
limit) cannot prove it never cited this job; holding the row would strand every recurrence of a
test whose old card is closed with more than three comments. Such rows are admitted with an
`owner_check` list naming each candidate (`candidate`, `status`,
`reason=closed_history_incomplete`) so triage checks them first. Lists whose serialized JSON
exceeds 1024 UTF-8 bytes use `owner_check_ref: {sha256, count}` instead of the inline list;
this is not an empty inventory or an ownership decision. Full, unshortened JSON is retained
in the journal's `owner_check_details(digest, payload)` table, committed atomically with the
page and keyed by the SHA256 of the stored UTF-8 payload. The reference is immutable even
if a later inventory differs. Retrieve it on the producer host before disposition:

```sh
sqlite3 -readonly "$QUEUE" "SELECT payload FROM owner_check_details WHERE digest='<owner_check_ref.sha256>';"
```

Whole-journal backups include these details; an occurrences-only export does not. Export
`SELECT digest,payload FROM owner_check_details` alongside pages and occurrences for triage
or rollback. Do not prune referenced details independently of their manifests.
The worst case is a
duplicate triage row, never a dropped occurrence. To revert to holding these rows, restore
the `RuntimeError("closed foreign owner history unavailable")` branch in `_foreign_owner`.

Intake manifest pages
are bounded to 6000 UTF-8 bytes and contain `needs_triage` status and run/attempt/job/test/SHA
provenance. A page is marked delivered only after exact content readback. Delivery does not
mean a test is fixed: the intake worker records per-test dispositions and named follow-up
owners, grouping related fixes rather than automatically creating one card per test.

Unattempted pages are rechecked against exact owners before each drain. Once create or
comment delivery has been attempted, the page remains immutable so an ambiguous response
cannot change its replay identity. A definitive `create_overlap` refusal is different:
read back every named match, route exact-test owners through the existing owner path, and
retry unrelated matches only with supported `distinct_from` evidence. After an earlier
ambiguous attempt, successful complete board listing must first establish that the intake
key is absent. A subsequent definitive refusal can then be reconciled; unreadable overlap
matches retain the immutable attempted page and remain pending, never an excuse to declare
unknown work distinct. The shared polling budget also stops starting new owner
resolutions and page deliveries; deferred rows remain durable and the poll reports failure.
Board/courier operations already started may still consume their existing transport timeout.
Before GitHub discovery, a drain gets half the invocation budget and checkpoints the JSON
cursor; a slow discovery cannot prevent already queued delivery on every invocation.
Within a drain, consecutive admissions are packed by `plan_pages` into multi-row pages
(maximum 6000 bytes), flushed after at most 16 rows, an ownership boundary, or halfway
through the drain window (then after each subsequent admission). Occurrences are durable before ownership resolution; each batch's
pages are committed before immediate delivery in the same drain. Pending owner resolutions
and prior attempted pages share one FIFO ordered by durable tickets, rather than
fixed-priority phases. Batches are also flushed before selecting a prior page.
A slow operation may consume the remaining window, but its rotated ticket lets the other
queue progress on the next drain, including after restart. No new external work starts at
or after the drain deadline. If a slow owner exhausts it, the unsent pages remain
durable and retain their batch boundaries during next-drain ownership rechecks, so that
unrelated slow owner cannot repeatedly delay the already-admitted prefix.
Durable FIFO tickets are assigned transactionally when occurrences, pages and run attempts
are enqueued. Replay preserves the original ticket; attempts rotate to the tail before external
calls, surviving process death. Older unscheduled rows are backfilled on journal open before
new admission, and recreated pages receive a new ticket even when SQLite reuses a deleted ID.
Thus sustained new arrivals cannot perpetually outrank an already-enqueued retry. Actual failed
run observations have durable persistence counts, so rotation does not reset the red-CI threshold.
GitHub discovery, retry backoff and both artifact download hops share the poll's absolute
monotonic deadline. Each HTTP read additionally has a 60-second cap. The existing socket-owning
transfer primitive cancels trickling headers/body, proxy CONNECT and TLS handshake at expiry;
an idle socket timeout alone is insufficient. DNS itself cannot be interrupted, but a connection
finishing after expiry is closed before credentials can be sent. Transport errors retain fixed
diagnostics and retryable descriptors. GitHub JSON responses are capped at 16 MiB; oversized
responses fail closed rather than advancing the cursor.
Run descriptors retain branch/workflow identity for supersession, with bounded metadata
validation. Malformed report input fails with a fixed diagnostic, not raw payload output.

A done or archived intake stays closed. Fresh undelivered occurrences use the same daily
subject with the first unused `#<generation>` suffix, reusing an open generation when present.
A lost acknowledgment is reconciled against the previous card's exact payload before any
replacement, so closure does not duplicate delivered evidence. No retry threshold, cadence,
red-main handling or self-heal policy changes are part of batching.

Run attempts are replayed from the journal, so a run whose artifact is permanently
unavailable (expired, invalid, missing) would otherwise log `FAILED` on every poll. Once a
successful GitHub listing no longer includes such a run and it has reached the usual
`FAILED` threshold, it is retired with one terminal
`FAILED run=<k> code=<c> polls=<n> retired=unlisted` line and marked complete, matching the
per-test producer, which dropped runs that left the lookback listing. A failed listing never
retires anything, and a still-listed run stays `FAILED` until its data becomes readable.
Retired runs remain queryable with `SELECT repo,key FROM runs WHERE complete=1` until
terminal-history retention expires.

## Retention and nightly pins

Each production poll prunes terminal history under the journal lock. Complete runs are
retained for 30 days plus the discovery lookback (currently 6 hours), counted from their
last listing or completion. Runs with undelivered occurrences are never pruned. Delivered
occurrences are retained for the same interval after acknowledgement and until their run
record is gone; delivered pages remain until all their occurrences have aged out. Pending
runs, undelivered occurrences, and undelivered pages have no age limit. Terminal/orphaned
FIFO entries are removed without renumbering any pending ticket. Legacy rows receive the
migration time, not a guessed historical completion date. SQLite reuses freed space; no
blocking VACUUM is performed. Export before expiry if longer audit history is needed.
Replaying an artifact outside this retention window is not guaranteed deduplicated; do not
use old manual reports as a replay shortcut.

The nightly reporter's immutable export includes `nightly_e2e.py`, `flaky_kanban.py`,
`flaky_intake.py`, `flaky_http.py`, `flaky_artifact.py`, and `flaky_artifact_poll.py` from
one revision. `PIN_FILES` checks every sibling, including missing exports, and generated
re-pin instructions name them all. `hermes_cli` is intentionally **not** a partial frozen
export: the transport deadline and scope-heartbeat modules belong to the installed Hermes
runtime and its transitive dependencies. The cron launcher must expose that runtime;
verify those imports and both dispatch/report entry points when re-pinning. The pin check
certifies sibling script bytes, not hermes_cli deployment or runtime compatibility.

## Read-only backlog and export

On the producer host, set `QUEUE` to the journal's absolute path. Inspect with SQLite in
read-only mode (do not point these examples at the Kanban database):

```sh
sqlite3 -readonly "$QUEUE" 'SELECT repo,day,target_day,delivered,count(*) FROM occurrences GROUP BY repo,day,target_day,delivered ORDER BY repo,day;'
sqlite3 -readonly "$QUEUE" 'SELECT repo,key FROM runs WHERE complete=0 ORDER BY repo,key;'
sqlite3 -readonly "$QUEUE" 'SELECT id,repo,day,card FROM pages WHERE delivered=0 ORDER BY id;'
sqlite3 -readonly -json "$QUEUE" 'SELECT id,repo,identity,day,target_day,metadata,delivered,owner,page FROM occurrences ORDER BY id;' > intake-occurrences.json
```

The JSON export contains metadata, never failure output. Keep exports private with the
producer state. Count exported rows and compare with `SELECT count(*) FROM occurrences`.
Use SQLite's online backup API or `.backup` for a consistent whole-journal backup; copying
an active database file alone is not a consistent backup. Preserve the lock marker with the
backup, but never replace an actively held lock inode.

## Recovery and rollback

At cutover the journal may replay legacy cursor runs. Journal-mode owner delivery recognizes
an exact test/job citation in the owner's body as well as its comments, so initial legacy
filings do not receive redundant "Flaked again" comments. Different jobs still get comments;
the legacy non-journal helper retains its original comment behavior.

An unreadable cached local owner is evicted only when a successful complete local board
listing (including archived cards) proves its ID absent. Normal owner discovery then runs
again before intake admission. An inventory error, a listed but unreadable owner, or a stale
foreign scope never proves absence. No retry count silently abandons a known OPEN owner.
For a permanently gone cross-board/foreign cached owner, stop the producer, back up the JSON
cursor, and verify deletion on the owning gateway/profile/board through its authoritative
Board surface (not absence in the hub replica). Remove only that exact test's entry from
`state['cards']` in the JSON cursor and resume the producer. This is the manual escape hatch
for scopes the local listing cannot prove absent. Do not remove entries for merely offline
or unreadable owners; restore read access/freshness instead. Retain the deletion evidence.
Never edit the journal or its identity/lock files for this recovery. Rediscovery still holds
stale/unreadable matching OPEN replica owners pending even after cache eviction.

Loss of only the JSON cursor is replay-safe while the journal remains intact. Missing or
corrupt journal state must not be treated as an empty queue. Missing queue or anchor and a
mismatched database UUID fail closed before polling or delivery. This detects replacement by
a different valid journal, not rollback to an older backup of the same journal. Restore a
consistent backup with the matching independently retained anchor while the producer is not
running (never replace an actively held lock inode). Verify `SELECT uuid FROM journal_identity`
against the anchor. Reconcile all later source artifacts and exact delivery receipts before
resuming: a matching UUID does not prove the backup is current. Bounded card threads cannot
reconstruct undispatched occurrences. Do not claim recovery when source evidence is unavailable.

Before rollback, capture a consistent backup and export all pending run descriptors,
occurrences and pages. Reverting admission code alone does not drain the queue and can
abandon pending work. Keep a named owner responsible for draining/reconciling that snapshot
through the intake implementation, or transfer each pending occurrence with readback before
retiring it. Do not delete the journal, archive existing exact-test owners or reset cursor
history as a rollback shortcut.

## Release verification

Local fixture cardinality proves batching, not production savings. Record the reviewed head,
required CI result, landed commit and deployed producer revision separately. Read back one
natural intake and its corresponding queue/cursor records; never manufacture production
flakes. If no natural occurrence arrives, leave that proof explicitly pending. Slot-hour
reduction needs a subsequent measured window, not a projection from card counts.

## Frozen Nov23–25 intake consolidation (through occurrence 826)

This is a historical metadata/routing export, not a claim that the tests are fixed or that
the producer backlog is empty. Repair evidence, independent exact-head review, required CI
and landing remain with the named repair owners. No skip, xfail, retry or timeout policy changes.

Validated **728 occurrences (99–826), 60 distinct exact tests**, in four nonoverlapping batches:

| Frozen batch | Occurrences | Exact tests |
|---|---:|---:|
| Nov23 (99–331) | 233 | 32 |
| Nov24 (332–720) | 389 | 30 |
| Nov25 (721–815) | 95 | 10 |
| Nov25 supplemental (816–826) | 11 | 4 |

[Machine-readable frozen source and per-test dispositions](ci-flaky-intake-2026-11-23-25.json)
retains every delivered occurrence record unchanged, including original run/job/attempt,
slice, source/head SHA and owner hints. Dispositions are separate from source observations.
SHA256: `15f79e2cd1ea5786c737ab9bd4c986d5af11acccad9a86be1e5e541ec3250056`. Each batch retains its own intake disposition; later recurrences
do not become fixed merely because an earlier disposition identified a merged repair.

### Per-test routing at intake

Counts in day columns sum occurrences, not independent failures. Owner IDs denote the
canonical repair scope, not current execution or an uptime guarantee. The MoA entry retains
the owning chat’s accepted existing-merged-repair disposition; this export did not rerun it.

| Exact test | Nov23 | Nov24 | Nov25 | Supplement | Repair owner / intake disposition |
|---|---:|---:|---:|---:|---|
| `tests/acp_adapter/test_gateway_unacked_slash.py::test_repeated_prompt_after_slash_command_is_new_work` | 0 | 1 | 0 | 0 | t_01a2117deceffea3 |
| `tests/agent/test_shell_hooks_tree_kill.py::test_interrupt_kills_hook_and_propagates` | 0 | 1 | 0 | 0 | t_01a2117e043cbca0 |
| `tests/ci/test_plugin_catalog_ci_confinement.py::test_a_syncs_own_scanner_cannot_wave_a_payload_through` | 1 | 0 | 0 | 0 | t_01a2117dc0f39dbd |
| `tests/gateway/test_active_session_command_authz.py::test_a_message_during_a_refusal_after_the_owner_finished_keeps_the_held_burst` | 1 | 0 | 0 | 0 | t_01a2117deceffea3 |
| `tests/gateway/test_authority_lane_moves_on.py::test_the_app_keeps_its_old_conversation_and_the_chat_gets_none_of_its_replies` | 0 | 1 | 0 | 0 | t_01a2117deceffea3 |
| `tests/gateway/test_command_discovery.py::test_ordinary_daemon_discovery_matches_legacy_extensions` | 0 | 10 | 0 | 0 | t_01a2117deceffea3 |
| `tests/gateway/test_detached_conversation_lifecycle.py::test_a_background_result_of_the_old_conversation_reaches_the_app_not_the_chat` | 1 | 0 | 0 | 0 | t_01a2117deceffea3 |
| `tests/gateway/test_detached_conversation_results.py::test_a_result_the_chat_started_on_the_old_conversation_lands_in_it_once_the_app_used_it` | 0 | 1 | 0 | 0 | t_01a2117deceffea3 |
| `tests/gateway/test_internal_wake_never_interrupts.py::test_idle_loop_tick_runs_as_a_fifo_turn_not_beside_it` | 0 | 1 | 0 | 0 | t_01a2117deceffea3 |
| `tests/gateway/test_journey_new_session_queued_work.py::test_new_session_never_absorbs_or_drops_earlier_queued_work` | 0 | 1 | 0 | 0 | t_01a2117deceffea3 |
| `tests/gateway/test_native_profile_registry_grant.py::test_registry_grant_renames_and_deletes_only_its_own_served_profile` | 10 | 14 | 3 | 1 | t_01a2117deceffea3 |
| `tests/gateway/test_new_moves_automation_wake.py::test_new_moves_a_queued_wake_to_the_fresh_session` | 0 | 1 | 0 | 0 | t_01a2117deceffea3 |
| `tests/gateway/test_plugin_idle_task_housekeeping.py::test_after_an_in_process_restart_only_the_new_housekeeping_records` | 1 | 2 | 0 | 0 | t_01a2117deceffea3 |
| `tests/gateway/test_responses_disconnected_identity.py::test_disconnected_stream_identity_joins_the_bound_and_never_reruns` | 1 | 0 | 0 | 0 | t_01a2117deceffea3 |
| `tests/gateway/test_session_bot_retry.py::test_context_overflow_retries_once_unless_the_failed_dm_is_still_an_open_tail` | 0 | 1 | 0 | 0 | t_01a2117deceffea3 |
| `tests/gateway/test_session_deferred_reasoning.py::test_levels_picked_during_a_running_turn_apply_to_the_next_turn_in_pick_order` | 0 | 1 | 0 | 0 | t_01a2117deceffea3 |
| `tests/gateway/test_session_hosted_daemon.py::test_room_unknown_discard_releases_only_its_followers` | 0 | 1 | 0 | 0 | t_01a2117deceffea3 |
| `tests/gateway/test_session_kanban.py::test_real_dispatcher_lifecycle` | 26 | 0 | 0 | 0 | t_01a2117deceffea3 |
| `tests/gateway/test_session_mutation_model_busy.py::test_a_deferred_pick_that_raises_is_refused_and_the_queue_keeps_draining` | 1 | 0 | 0 | 0 | t_01a2117deceffea3 |
| `tests/gateway/test_session_mutation_model_busy.py::test_a_retried_deferred_pick_does_not_undo_a_newer_pick` | 1 | 0 | 0 | 0 | t_01a2117deceffea3 |
| `tests/gateway/test_stream_consumer_wecom_native.py::TestClarifyEagerReseed::test_double_clarify_boundary_reseed_chain` | 0 | 1 | 0 | 0 | t_01a2117deceffea3 |
| `tests/gateway/test_unviewed_completion_notes.py::test_completion_after_new_is_noted_on_the_closed_session` | 0 | 1 | 0 | 0 | t_01a2117deceffea3 |
| `tests/gateway/test_unviewed_completion_notes.py::test_input_admitted_after_the_completion_sees_its_note` | 1 | 0 | 0 | 0 | t_01a2117deceffea3 |
| `tests/gateway/test_unviewed_completion_resumes_parent.py::test_successful_manual_cron_run_stays_a_note` | 0 | 1 | 0 | 0 | t_01a2117deceffea3 |
| `tests/gateway/test_wake_admitted_before_drain_authority.py::test_a_wake_claimed_just_before_a_drain_runs_rather_than_settling_unrun` | 0 | 1 | 0 | 0 | t_01a2117deceffea3 |
| `tests/gateway/test_wake_admitted_before_drain_authority.py::test_authority_route_runs_a_wake_receipted_before_drain_once_after_restart` | 0 | 1 | 0 | 0 | t_01a2117deceffea3 |
| `tests/gateway/test_wake_during_restart_drain_authority.py::test_authority_route_refuses_a_wake_when_the_drain_starts_during_admission` | 1 | 0 | 0 | 0 | t_01a2117deceffea3 |
| `tests/hermes_cli/test_annotation_native_holder.py::test_foreign_reader_sessions_do_not_project` | 2 | 0 | 0 | 0 | t_01a2117dd47317fc |
| `tests/hermes_cli/test_annotation_native_holder.py::test_real_projection_idle_and_changed_source` | 4 | 17 | 2 | 0 | t_01a2117dd47317fc |
| `tests/hermes_cli/test_annotation_native_holder.py::test_wal_commit_racing_its_notification_is_confirmed` | 3 | 1 | 0 | 0 | t_01a2117dd47317fc |
| `tests/hermes_cli/test_browse_schema_drift.py::test_browse_budget_is_armed_before_the_macos_durability_pragmas` | 45 | 0 | 0 | 0 | t_01a2117dd47317fc |
| `tests/hermes_cli/test_config_save_integrity.py::test_transient_read_error_is_not_recorded_as_a_corrupt_config` | 9 | 0 | 0 | 0 | t_01a2117dd47317fc |
| `tests/hermes_cli/test_dashboard_password_command.py::test_a_restart_keeps_sign_ins_and_one_after_a_change_ends_them` | 0 | 0 | 1 | 0 | t_01a2117e043cbca0 |
| `tests/hermes_cli/test_fact_clock_only.py::test_clock_only_session_owner_advances_state_without_rows` | 0 | 8 | 0 | 0 | t_01a2117dd47317fc |
| `tests/hermes_cli/test_fact_tree_failures.py::test_native_failure_and_recovery_through_public_read` | 2 | 14 | 0 | 0 | t_01a2117dd47317fc |
| `tests/hermes_cli/test_gateway_snapshot_restore_live.py::test_snapshot_restore_rejoins_the_restored_authority` | 0 | 11 | 0 | 1 | t_01a2117e043cbca0 |
| `tests/hermes_cli/test_hf_browse.py::test_sideload_rejects_non_gguf` | 1 | 0 | 0 | 0 | t_01a2117e043cbca0 |
| `tests/hermes_cli/test_kanban_active_pr_idle_custody.py::test_hold_cap_uses_existing_visible_supervisor_escalation` | 0 | 0 | 0 | 1 | t_01a2117e043cbca0 |
| `tests/hermes_cli/test_kanban_origin_ledger.py::test_scheduled_owner_record_publishes_its_park` | 1 | 0 | 0 | 0 | t_01a2117e043cbca0 |
| `tests/hermes_cli/test_kanban_scratch_archive_retention.py::test_free_bytes_reads_the_real_filesystem` | 1 | 8 | 3 | 0 | t_01a2117e043cbca0 |
| `tests/hermes_cli/test_kanban_session_activity.py::test_hub_session_rows_do_not_invalidate_the_remote_links_projection` | 1 | 0 | 0 | 0 | t_01a2117e043cbca0 |
| `tests/hermes_cli/test_kanban_session_activity.py::test_session_kanban_tasks_endpoint` | 6 | 0 | 0 | 0 | t_01a2117e043cbca0 |
| `tests/hermes_cli/test_load_progress.py::test_loading_events_build_snapshot_and_terminal_clears` | 1 | 0 | 0 | 0 | t_01a2117e043cbca0 |
| `tests/hermes_cli/test_moa_preset_deletion.py::test_moa_save_refuses_transient_read_failure_without_losing_config` | 5 | 0 | 0 | 0 | Existing merged MoA PR4155 (intake disposition) |
| `tests/hermes_cli/test_project_plugin_rce_bypass.py::TestProjectPluginsEnvGate::test_falsy_values_keep_project_plugins_disabled` | 3 | 0 | 0 | 0 | t_01a2117e043cbca0 |
| `tests/hermes_cli/test_project_plugin_rce_bypass.py::TestProjectPluginsEnvGate::test_truthy_values_enable_project_plugins` | 1 | 0 | 0 | 0 | t_01a2117e043cbca0 |
| `tests/hermes_cli/test_session_owner_discovery_starting.py::test_a_gateway_still_starting_past_the_budget_answers_retry_not_drain` | 1 | 0 | 0 | 0 | t_01a2117deceffea3 |
| `tests/hermes_cli/test_session_owner_discovery_starting.py::test_concurrent_writes_behind_a_persisting_start_each_answer_the_starting_503` | 0 | 1 | 0 | 0 | t_01a2117deceffea3 |
| `tests/hermes_cli/test_sessions_capture_process.py::test_faults_are_typed_and_reaped` | 0 | 20 | 0 | 0 | t_01a2117e043cbca0 |
| `tests/hermes_cli/test_spawn_gateway_restart_cooldown.py::TestRepeatRestartWithinCooldown::test_a_storm_of_requests_produces_exactly_one_restart` | 16 | 0 | 0 | 0 | t_01a2117deceffea3 |
| `tests/hermes_cli/test_web_server_files.py::test_directory_listing_skips_vanished_regular_file` | 5 | 0 | 0 | 0 | t_01a2117e043cbca0 |
| `tests/hermes_cli/test_web_server_idle_proof.py::test_live_pooled_children_prove_idle_or_busy_over_the_desktop_probe` | 0 | 1 | 0 | 0 | t_01a2117e043cbca0 |
| `tests/hermes_cli/test_webapp_uploads.py::test_browser_document_upload_remains_extractable_after_file_attach` | 0 | 2 | 3 | 0 | t_01a2117e043cbca0 |
| `tests/plugins/test_board_startup.py::test_native_startup_prewarms_then_serves_without_legacy` | 2 | 0 | 0 | 0 | t_01a2117e043cbca0 |
| `tests/plugins/test_kanban_dashboard_plugin.py::test_board_card_exposes_current_run_start` | 78 | 252 | 71 | 8 | t_01a2117e043cbca0 |
| `tests/plugins/test_kanban_dashboard_plugin.py::test_bulk_archive` | 0 | 0 | 1 | 0 | t_01a2117e043cbca0 |
| `tests/plugins/test_kanban_dashboard_plugin.py::test_bulk_status_ready` | 0 | 0 | 1 | 0 | t_01a2117e043cbca0 |
| `tests/plugins/test_kanban_dashboard_plugin.py::test_bulk_status_running_rejected` | 0 | 0 | 1 | 0 | t_01a2117e043cbca0 |
| `tests/tools/test_mcp_child_filter_supervisor.py::test_a_supervisor_whose_spawn_has_not_returned_yet_is_still_not_an_mcp_child` | 1 | 0 | 0 | 0 | t_01a2117deceffea3 |
| `tests/tui_gateway/test_readiness_singleflight.py::test_probe_outliving_its_budget_answers_retryable_unknown_then_reprobes` | 0 | 13 | 9 | 0 | t_01a2117deceffea3 |

### Provenance and repair boundaries

- Nov23 intake `t_01a205ca02b80d5b`: 80 immutable pages; final archive SHA256
  `bab63f08772b37ddca39a93fe616dbfdcf6668d39109c2ae836ef690e04c0276`.
- Nov24 intake `t_01a20adbe87cad02`: 100 immutable pages; archive SHA256
  `5791093601506b34b0e555b8f9f3c5393d7efa81f3867d1f24aff6806d87748e`.
- Nov25 intake `t_01a2100379f617aa`: frozen 95 occurrences; archive SHA256
  `30a1e5a036bbce258e559bbea54ddc2eadd56ebd77ebb0c5ad51da07a5ca66e9`.
- Supplemental intake `t_01a211893144b05a`: two immutable pages; archive SHA256
  `7c7539e70f87da3e92b125d4243bd5d9b969e7ccf12c168c232358901aadc17e`.
- These artifacts are downloadable from the respective intake completion receipts.
  The journal remains backlog truth; undispatched rows are outside this frozen export.
- Board current-run-start repair [PR4390](https://github.com/softmax-universe/daveey-hermes/pull/4390)
  was published for CI/review. That is not a merged/fixed disposition for all occurrences.
  Original Nov23 first attempts show `503 board_scope_unavailable`; the repair drains only
  the full-app mounts imported by a bare-router test, preserving the foreign-home refusal.
- Board/CLI owner `t_01a2117e043cbca0` owns this single consolidation and its remaining repairs.
  Other intake owners retain their exact-test scopes. Historical closed registry owner
  `t_01a1b8f8ac4ac8e8` is not an open owner or proof that Nov23–25 recurrences are fixed.

## Frozen Nov25 supplements (occurrences 827–890)

This adds **64 unchanged observations across 5 exact tests**, without replacing the
prior 99–826 export or rewriting its historical intake dispositions. Report time is not
source time: `source_sha` and `head_sha` remain distinct. This is not a post-fix-recurrence
claim, an environmental waiver, or an assertion that the producer backlog is empty.

[Frozen source, dispositions and explicit source qualifications](ci-flaky-intake-2026-11-25-supplement-827-890.json)
has SHA256 `84014127bd58016d138246532ad4de8e732963dbb10870707c715a40c096364b`. The two original batches are 827–885 (59 records,
intake `t_01a211a4ee04cb0c`) and 886–890 (5 records, intake
`t_01a2122da94cbacd`). Every occurrence/run/job/attempt/slice/source/head field and
every original per-batch disposition is retained unchanged. Source qualifications are
separate annotations, never edits to observations.

| Exact test | Observations | Current canonical scope / boundary |
|---|---:|---|
| `tests/gateway/test_native_profile_registry_grant.py::test_registry_grant_renames_and_deletes_only_its_own_served_profile` | 3 | `t_01a2117deceffea3`; gateway repair/qualification pending; registry4406 unqualified after failed final-head sweep |
| `tests/hermes_cli/test_annotation_native_holder.py::test_real_projection_idle_and_changed_source` | 2 | Historical PR4389 repair, not an open repair owner; board-cli owns provenance/export only |
| `tests/hermes_cli/test_webapp_uploads.py::test_browser_document_upload_remains_extractable_after_file_attach` | 1 | `t_01a2117e043cbca0`; mechanism/repair still pending, including singleton830 |
| `tests/plugins/test_kanban_dashboard_plugin.py::test_board_card_exposes_current_run_start` | 50 | `t_01a2117e043cbca0`; related PR4390 landed, actual source qualification is separate |
| `tests/tui_gateway/test_readiness_singleflight.py::test_probe_outliving_its_budget_answers_retryable_unknown_then_reprobes` | 8 | `t_01a2117deceffea3`; gateway repair/qualification pending; registry4406 unqualified after failed final-head sweep |

### Latest original dashboard attempts and source qualification

- Occurrence890: run45586926211 / job139115628861, original first-attempt log
  lines1827–1836 shows `503 facts_unavailable / board_scope_unavailable`. Its actual
  source is `dd2eeb0f70ac7704d6c4434849a2222e631610da` (also its head).
- Occurrence888: run45588334044 / job139116035958, original lines1843–1852
  shows the same scope refusal. Its actual source is
  `4e0f31843c194ce8c8459ed66f830d5f78ee8801`, not reported PR head
  `fa97a06bc8756bccc97cbc75c5d29232f4aa969b`.
- GitHub contents API readback for actual source888/890 and886 found the same
  dashboard test blob `96f12286b4c8cdf297df54c84b41cee1530f8279`, without
  the imported-dashboard custody fixture or its regression. Therefore these observations
  do not establish a recurrence on PR4390’s repaired source. This is source-content
  qualification, not proof that every historical observation had an identical schedule.
- Other 47 dashboard observations retain explicit unread-source qualifications in the
  JSON. They are not silently classified as pre-fix or post-fix by timestamps.
- Annotation854/860 retain the intake’s GitHub-compare qualification: their actual
  sources diverged before merged PR4389 (`1cbd643a68267ad5b7ea0cb6191bbe907ff392c6`),
  behind10/1 respectively. The closed historical native-config repair card is not an
  active repair owner. These two observations do not prove a post-fix recurrence.

### Repair delivery snapshot (November 25, 2026, 11:56 UTC)

The earlier section’s “published for CI/review” language is a frozen historical snapshot.
The board-cli owner has since independently verified these canonical merges:

| Exact scope | Reviewed PR head | Landed commit |
|---|---|---|
| Current-run-start | [4390](https://github.com/softmax-universe/daveey-hermes/pull/4390), `472aff6164aa58c2a09bfe43566c502cd18efd10` | `4e9ce1e397f26b6ac35bff52468c0d8157f41f82` |
| Real filesystem free-byte sample | [4400](https://github.com/softmax-universe/daveey-hermes/pull/4400), `4971e859d9762d3b5cc0e7f0dfefa823edf04a36` | `5b7382b6f48424d9e3059cf549cbd5a334131762` |
| Scheduled owner park clock | [4404](https://github.com/softmax-universe/daveey-hermes/pull/4404), `06cb00704473cdece42db32c9763c32fb0c34aa2` | `8d138277145f5c87314c108c2629bfa1a551f54b` |
| Guard-hold cap clock | [4412](https://github.com/softmax-universe/daveey-hermes/pull/4412), `d27a26aa9c00331459aa95191016be45ee0f82af` | `4c1799e471f08540f88566cdd9cee986629dab7b` |
| Injected model-load progress | [4415](https://github.com/softmax-universe/daveey-hermes/pull/4415), `ffa0d0d1b496967ecf681450b1839e2e624f4fa4` | `d17d094e77e7cf6524f28d95f591861ea2b17f5f` |
| Session tasks endpoint | [4419](https://github.com/softmax-universe/daveey-hermes/pull/4419), `f0f8845e038c3d0683f66846e26c66e8080aa681` | `9ba632a68d0bd81f70d5e2cf9d3375cecf61978a` |
| HF non-GGUF, project-plugin falsy/truthy, vanished-file listing (4 exact tests) | [4421](https://github.com/softmax-universe/daveey-hermes/pull/4421), `b87ca2989c29bb8c8a89fa6018c73083c4d63929` | `fb8d369a35c3f81839b7204fa9b37509a64660c1` |

These ten exact tests have deterministic controls, independent exact-head review, green
required CI and twenty consecutive no-retry whole-file trials on each final published
head. The owner retains every raw summary/head/trial receipt. No test skip/xfail/deletion,
sleep bump, broader timeout or retry-threshold change was used. Initial metadata export
[4396](https://github.com/softmax-universe/daveey-hermes/pull/4396) landed separately as
`5afbd0da4f9dd1664f41d394ade84c5f24ef46be`, preserving 728 observations99–826.

Eleven board-cli exact tests remain incomplete: hub-session projection, native startup,
bulk archive/ready/running-rejected, document upload, password restart, shell interrupt,
snapshot restore, session capture faults and desktop idle proof. Whole-file passes or a
related merge do not qualify these automatically. The three bulk historical failures came
from source1c568929436ea8ca50c5a49b3b534b81d9f708bb, which has an earlier host-origin
websocket test importing the full dashboard before bulk. That branch-only predecessor is
absent on current main; causal qualification must not blame only the later specify test.

Gateway owner `t_01a2117deceffea3` retains its separate thirty-test scope. Authority
[4418](https://github.com/softmax-universe/daveey-hermes/pull/4418) is independently
GitHub-read MERGED, reviewed9729a47660186c3110174f503ba90fff40426409 →
landed7f7561f52e83e5f76edac498082462c5fdd17192. Its detailed twenty-round111-test
proof and review are operator-reported, not rerun by this export. Cooldown4377 and
housekeeping4397 are reported landed. WeCom4422 is reported published at
636306532bcaa24e5ca159ea070ec872a3551fbd, pending final proof/review/CI/landing.
Registry4406 remains explicitly **unqualified**, despite diagnostic passes, after a
failed final-head sweep. Supplemental887/889 retain that owner and actual source/head.

No new competing repair owners or repository exporters were created. This consolidation
does not close either repair owner, transfer its acceptance, or imply fleet deployment.


## November 25 supplemental intake — occurrences891–895

Intake `t_01a2127783e23224`. Frozen five immutable pages, five occurrences and three exact tests. Source metadata preserved unchanged in occurrences.json; dispositions are separate. No retry threshold changes.

| Exact test | Occurrences | Disposition / follow-up owner |
|---|---|---|
| `tests/gateway/test_native_profile_registry_grant.py::test_registry_grant_renames_and_deletes_only_its_own_served_profile` | 892 | source/log qualification pending; `t_01a2117deceffea3` |
| `tests/hermes_cli/test_webapp_uploads.py::test_browser_document_upload_remains_extractable_after_file_attach` | 891 | source/log qualification pending; `t_01a2117e043cbca0` |
| `tests/tui_gateway/test_readiness_singleflight.py::test_probe_outliving_its_budget_answers_retryable_unknown_then_reprobes` | 893,894,895 | source/log qualification pending; `t_01a2117deceffea3` |

Registry historical owner t_01a1b8f8ac4ac8e8 is DONE; its merged PR946 is history, not ownership of occurrence892. Current gateway owner reports related retirement repair PR4406 merged, but that does not establish source ancestry or repair coverage of occurrence892. Upload owner reports a catalog import fix in qualification; it is not acceptance of occurrence891. Readiness occurrences893–895 remain metadata-only observations. No environmental waiver or timing attribution.

Single consolidated repository docs/ci-flaky-intake.md publication remains with t_01a2117e043cbca0. This is consolidation input, not a claim to have modified repository main. Existing independent review, required CI, canonical landing and stability requirements stay on repair owners.

## Original run provenance

- Occurrence 891: https://github.com/softmax-universe/daveey-hermes/actions/runs/45600903679/job/139162679647; attempt 1; source `7f7561f52e83e5f76edac498082462c5fdd17192`; head `7f7561f52e83e5f76edac498082462c5fdd17192`; slice 10/12.
- Occurrence 892: https://github.com/softmax-universe/daveey-hermes/actions/runs/45603281098/job/139167921529; attempt 1; source `677a657f0416d24c51b6eacc017d25cdd9f53a59`; head `677a657f0416d24c51b6eacc017d25cdd9f53a59`; slice 5/12.
- Occurrence 893: https://github.com/softmax-universe/daveey-hermes/actions/runs/45607505629/job/139182863450; attempt 1; source `b4d9b696fac9ec274d3b00cc4eb0fb850898f399`; head `448e9df4e0b130c55568b722e902d82f805c9a8c`; slice 9/12.
- Occurrence 894: https://github.com/softmax-universe/daveey-hermes/actions/runs/45610929109/job/139194117964; attempt 1; source `68da7497670db04be1c6cd6e5d9cc861d954784b`; head `68da7497670db04be1c6cd6e5d9cc861d954784b`; slice 9/12.
- Occurrence 895: https://github.com/softmax-universe/daveey-hermes/actions/runs/45620064945/job/139226012827; attempt 1; source `010d18f737a000db96ed7068799873f80da2cf9b`; head `402680056ad2998fedc287d19c602a90a3d81395`; slice 8/9.


### Owner qualification update (2026-11-25 14:10 UTC)

The intake dispositions above remain immutable metadata. Owner qualification now confirms upload891
original job139162679647, lines1855–1864: `contract=None` at `session.resume`, followed by
`AttributeError: NoneType.params`. Actual source7f7561f52e83e5f76edac498082462c5fdd17192
imports the ready registry submodule without awaiting its still-initializing package catalog.
A controlled cold real-package schedule reproduces that exact failure; historical thread telemetry is absent.
The existing owner has a package-catalog barrier repair in qualification, not landed: original20 whole-file
trials27pass each/no retries and188 direct-import consumer files1367pass/0fail/1pre-existing skip.
Canonical publication changed the head to02717dd5ebbe58ac6f2de43083ef377b4fb71a3f; final-head20-run proof,
requiredCI, independent exact-head review and landing remain owed. Normal PR creation hit GraphQL quota;
no alternative identity or reserve bypass was used. Registry892/readiness893–895 remain unqualified by this owner.

Complete unchanged source records/pages/dispositions and separate qualification: [ci-flaky-intake-2026-11-25-supplement-891-895.json](ci-flaky-intake-2026-11-25-supplement-891-895.json); SHA256 `eb70c3436aeb9276ea17c2f2109c37743cc109a9d5329da7cb44190d8408943a`.


## Nov25 supplemental CI intake: occurrences 896–899

Frozen from intake t_01a213014af9133f: 3 immutable pages, 4 unique occurrences, 2 exact tests. Source records are unchanged in occurrences.json; dispositions remain separate.

This is docs/ci-flaky-intake.md consolidation input, not a claim repository main changed. Single consolidated publication owner remains @task:default@hub/t_01a2117e043cbca0. No duplicate repair or docs PR. No retry, timeout, skip or xfail policy changes.

## `tests/hermes_cli/test_webapp_uploads.py::test_browser_document_upload_remains_extractable_after_file_attach`
- Occurrences: [898, 899]; count 2
- Disposition: existing_exact_test_owner_source_qualification_pending
- Follow-up owner: @task:default@hub/t_01a2117e043cbca0
- Metadata records retries only. Preserve existing upload investigation; original first-attempt logs and actual source ancestry require qualification. No repaired claim.

Occurrence 898: https://github.com/softmax-universe/daveey-hermes/actions/runs/45632513175/job/139270242126; attempt 1; slice 11/12; source `c5f920054975306b77bf7fecbaee14f93299c635`; head `b9230afc8b31cfb53eef79c564f683fa5da7528d`.

Occurrence 899: https://github.com/softmax-universe/daveey-hermes/actions/runs/45630649293/job/139262868485; attempt 1; slice 7/12; source `5a10c083d4b1029aa366511a70afaab9046a6bc4`; head `a8b333b0617892c16c9d06bddc86af42d50fddd8`.

## `tests/tui_gateway/test_readiness_singleflight.py::test_probe_outliving_its_budget_answers_retryable_unknown_then_reprobes`
- Occurrences: [896, 897]; count 2
- Disposition: existing_exact_test_owner_source_qualification_pending
- Follow-up owner: @task:default@hub/t_01a2117deceffea3
- Metadata records retries only. Original first-attempt logs and actual source ancestry require owner qualification; no causal, environmental waiver or repaired claim. Related readiness PR4439 is reported by existing owner, not intake-verified repair of these sources.

Occurrence 896: https://github.com/softmax-universe/daveey-hermes/actions/runs/45625456728/job/139244532540; attempt 1; slice 10/12; source `88dd658a13dda085f26c5c3a7e556f2d95470561`; head `88dd658a13dda085f26c5c3a7e556f2d95470561`.

Occurrence 897: https://github.com/softmax-universe/daveey-hermes/actions/runs/45628752230/job/139256493231; attempt 1; slice 5/10; source `0aecd2644bdda46d45e934bf7446b71b1a3ab757`; head `23c690065cfe6f94d2a3b98b9b69fb238f363b44`.


## Nov25 supplemental CI intake — occurrences 900–902

Intake: `t_01a21337bf7a01da`. Repository: `softmax-universe/daveey-hermes`.
Frozen input: **2 immutable pages, 3 unique occurrences, 3 exact tests**.
This is a consolidation input for `docs/ci-flaky-intake.md`, not a competing repository edit or repair claim.
The unchanged source records are in `manifest-pages.json`; `validate_manifest.py` verifies both page SHA256 values, unique IDs, exact-test cardinality and SHA formats. `validation.json` records success.

## Dispositions and follow-up custody

| Occurrence / exact test | Observed first attempt and disposition | Follow-up owner |
|---|---|---|
| 900 — `tests/hermes_cli/test_isolated_scope_listing.py::test_real_profile_flag_isolated_serve_lists_only_its_profile` | Job 139283024204 of run 45636312398, attempt 1, slice 4/10. `occurrence-900.log:1310–1349`: test's `finally` enumerates a process family, then `p.terminate()` at test line 177 raises `psutil.NoSuchProcess` from ESRCH after disappearance. Summary 1354–1355: 1 failed, 4 passed; retry passes. **New cleanup/process-disappearance race routed for repair qualification**; not proof the isolated listing contract failed, not an environmental waiver. Retain process identity and tolerate only actual disappearance, prove with a controlled real-child schedule, retain all original assertions and strict cleanup. Historical scheduling telemetry is absent. | Existing board/CLI group `t_01a2117e043cbca0`: new follow-up routing comment **253396**. This is newly routed scope, not a claim of prior exact-test ownership or completed repair. Its owner may route a specialist through the owning chat. Hub task-body/title search found no exact match other than this intake; this is not global absence certification. |
| 901 — `tests/tui_gateway/test_readiness_singleflight.py::test_probe_outliving_its_budget_answers_retryable_unknown_then_reprobes` | Job 139280297692 of run 45634653848, attempt 1, slice 1/12. `occurrence-901.log:1821`: old `assert started.wait(timeout=2)` fails. **Pre-repair observation; preserve existing exact owner**. Compare actual `source_sha` 7f929c56a48bd9580f4ff46c7109014f92e14936 to reported landed PR4439 commit 67876b0fd11bb585442e60c57f3f1b30ed3598eb: status ahead, behind_by 0, merge-base equals source (13 commits ahead). Not a post-fix recurrence. Owner reports canonical independent review, CI, final 20 whole-file runs and merge; this intake independently checked ancestry and original symptom, not those repair proofs. | Existing gateway exact-test owner `t_01a2117deceffea3`, comment **253402**. |
| 902 — `tests/hermes_cli/test_webapp_uploads.py::test_browser_document_upload_remains_extractable_after_file_attach` | Appended operator page received during this run. Job 139293745798 of run 45637719811, attempt 1, slice 3/12. `occurrence-902.log:1869–1873`: `contract.params.model_validate(params)` raises `AttributeError: 'NoneType' object has no attribute 'params'`; summary 1938–1939: 1 failed, 25 passed; retry passes. **Existing exact-test owner, actual-source repair qualification pending**. Consistent symptom with its cold contract-registration investigation; not proof of the historical import schedule, a completed fix or post-fix recurrence. Actual `source_sha` equals `head_sha` c5131a9e7f8898cd6017a41dc6bdf6cc4569d6d1; original value retained. | Existing board/CLI exact-test owner `t_01a2117e043cbca0`, comment **253408**. |

## Consolidation and remaining work

Single repository docs/export publication stays with `t_01a2117e043cbca0`, already responsible for the adjacent supplements. Include this input with prior 891–899 evidence instead of opening another docs PR. Comments 253396 and 253408 request this consolidation. Until that owner lands it, this file is a delivered consolidation **input**, not a claim that repository docs already include 900–902.

No duplicate repair cards were created. No production code, test, retry count, timeout, threshold, skip or xfail was changed. Intake completion closes routing only. Repair qualification, independent exact-head review, required CI, canonical landing and repository export publication remain with the named open owners. Original immutable metadata status remains `needs_triage`; derived dispositions are separate and do not rewrite producer history or claim the whole journal backlog is empty.

## Provenance

- Page `8cefbfeab7d1d3706027a85d296fa9dd7c8d5d32314e0bdba690e148c646e599`: body, occurrences 900–901.
- Page `d19d706a8fb818e9d90a622dd351436c69ea7819fb327b72b0edca9c3c12fc03`: operator comment 253394, occurrence 902.
- Full unshortened body and appended page checked through `kanban_show(verbose=true)` during this run.
- Original logs retrieved directly with `gh run view RUN -R softmax-universe/daveey-hermes --job JOB --log`, not fabricated or reproduced output.
- Owner lookup used exact named card readbacks and bounded authoritative hub task-body/title search; replica list calls were refused as worker-restricted and did not establish absence.


### Owner qualification snapshot (2026-11-25 15:26 UTC)

All seven source records896–902, five immutable pages and both original disposition sets are preserved unchanged. Counts: upload3, readiness3, isolated-profile cleanup1; three exact tests.

Uploads898/899: original jobs139270242126:1873–1882 and139262868485:1889–1898 report `contract=None` followed by `None.params`. Upload902 original job139293745798:1869–1873 reports the same validation failure. All three actual source revisions lack the package-catalog import barrier. These are pre-repair source observations, not post-fix recurrences or observed historical thread schedules. Existing repair PR4446 at02717dd5ebbe58ac6f2de43083ef377b4fb71a3f has original/final20 whole-file runs27pass each; required CI, independent exact-head review and landing remain owed.

Isolated-profile900 is a fixture cleanup race: original job139283024204:1310–1349 raises `psutil.NoSuchProcess` at `p.terminate()`. Actual source has unsuppressed cleanup. Controlled real owned children reaped immediately before census/terminate/kill produce three original failures; narrow disappearance handling preserves permissions errors and the original profile-list assertion (whole file9pass). Commit0e16b671d45210180d294bdd1eea6b047d9c1a3a is under20-run qualification, not published or landed. No environmental waiver, skip or retry change.

Gateway readiness status is owner-reported here: PR4439 landed67876b0fd11bb585442e60c57f3f1b30ed3598eb with independent review/CI/20x6. Actual sources896/897 lack that repair per owner; intake independently checked901 ancestor/pre-fix. The original frozen intake dispositions above are retained, not retroactively rewritten.

Complete unchanged inputs and separate owner annotations: [ci-flaky-intake-2026-11-25-supplement-896-902.json](ci-flaky-intake-2026-11-25-supplement-896-902.json); SHA256 `b176eecd27f60953db49323aa504bc2e1ac4b6977ffa9b546f8cbe1fa7aefaef`.


## Nov25 supplemental intake: occurrence903

One immutable source record, one page and one exact test are preserved separately in [ci-flaky-intake-2026-11-25-supplement-903.json](ci-flaky-intake-2026-11-25-supplement-903.json). SHA256 `d0a68c2c056740cb435d097f8921ff5f934f88a756691b12b41ab7085f7b0348`. Prior exports and historical totals above remain unchanged; combined supplemental range891–903 contains thirteen records.

Source run45644573502 attempt1/job139312608655, actual source68ff324cb940486f0923391c7f9105f466d97369; exact test `tests/tools/test_mcp_child_filter_supervisor.py::test_an_owned_spawn_racing_mcp_discovery_does_not_hand_mcp_the_supervisor`. Frozen disposition: `existing_owner_pending_review_and_landing`; repair owner t_01a2117deceffea3. No fixed or environmental disposition is inferred.

The intake reports ordinary-child inclusion failed before positive-control exec argv readiness, not supervisor exclusion. The repair owner reports source qualification, deterministic RED/GREEN and twenty whole-file rounds at PR4445 head5fbe628f97586c84f653769eb87966db69506c26. Those proofs were not rerun by this docs consolidation. Historical argv scheduling is unknown. At the frozen intake snapshot fresh changed-head review, CI acceptance and canonical landing remained owed, and that intake granted no release of its reported decision/spend hold. These historical dispositions are preserved unchanged, not assertions of current execution or landing.


## November 25 supplemental intake — occurrences904–916

# CI intake 2026-11-25: occurrences904–916

Frozen 10 immutable pages; 13 unique occurrences; 4 exact tests. Metadata observations remain unchanged. Intake closes routing only, not repairs. No timeout/retry/skip/xfail policy changes.

Canonical docs export remains with `t_01a2117e043cbca0`; add this as a supplement to `docs/ci-flaky-intake.md`, not a rewrite of earlier frozen batches. Repository publication is pending that existing owner.

## tests/gateway/test_native_profile_registry_grant.py::test_registry_grant_renames_and_deletes_only_its_own_served_profile

Occurrences: [904, 910, 911]; count=3.
Disposition: **existing_owner_unresolved_recurrences**.
Follow-up: `t_01a2117deceffea3` (triage; decision_pending/spend hold; not released).

Original910 log lines1905-1913: PATCH rename succeeds, gamma still exists, assertion line102. Metadata904/911 retained without independent log qualification. Historical closed owner t_01a1b8f8ac4ac8e8 repaired an earlier writer in PR946; not a new occurrence owner. Exact historical filesystem writer remains unknown. Gateway owner retains remaining repair/source qualification.

## tests/hermes_cli/test_webapp_uploads.py::test_browser_document_upload_remains_extractable_after_file_attach

Occurrences: [907, 913]; count=2.
Disposition: **existing_owner_merged_related_repair_source_qualified**.
Follow-up: `t_01a2117e043cbca0` (running).

Original913 log lines1875-1879: contract.params.model_validate(params) raises AttributeError because contract is None. PR4446 independently GitHub-read MERGED at9e8355668e0bef82a72d48fe8958a86ee8b2e34e. Actual source913 is ancestor of landing (behind5); source907 diverged before landing (behind14/ahead1), so neither establishes a descendant post-fix recurrence. Source907 patch-content coverage and original log remain unread. Existing owner retains causal repair proof; intake does not independently reproduce import ordering or declare every observation fixed.

## tests/hermes_state/test_session_status_checkpoint_fences.py::test_structured_receipt_outranks_stale_checkpoint

Occurrences: [915]; count=1.
Disposition: **existing_PR4478_owner_first_attempt_failure**.
Follow-up: `t_01a2143fcfb27f95` (running).

Original915 PR4478 merge-source log lines1116-1139: [3-unresolved] returns done instead of unresolved at line51; isolated retry lines1142-1156 passes. Existing PR4478 author t_01a2143fcfb27f95 owns exact added test/receipt precedence. Original manifest node ID remains unparameterized; log suffix is annotation only. Mechanism not established; qualify before landing. Direct comment143545 readback requested.

## tests/tui_gateway/test_readiness_singleflight.py::test_probe_outliving_its_budget_answers_retryable_unknown_then_reprobes

Occurrences: [905, 906, 908, 909, 912, 914, 916]; count=7.
Disposition: **existing_owner_post_PR4439_recurrence_confirmed**.
Follow-up: `t_01a2117deceffea3` (triage; decision_pending/spend hold; not released).

Actual sources914=97a2682811aa3487e10d49fc7f4f339f60a9fac6 and916=b0a91e3585298c85071a9dac86b9ee16f6820437 descend from landed PR4439 67876b0fd11bb585442e60c57f3f1b30ed3598eb (behind0). First-attempt logs914 lines1856-1861 and916 lines1299-1304 fail assert started.is_set() at line210, not old started.wait(timeout=2). Source916 confirms queued_probe parameterized fixture. This is a recurrence on repaired source, not delayed pre-fix metadata; causal mechanism unproven. Other five sources remain individually unqualified. Preserve existing held owner; this note grants no execution/budget release.

## Evidence boundaries

Raw original logs: readiness914/916, upload913, registry910, checkpoint915. Other individual sources/logs are explicitly unqualified, not silently presumed pre-fix or same-cause. Original filesystem/scheduler/contract import telemetry is unavailable in these observations.

No new repair cards, no ownership transfers, no release of gateway decision/spend hold. Downloadable JSON contains every run/job/attempt/slice/source/head field and original owner hints.

### Subsequent owner qualification (append-only; original input above unchanged)

Upload907 independently fetched original run45680546732/job139440104309: lines1806–1838 show an existing legacy `SessionResumeResult` rejecting the canonical result (`info.title=None` and eight extra canonical fields), **not** `None.params`. Its actual checkout is `b5898d1fea1176e3067b7c8047861a728268939a` (checkout receipt line168). Both913 actualsource `97a2682811aa3487e10d49fc7f4f339f60a9fac6` and907 lack the package-completion import barrier landed by PR4446; relevant contract/package definitions are byte-identical to that landing. Fresh913 original lines1875–1879 confirm missing-contract `None.params`. Neither observation establishes a descendant post-repair recurrence.

A controlled import held **after** legacy sessions registration but **before** canonical registration reproduces907’s same `SessionResumeResult`/`extra_forbidden` class when only the landed barrier is reverted; restoring it passes the complete28-test upload file. This proves the additional product susceptibility, not the unrecorded historical thread schedule. Additional permanent ordering regression is published in [PR4484](https://github.com/softmax-universe/daveey-hermes/pull/4484), pending final twenty-run qualification, independent review, required CI and canonical landing at this snapshot. Existing PR4446 remains independently verified merged at `9e8355668e0bef82a72d48fe8958a86ee8b2e34e`; previous completed proofs are preserved, not rerun.

Readiness914/916 remain **POST-REPAIR RECURRENCE, cause unknown** per intake-qualified actual source and repaired assertion. Registry904/910/911 and readiness905/906/908/909/912/914/916 retain gateway owner `t_01a2117deceffea3` and its decision/spend hold; this supplement grants no release. Checkpoint915 retains existing PR4478 owner `t_01a2143fcfb27f95`. Other individual logs remain unqualified exactly as original input states. No competing repair owners, retry/timeout changes, skip or xfail.

### Original run provenance904–916

- Occurrence 904: https://github.com/softmax-universe/daveey-hermes/actions/runs/45668265235/job/139396862002; attempt 1; source `c4d8b9c8dcf858a6408a192a3d6e73c4b01f34b0`; head `88e587feb641ad61fa63536e6e5d178b9e505ee3`; slice 9/12; observed_day 2026-11-25; status needs_triage; retried tests; owner_check [{'candidate': 't_01a1b8f8ac4ac8e8', 'reason': 'closed_history_incomplete', 'status': 'done'}].
- Occurrence 905: https://github.com/softmax-universe/daveey-hermes/actions/runs/45677703052/job/139430092531; attempt 1; source `6f6b4201087267b3d4beb87b380e839b28977430`; head `273abc82b89a0bdd6804c54d5956118091e0107b`; slice 12/12; observed_day 2026-11-25; status needs_triage; retried tests; owner_check [].
- Occurrence 906: https://github.com/softmax-universe/daveey-hermes/actions/runs/45679711209/job/139437644775; attempt 1; source `b6e636ac96b08d4864acc69adc93350fa079790d`; head `8f71956e7dd3386b922e0338884b2c141341d4b4`; slice 3/12; observed_day 2026-11-25; status needs_triage; retried tests; owner_check [].
- Occurrence 907: https://github.com/softmax-universe/daveey-hermes/actions/runs/45680546732/job/139440104309; attempt 1; source `b5898d1fea1176e3067b7c8047861a728268939a`; head `2b49dbf72c9ec285f9fc2bb161a10ecf5efd17b4`; slice 10/12; observed_day 2026-11-25; status needs_triage; retried tests; owner_check [].
- Occurrence 908: https://github.com/softmax-universe/daveey-hermes/actions/runs/45685512540/job/139457463435; attempt 1; source `c5353d888e017723ac80ebec03c1892708db7bf6`; head `df6df9311474ea889fccb4e1e480762b0085ffb4`; slice 12/12; observed_day 2026-11-25; status needs_triage; retried tests; owner_check [].
- Occurrence 909: https://github.com/softmax-universe/daveey-hermes/actions/runs/45687071801/job/139462888984; attempt 1; source `00e0e74f07c3e10f3d2b11a2cc2ae3efad77e54b`; head `70d99a537fae8d86b9788a791a0351780fa4d195`; slice 1/12; observed_day 2026-11-25; status needs_triage; retried tests; owner_check [].
- Occurrence 910: https://github.com/softmax-universe/daveey-hermes/actions/runs/45686951716/job/139462423521; attempt 1; source `674e70951123c85860f2f05c80c575d7d5912c21`; head `ad4df1f3dcd1383c6d0b11dde4b38510a5d7c5b3`; slice 10/12; observed_day 2026-11-25; status needs_triage; retried tests; owner_check [{'candidate': 't_01a1b8f8ac4ac8e8', 'reason': 'closed_history_incomplete', 'status': 'done'}].
- Occurrence 911: https://github.com/softmax-universe/daveey-hermes/actions/runs/45686924650/job/139462381457; attempt 1; source `18862a2a3e34ba17d6275cda04e57af1bc625175`; head `a0a77addfacb0898b23e3e79e5626a61d54134ec`; slice 4/5; observed_day 2026-11-25; status needs_triage; retried tests; owner_check [{'candidate': 't_01a1b8f8ac4ac8e8', 'reason': 'closed_history_incomplete', 'status': 'done'}].
- Occurrence 912: https://github.com/softmax-universe/daveey-hermes/actions/runs/45695722693/job/139493028425; attempt 1; source `1bfeff84ea1e69d3dc0884a7e13249a987236b60`; head `12c50c4a721304cb17187a4fb2241731346033a8`; slice 11/12; observed_day 2026-11-25; status needs_triage; retried tests; owner_check [].
- Occurrence 913: https://github.com/softmax-universe/daveey-hermes/actions/runs/45700200503/job/139512282705; attempt 1; source `97a2682811aa3487e10d49fc7f4f339f60a9fac6`; head `97a2682811aa3487e10d49fc7f4f339f60a9fac6`; slice 1/12; observed_day 2026-11-25; status needs_triage; retried tests; owner_check [].
- Occurrence 914: https://github.com/softmax-universe/daveey-hermes/actions/runs/45700200503/job/139512282519; attempt 1; source `97a2682811aa3487e10d49fc7f4f339f60a9fac6`; head `97a2682811aa3487e10d49fc7f4f339f60a9fac6`; slice 3/12; observed_day 2026-11-25; status needs_triage; retried tests; owner_check [].
- Occurrence 915: https://github.com/softmax-universe/daveey-hermes/actions/runs/45703651162/job/139520417730; attempt 1; source `d84329e7aea74940921c62887b51dd7b6a495883`; head `29fcb82ab298ddb36ed5a4590f4326fa4fd62e8b`; slice 2/2; observed_day 2026-11-25; status needs_triage; retried tests; owner_check [].
- Occurrence 916: https://github.com/softmax-universe/daveey-hermes/actions/runs/45706485481/job/139530190320; attempt 1; source `b0a91e3585298c85071a9dac86b9ee16f6820437`; head `9c74365e4ff2eb2c936018c8a9edfd8fa3530c3c`; slice 4/11; observed_day 2026-11-25; status needs_triage; retried tests; owner_check [].
