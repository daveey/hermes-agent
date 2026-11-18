# Daily CI flake intake

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
