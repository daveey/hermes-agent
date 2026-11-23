# Shared facts v1 — implementation checkpoint (NOT delivery)

Authoritative review: task t_01a20518ba6edfda, attachment review.md SHA256
621fb9041060b3e08c1cb1623d5eaacd2b25b28b0366b5bce543b4a785109853.
Coordinator/producer: t_01a20518eff1c5e4. No live serve signals.

## First-ship priority update — creator, 2026-11-23 00:53 UTC
The mitigation is reported live but total voluntary context switching remains
124,877/s in the creator's later window. First ship must move request-path
board/projects (e), sessions (b), and the remote-links refresher, not merely d
background annotation work. The panel-card threads are short-lived per-session
`board_lane._read -> feed.compute` expansions, not one permanent producer.
`kanban_links_refresh -> _refresh_links_scope -> _project_remote_links` is a
separate remaining hot lane. Do not duplicate or further tweak the mitigation.
No first-ship PR exists at this checkpoint; the earlier ETA is not a delivery claim.

### Native goal/cron annotation slice (additive, not activation-ready)
- `AnnotationFacts(..., view_facts=True)` explicitly reads family-named ViewFacts;
  default SessionFact mode is preserved. `selector_for(scope,row,display=...)`
  retains profile, lineage order/root and display variant; demand and lookup use
  identical keys. Malformed selectors, unsupported families and exhausted demand
  capacity fail retryably, not with legacy I/O or raw request exceptions.
- `fact_annotation_views.projections()` currently registers ONLY goal and cron.
  No other annotation family is falsely certified. Full d activation still needs
  the remaining five families and startup readiness must enforce that coverage.
- `AnnotationMembershipSource(engine, SourceId(scope,'annotations.goal'|'annotations.cron',origin),path)`
  captures an authorized profile's real state_meta or jobs.json, uses native file
  hints and a 30s recapture deadline, and publishes bounded immutable membership
  input on the SAME ContentOwner feed via `operational=[...]`. No discovery,
  second content owner, retained DB connection or request capture is added.
- Goal reads use a tracked read-only transaction with key/byte/time caps. Cron
  absent/empty file means canonical no jobs; malformed JSON and other I/O failure
  remain unavailable. Missing goal DB remains unavailable. Periodic successful
  recapture renews evidence (90s validity, >=30s scheduling margin); repeated
  content-free hints do not churn revisions. Source failures preserve last-good
  timestamps, and successful real recapture heals existing accepted demands.
- Native tests run actual SQLite/file mutation through source, feed, reducer,
  real in-process scoped handle and decorated annotation requests, checking
  canonical rendered parity, lineage variant separation, old snapshot isolation,
  request collector tripwires and malformed-file stale/recovery. Six-file focused
  run:49 tests passed, no retries. Three audit regressions reproduced RED then
  fixed (demand exception normalization, absent cron inventory, renewal margin).
  A bounded component audit is NOT final exact-head ship review.
- Remaining: five annotation captures, all native b/e/panel materializers,
  links-refresher integration, authorized serve startup, actual rendered live
  parity, review/CI/ship/natural-deploy CPU measurement. Demand eviction remains
  deferred with a hard4096 selector cap; caller gets retryable unavailability.
  No activation, deployment, PR or live signal.

## URGENT scope supersession — David, 2026-11-22 23:36 UTC
This later user steer overrides the coordinated-first rollout requirements below:
- Ship consumers incrementally behind per-consumer switches, d/e first; others
  remain legacy. Do not wait for all consumers or the distributed latch.
- First runtime runs ChangeFeed/FactStore in serve. Dedicated host/IPC activation
  is deferred, not a first-ship prerequisite.
- Rendered-field parity is the first-ship gate; exhaustive byte parity follows.
- One independent exact-head review plus required CI; re-review correctness fixes,
  not polish. Always canonical hermes-core-wt ship and natural updater; NEVER signal serve.
- External PR/git/config sources may use bounded 30–60s refresh initially. Record
  each actual timer in the follow-through ledger; no false <1s freshness claim.
- Published task/session minimum source coverage first; other adapters follow.
- Do not duplicate parallel serve-urgent-mitigation (queue position removal,
  refresher coalescing, shared notifier fingerprint).

One cleanup/follow-through owner: t_01a205a0c8c55154, after this first-ship card.
It retains every deferred guarantee and must distinguish planned shortcuts from
those actually shipped. Final receipt here must identify actual shortcuts/flags.
At the current checkpoint the in-process transport and independent per-consumer
release controls are implemented and fixture-tested, but NOT mounted in serve.
Existing d/e/b/f integration remains inert, not source-driven production.

### In-process milestone (a43b7f13d0 + source2bea integration)
- `fact_inprocess.InProcessFacts(store, authorized_scopes, demand=enqueue)` owns
  sleeping delivery and independent validity-expiry workers. `.acquire(scope,
  lifecycle=authority)` returns the same ScopedHandle API without IPC/process
  spawn. Startup must provide already-authorized scopes; no request discovery.
- `IncrementalLifecycle(expected={(scope,name),...})` reuses one ProcessLifecycle
  per consumer. `transition(name,scope,mode,generation)` requires explicit v1
  readiness, drains/rolls back ONLY that consumer, never auto-activates another.
- Local subscription cancellation is nonjoining (consumer transition locks may
  also be needed by callbacks). Handle close fences pending/running callbacks
  outside consumer locks, retains custody on timeout, and refuses self-close
  from the delivery thread. Only locally-owned subscriptions are drained; IPC
  subscriptions sharing a store are untouched. Failed callbacks back off .1–5s;
  contended delivery locks arm a retry rather than spinning. Readers refuse if
  either worker dies. Slow callback delivery cannot postpone validity expiry.
- Five audit counterexamples were reproduced as five failing tests, then fixed;
  additional contention/dead-worker/cancellation tests pass. Seven canonical
  annotation-family fixtures and three board exported-route/graph/delete/health
  fixtures now also pass through the real in-process transport and production
  per-consumer release control (no FakeStore or fake lifecycle). Fixture data
  publication is explicit: NOT native-source→consumer or startup proof.
- Final focused run: 5 files/36 tests passed. After merging source milestone
  2beaacd45dda8ee2277926e3079c87087353f764: 31 files/160 tests passed, no retries.
  This selection includes all test_fact_* plus local d/e parity and session
  content-change tests; do not sum overlapping runs. Full checks passed before
  source merge; final integrated checks must be rerun before publication.
- Minimum source branch now integrated: bounded task/session bootstrap, closed
  dirty replay, restore fences, large (>2000 rows/>4MiB) captures and revision
  recovery. This does NOT deliver external/operational source adapters.
- NEXT: actual source commit/materializer bindings and authorized serve startup
  for d/e, then rendered-field fixture/live snapshot parity, exact-head review,
  required CI/canonical ship and natural-deploy measurement. No live flags,
  background jobs, PR, deploy, or serve signal have been issued.

### Native content publication increment
- `fact_content_owner.ContentOwner(engines_by_scope, ContentBinding[] )` now
  composes task/shadow/session adapters with ONE ChangeFeed owner thread and
  `fact_source_commit.SourcePublisher`. Source scope/kind/duplicate validation
  happens before thread creation. Trusted startup supplies paths; no discovery,
  serve mounting, consumer activation or dynamic demand queue is implied.
- Default hydrators in `fact_content_rows` read every task scalar except the
  canonical cursor-excluded bookkeeping columns, and all session scalar columns
  under listing+content coverage. SQL remains inside guarded capture transactions,
  id batches <=500. Related task tables/operational fields are NOT included.
- `TaskContentInput` / `SessionContentInput` keys use JSON
  `[source.kind,source.origin_id,row.id]`; they are intentionally not the completed
  consumer TaskFact/SessionFact schemas. `SourceMembership` and `SourceState`
  keys use JSON `[source.kind,source.origin_id]`. Membership value is a tuple of
  namespaced FactKeys; state carries source_kind/origin_id/cursor_state and a
  canonical JSON revision_token. The actual source cursor/provenance and changed
  rows/deletions/dependent outputs commit in one reducer/store generation.
- Capture before must match last committed cursor. Failed store publication
  cannot advance private cursor/membership; retry accepts the same capture.
  Source failure retains stale last-good rows; before first success state remains
  unknown, including repeated failure. Empty-delta retry recovers retained rows.
  Heartbeat-only writes publish no facts. Content-only deltas retain membership
  object/index rather than rebuilding every row key. Actual membership changes
  still rebuild the source membership tuple (not a constant-time membership claim).
- Added native tests cover both paged task/session bootstrap with max_keys=1,
  real writers -> owner -> local transport -> canonical session assembly, cursor
  and view same-generation, retained snapshots, scoped local/shadow collisions,
  deletes, only-dirty dependent reduction, failure/retry and startup custody.
  Selection/count/annotation inputs to the canonical session assembly remain
  controlled; this is NOT full route or d/e native-source parity.
- Bounded independent audit found repeated initial failure incorrectly becoming
  stale. Regression failed on actual implementation (4 pass/1 fail), then fixed.
  This component audit is NOT final exact-head ship review.
- NEXT: register actual d/e materializers (including non-scalar inputs), owner
  demand scheduling and authorized request-local serve startup. Do not replace
  these with fixture publications. Rendered live parity/review/CI/ship/natural
  deployment and feed-thread measurement remain open.

Latest integration verification: b514d37df cherry-picked as83c6ea2f8d fixes exact
canonical manifests and adds request-local sessions use(binding). Full selection
94 files/743 tests passed, no retries. f6019bef rebased host milestone merged;
39 notification/remote/fanout files438 tests passed, no retries. These overlap
other runs; do not sum them as one suite. Full check21 passes. Mount reentry fix
1dc85a8d allows drained legacy-only ASGI reuse; authorized/failure fences remain.

## Parallel owners
Creator adopted producer split (no acceptance relaxation):
- source capture/adapters/watch/writer revisions: t_01a20567541fa55b (`wt/changefeed-sources`)
- host/store/IPC/replicas/reset paging: t_01a205676ff2a292
- gateway/unloaded scheduling: t_01a205678a70dbcb; also owns
  `gateway/kanban_watchers_notifier.py` and `tui_gateway/kanban_unloaded_gates.py`.
- coordinator retains shared contract/FakeStore, materializers/manifests, startup/auth,
  parity, latch, integration and final review/ship/acceptance. No duplicate owners.
- b sessions/sidebar/stamps: t_01a2053d2c9d727b
- c mods session-panel: t_01a2053d2f43d50e
- d annotation integration: t_01a2053d3177d394
- e board/projects/tree: t_01a2053d33aee6a6
- f notification readers/durable executors: t_01a2053d36138399

All run against this shared fake now; no completion edges hold their offline builds.
They must not commit copied producer modules. Compatible inert landing only; all
production source coverage, prewarming and global readiness required before cutover.
Keep legacy implementations for tested rollback; stop their read loops in facts mode.

## Frozen seams (some NOT IMPLEMENTED yet)
Implemented types in fact_contract, store in fact_store, FakeStore in fact_fake:
- Scope(install_id, profile_id, board_id=None); SourceId(scope,kind,origin_id)
- FactKey(scope,kind,id); authority-qualified IDs supplied by source binding
- Fact(key,value,rev=0,source_vector=(),observed_at=0,valid_until=None,
  health='unknown',error_code=None). Values deep frozen mappings/tuples.
- Position(epoch,rev); Ready(encoded,epoch,rev); Pending(reason); Unavailable(reason)
- Delta(epoch,prev,rev,upserts,deletes,reset,health)
- snapshot(scope).get -> Fact|None, get_many -> tuple; view(name,selector_mapping)
- key/position/view_key constructors; Ready/Pending/Unavailable classes on v1
- subscribe(scope,kinds,keys,after,callback), explicit drain(), close registration
- changes(scope,after,kinds=(),keys=None), scoped reset for evicted/future/wrong epoch
- fake bind_consumer(name,scope,on_mode), report_ready(name,scope,version=1,ready=True)
- fake activate(scope), rollback(scope): all five consumers required; failed transition
  rolls back attempted callbacks; NOT the production distributed release latch.

NOT IMPLEMENTED: fact_host.acquire(authorized_scope)->ReplicaHandle with .v1,
.close(), .demand(view,selector). Acquisition startup only; demand memory queue to
owner. ctx.facts.scope authorized scope and ctx.facts.handle scoped handle. v1
register_source(adapter)->Registration forwards plugin source registration to host;
no core imports of plugins. Consumer callback on_mode(mode,generation) off request
thread; fail closed on loop-stop timeout and reverse already transitioned consumers.

Request profile binding: consumer-specific context-local use(binding), supplied by
startup/middleware authorized registry; no process-global single binding. Scope
lookup/default-board mapping is memory only. Missing facts-mode binding -> unavailable,
not None/legacy fallback. None means explicitly selected legacy mode only.

Canonical view names: sessions, sessions_sidebar, sessions_stamps, panel_status,
panel_bundle, board, boards, projects, tree, project_sessions, annotations. Selector
mapping retains every original parameter/default; scope separate. No projection
field alternative under sessions.

SourceState reserved internal FactKey kind carries {source_kind,origin_id,
cursor_state,revision_token} for durable-source-only wakes even if user views do
not change. Ring only wakes action subscribers, never ACKs or seeds durable cursors.
Source state health/reset/removal and authority are explicit.

## Latest process participant increment (NOT global activation)
Recovered c08126b6 from the interrupted predecessor rather than replaying the
older continuation. Integrated host91d0d2fd, dbeecd0bf, eb6d343c, plus exact
incremental d9129c16c and e78dee863 real-host milestones by cherry-pick (their
new branches rebased shared history; old branches intentionally retained).

`fact_runtime.ScopedHandle(replica, participant)` now supplies a scope-checking
v1 facade with the missing bind_consumer/report_ready methods, plus context-bound
replica callbacks. `fact_participant.ProcessLifecycle(expected={(scope,name),...})`
is an explicitly process-local participant driver, NOT a distributed release
coordinator. Real AnnotationFacts runs against it and actual FactHost/IPC in tests.
- Lifecycle callbacks retain the binding ContextVars, never the control thread's
  profile. Readiness reports never activate. Unknown/unbound/foreign scopes refuse.
- Failures rollback all callbacks with a newer generation; failed rollback retains
  custody and attempts other callbacks. Closing bound handles refuses; failed
  replica close fences new operations and permits retry. Reentrant transitions
  cannot regress release generation.
- v1 readiness reports lack an incarnation parameter. Retired slots are therefore
  NOT reusable within a participant capability; new startup/roster requires a new
  participant and handle. This prevents old reporters/registrations certifying or
  removing a successor. Failed initial binding keeps cleanup custody, recoverable
  with explicit successful legacy transition then unbind_consumer.
- This module does NOT certify prewarmed views, derive the authoritative process
  roster, fence cross-process requests, or provide generation-qualified readiness
  leases. The real global latch MUST do those before invoking this participant.
  Do not expose transition as a plugin or request API or call it to enable live
  facts. No config/startup activation is wired by this increment.
- Internal bounded audit reproduced stale-registration/readiness, close/bind and
  nested-generation hazards. Regression tests address them; not ship approval.
- Consumer b's forthcoming request-local `sessions_fact_routes.use(binding)` must
  be wired by trusted middleware, not the old process singleton.
- c wrapper shape confirmed via operator184: module.bind_facts(ctx, **kwargs)
  consumes ctx.facts.v1/scope/handle and returns an exact-installation synchronous
  closeable. Owner-side plugin source/projection registration remains unpublished.

## Latest integration increment (not production compatibility)
Coordinator b351d45f6e integrates host5a34880c10, sourceb3d5fd14,
bc9a8332842 and dc8b59ddcd, plus efbe4000c15.
- Real session content mutation now traverses native source -> ChangeFeed ->
  canonical `assemble_sessions` -> atomic ViewFact -> actual host IPC -> scoped
  replica -> strict parity in test_fact_session_pipeline. Selection, annotation,
  totals and diagnostics are controlled fixture inputs, NOT full route/live parity.
- `fact_plugin_mount` supplies app-local exact-API-path startup bindings:
  `lifecycles(app).authorize(api_path, ctx, kwargs)` then trusted mount invokes
  `bind_facts(ctx, **kwargs)`; absent binding invokes optional `start_legacy()`.
  Both return a synchronous closeable handle. Existing dashboard trust gates
  remain before import/start. app_lifespan closes hot-mount handles off-thread.
  Failed drains retain custody; partial router publication rolls back; closed
  lifecycle reentry refuses before serving. Startup hooks unwind their own
  partial setup when they raise before returning a handle.
- This is NOT the plugin producer registration API, native profile auth registry,
  hosted plugin lifecycle integration, or production startup binding. Those remain.
  Raw ReplicaV1 lacks bind_consumer/report_ready; coordinator still owes authorized
  lifecycle facade plus all-scope latch, never manual consumer activation.
- Renderer/JSON failures now publish unknown/projection_failed for only that view;
  dependency discovery errors still abort the generation (known remaining gap).
- Internal audit found partial router and closed lifecycle reuse bugs; both
  reproduced RED then fixed. Not final exact-head review.
- Integrated b manifest AST checks currently fail 7 cases after d extraction;
  b owner asked to retarget actual pure canonical helpers, preserving exhaustive
  assertions (operator58/62 applied). Host5a348 resolves health/footgun blockers,
  adds separate demand IPC, owner expiry and 48MiB atomic generation budget.
  Final coordinator suite: 119 tests / 25 files pass, including host pressure,
  real session pipeline, all fact modules and board/native route lifecycle tests.
  Full scripts/check --base beba674d9995 passes all21 checks; six raw-thread
  advisories remain. This excludes the known failing b/d manifest tests.
- Future gateway rev7 boundary: dispatcher consumer alone may own
  gateway/kanban_watchers_dispatcher.py + new gateway/kanban_dispatch_facts.py
  and dedicated tests after independent plan approval. No shared producer edits.
  C7-C9 closeout coverage DECLINED as accepted compatibility (not implemented or
  writer-parity proven); rev7 proceeds tick-only unless a later explicit milestone
  proves those inputs. Existing operational producer coverage stays required.
- e continues bounded canonical extraction/schema milestone; aggregate integration
  and release remains coordinator-owned, no duplicated release card. Source owner
  may make narrow fact_feed CapturePending continuation edits with fairness,
  private staging and no cursor/publication until final barrier.

## Coordinator increment (not production integration)
`fact_parity.compare_views` compares an explicit list of expected JSON responses
against one pinned Snapshot. Empty comparison cannot pass; strict scalar types,
field presence, order, nulls, stale/missing and invalid/duplicate JSON are checked.
No payload values appear in diagnostics. This is a comparator primitive, NOT the
complete artifact loader, mutation harness, or coherent live-hub capture proof.
`fact_materializers.view_reducer` bridges registered pure canonical renderers to
ViewFact encoding in the atomic reducer generation; unknown view demands are
refused before binding. Missing/stale/expired declared inputs prevent rendering.
Consumer owners are extracting real renderers; NONE is production registered yet.
Expiry still needs owner deadline invalidation (Snapshot.view does not read a clock).
Optional absent reads need an explicit membership/known-absence contract; currently
all declared dependencies are required, conservatively unavailable if absent.
Serializer exceptions still abort a source batch; per-view error isolation is pending.
Short read-only audit found nested frozen selector serialization and unknown-demand
poisoning; both reproduced RED and fixed. Strict integer/float comparison is retained
intentionally for the review's exact typed legacy parity, not treated as an exception.
FactKey/SourceId/Scope values are now accepted by freeze after immutable identity
validation, allowing reviewed parents/chat-links/source-vector schemas.
Integrated source milestones through 6848e4ba80ae379195d1182d892625430e23f1a4:
budget+1 bounded dirty cursor reads plus separate transactional message/counter
clock. This does not supply paged FULL/watch/adapters/restore continuity.
Integrated d67ff39f1e55147e28185adb7ec077b4be79e4a79: six pure canonical
annotation seams plus retained lifecycle custody. A controlled goal fixture now
passes legacy entrypoint -> same canonical projection -> atomic ViewFact -> strict
parity comparator, with legacy capture forbidden during materialization. This is
one controlled canonical-input proof, not source mutation or live-hub parity.

## Producer work still REQUIRED
Current code is an incomplete producer foundation, not a functioning production ChangeFeed.
`fact_capture.capture_tasks` now exercises the canonical guarded opener and task
cursor with hydration in one transaction, 500-key batches, a 2000-key limit and
20ms target/progress interruption. It closes before immutable projection freeze,
refuses generation changes, skips hydration on heartbeat-only writes, and falls
back to bounded FULL when a large deleted-key delta has small live membership.
This is NOT the complete TaskFact adapter: hydrators/source coverage, bounded
paged FULL/replay and byte budgets are still missing. The Python hydrator cannot
be preempted; time overrun is detected, not a hard 20ms guarantee. Cursor reads
now enforce max_keys+1 before materializing their dirty-key set.
`fact_feed.ChangeFeed` now runs one event/deadline/retry owner thread, arms watch
callbacks before bootstrap, advances cursor only after successful commit, fences
unregister from success AND health publication, and retains late wake hints.
Native watch multiplexer/actual adapters and host runtime are NOT wired. This is
not yet the compatible production-host milestone consumers need. Deadline values
are monotonic. Failed source retries are bounded backoff, not ordinary polling.
Short independent audit found worker death on TypeError, opening-identity races,
large deleted-delta starvation, health/unregister races and scheduler-lock callback
convoy; fixes and focused tests added, not final exact-head ship approval.
Consumer e provider must return BoardViews even in legacy for participating scopes
so pre-cutover request custody exists; None is only unbound/nonparticipating.
`fact_reducers.ReducerEngine` now stages source batches and their transitive keyed
closure, reduces in declared kind-DAG order, tracks reads used to discover dynamic
membership, and publishes one generation only after every reducer succeeds. Edge
updates touch the affected closure; source deletion retains missing-key watches so
recreation invalidates dependents. Tests cover unrelated-key exclusion, no-op source
writes, retained snapshots, changed memberships, failed reduction, cycles and scope/
kind/output ownership refusal. This is an owner-only engine, not a concurrent writer
facade. Demand unbinding, deadline scheduling, source health/vector propagation and
production reducers remain required; FactStore's O(all keys) copy is still unresolved.

Outstanding consumer integration blockers found during parallel tests:
- Single >4MiB facts and large multi-panel resets need bounded pinned snapshot/transport
  recovery. Current Delta/FakeStore is NOT proof of this; never bypass queue caps.
- Native `_profile_scoped` must receive authorized memory registry upstream; preserve
  auth, do not bypass it to satisfy no-I/O tests.
- Generic dashboard mount must call consumer `bind_facts` even in legacy, since board
  prewarm is now lifecycle-owned, not import-owned.
- Annotation ownership transfer must drain/suppress all old profile prewarm/holders.
- Readiness is driven by ViewFact subscriptions after hot selector materialization;
  source availability or registering a consumer alone never constitutes readiness.

Remaining work:
1. Complete bounded store internals, dependency graph/index updates and atomic source
   cursor publication. Current dict generation copies are O(all keys), not acceptable
   evidence of incremental production complexity; ring byte accounting is approximate.
   Subscriber draining needs serialized delivery/callback-error handling and bounded
   reset paging in host. No production thread yet.
2. Source adapters: guarded short SQLite transaction captures cursor AND hydrated rows;
   reuse task/session trust logic, FULL, lineage and generation guards. Never naive
   mode=ro in places requiring managed query-only mode=rw. No torn cross-row read.
   Bootstrap pages + dirty replay/final barrier. Failed capture keeps last-good stale.
3. All excluded input coverage: messages/counters/edits/deletes/moves; operational
   claims/leases/wakes; remote parents/roster/links; notice arrival/claim/ack; notify
   subs add/remove/rebinding; config/policy; goal/state_meta/admissions/turns; cron,
   process/subagent lifecycle; transcript replace/truncate; git/PR owner publications;
   hub/origin-ledger/origin-work; plugin wait sources/user steps/rentals; deadlines;
   projects.db and git/path existence. Audit actual writers (cross-key REPLACE hazard).
   Mere watcher or latest-message ID is not sufficient; no routine DB polling.
4. Exact TaskFact/SessionFact/ChatWaitingFact/QueuePositionFact/DerivedCounts schemas
   and exhaustive Legacy* field/type/nullability manifest from canonical serializers.
   Snapshotting legacy bytes proves only wire parity, not incremental source parity.
5. Single dedicated host projection owner + authenticated/restrictive IPC scoped
   immutable replicas, ownership term fences, checkpoint/restart validation. Replace
   annotation start_holder ownership, not a second owner. Consumer d supplies stop/join.
6. Registered config facts.mode=legacy|shadow|facts, consumers switches; core plugin
   API manifest ctx.facts.v1; generic dashboard mount ctx injection and serve lifecycle
   wiring. Five-consumer version/health/prewarm release-generation latch, tested rollback.
7. Actual shadow parity harness consumer, fixture mutation matrix + restricted coherent
   live snapshot parity. No private databases committed. Independent exact-head review,
   CI and hermes-core-wt ship (no PR yet). Natural deployment, no signal/restart shortcuts.
8. Combined live acceptance: serve <20k cs/s 60s, feed <500 cs/s diagnostic, endpoint
   and actual click-to-paint p95 <500ms, every-source freshness <1s, zero erroneous
   changes timeouts 30min incl restart/reconnect, owner/gateway/RSS/transaction accounting
   and rollback no duplicate notifications. Before/after thread tables on origin
   t_01a2018f71f6939a. No acceptance claim until all pass.
   Session-switch acceptance amendment (origin t_01a204bd2d509466): at least 10
   natural MacBook clicks, paint=cache and paintMs <50; reconciledMs p50 <1000 ms,
   p90 <3000 ms. Abandoned opens are censored, never counted as passes. Existing
   baseline reports cache paint p50 7/p90 30 ms but reconcile p50 4792/p90 19278 ms;
   verify restricted evidence on that origin, not this relayed summary. Include
   synchronous user-steps /requests reconciliation in the measured critical path.

## Runtime demand integration (2026-11-23)

`ContentOwner.demand(scope, view, selector)` is the in-process facade callback.
It validates registered view/scope, bounds selectors to 16KiB and admits at most
4096 resident selectors per scope. It enqueues only: `DemandSource` runs on the
same ChangeFeed thread as native task/session capture, never a request/delivery
thread. Bootstrap with no demands performs no reduction or source read. Repeated
selectors coalesce for the owner lifetime. Reference-counted eviction is deferred;
capacity refusal is explicit, not an unbounded cache.

A failed speculative binding is withdrawn before another source/health publication
can encounter it. Refused demands rotate with bounded feed backoff, allowing a
small selector behind an oversized one to progress. Dependency callback errors
(including malformed selectors and cross-scope/kind keys) publish an unavailable
ViewFact rather than poisoning a source generation; valid reads before failure
retain dependency edges for recovery. Continuation deadlines are evaluated after
reduction so an intervening native source hint takes precedence.

Verification: new seven-test demand suite includes actual native task bootstrap
then local handle demand, no source re-read on demand, all projection calls on the
feed owner thread, duplicate idempotency, scope/bounds/closed refusal, source
publication after rejected demand, actual store byte-budget refusal with smaller
view progress, and dependency-failure recovery. Four poison regressions failed
before their fixes. Final no-retry integrated selection: 35 files / 178 passed,
full `scripts/check --base origin/main`: 21 checks passed. Scoped independent audit
identified these defects; it is NOT the final exact-head release review.

Remaining first-ship gates unchanged: production d/e source-complete materializers,
authorized serve startup/request-local bindings, live rendered-field parity,
independent final review/CI/canonical ship and natural updater measurements. No
runtime activation or live signal. The native title view test is demand integration
proof, not full board/annotation parity.

## Publication clock correction and next integration boundary

The native source commit, source-failure publication and demand publication now
pass epoch seconds to ReducerEngine, not monotonic uptime. Canonical renderers,
Fact.observed_at/valid_until and the expiry/read paths all use epoch seconds.
Only feed wake deadlines/backoff use monotonic time. A three-path regression
proved the old code passed uptime to renderers and would admit an already-expired
epoch dependency; the correction preserves monotonic mailbox scheduling.

Read-only integration audit (not final ship review) identified the remaining d
boundary more precisely; do not mistake source-content completeness for it:
- AnnotationFacts reads SessionFact.annotations, whereas DemandSource currently
  binds only ViewFact. Actual producer registration must bridge these deliberately;
  do not publish test fixture patches as native-source proof.
- Annotation request semantics include lineage and kanban display=True. Existing
  raw session-id reads and dropped kwargs do not encode those variants; preserve
  them when choosing the production output key/schema.
- Lifetime-coalesced accepted demands do not refresh expired projections. The
  production owner needs explicit expiry/source refresh scheduling, not an expiry
  worker merely marking old data stale. Never extend expired evidence by rerender.
- Native task/session scalar rows omit annotation operational inputs: goal state_meta,
  cron jobs, turn leases/interrupted turns, process registry/checkpoints, and kanban
  links/runs/subscriptions/shadow/external/plugin waits. Reuse canonical capture
  halves with explicit source health and watch/deadline coverage before cutover.
  Subagent data is session-derived; PR text keys require message capture and PR
  status can use the authorized 30–60s external refresh. No empty-on-error shortcuts.
- A slow kanban holder capture must not run inline and stall the sole content feed.
  Any auxiliary capture executor must have bounded admission/custody and publish
  through the owner mailbox. No request-side legacy I/O or parallel second holder.

These are integration findings, not implemented adapters or an activation claim.
Existing cleanup owner remains unchanged; d/e first and c/b/f legacy remain the
approved release ordering. All final review/live acceptance gates remain open.

## Consumer discoveries to preserve
b: list 11 query args, exact errors/storage/profile totals; sidebar separate usage/
truncation/failed profiles; stamps public annotations + cwd/git/outcome/muted.
Lifecycle wrapper before coalescers; HTTP cancellation retains scan custody.
c: ChatWaitingFact.panel includes full internal feed bundle required by _store;
ViewFact encoded wire comes from feed.wire_bundle/Feed._encode. No new wire protocol.
d: turn/background/subagent share execution_observations; source-partitioned merge,
not seven dict replacements. kanban needs_you/kanban_external fields too.
e: board fresh bookkeeping intentionally bypasses old snapshot cache; exact Task
asdict + age, summary, run time, link/comment counts, progress, managing session;
remote collision precedence and optional fields preserved. /events remains durable.
Scope e includes /board,/boards,/projects, REST tree, native tree/project_sessions;
canonical catalog mutation/discovery executors remain separate.
f: found pre-action cursor ACK and newest-first LIMIT 500 data loss; owns oldest-first
bounded drain and cursor-after-action correction in same workstream. No chat-closeout
mods edits: separate gateway owner. SourceState wake supports projected-value equality.

Hotspot: hermes_cli/web_routers/profiles.py — b sessions/sidebar/stamps; e project-tree
section only. Producer owns generic host startup integration, not either route body.

## Native remote-links display source (not activation-ready)

`fact_remote_links.RemoteLinksSource(engine, SourceId(scope, 'remote.links', origin),
origin_scope)` is an operational ContentOwner source. Trusted startup supplies the
exact `_origin_scope()` tuple while in the authorized profile context. A single
auxiliary worker runs the actual canonical `_project_remote_links` and captures its
origin-row sink, including released rows; the feed never runs that expensive read.
There is one in-flight job and one retained result per source. Feed publication is
atomic, failures preserve last-good timestamps/unknown initial state, and periodic
recapture is 30s after completion. Freshness starts at capture start, expires at90s.
The capture output is key/byte/time checked before publication; this is NOT a hard
RSS or cancellable-query bound. One deadline marks a hung worker unavailable;
no replacement worker is launched beside it. Close retains worker custody on timeout.
ChangeFeed now retains failed watch cleanup entries and attempts other cleanups,
so a subsequent owner.close actually retries the failed join.

`LinksFacts(handle, scope, origin_scope)` plus request-local `use(binding)` routes
ONLY explicit stale/display `_remote_links` / `remote_links_and_origin_rows` calls
to immutable scoped facts, before legacy scope discovery, locks, fingerprints or
ScopeRefresher admission. Strict reads retain canonical read-your-writes. A forced
holder revalidation refuses the display bridge rather than certifying old evidence.
The bridge returns detached lists/dicts and fails unavailable, never legacy fallback.
It must be pinned by trusted middleware, NOT selected from a query string.

Real fixture proof seeds and accepts actual origin-ledger receipts, captures through
ContentOwner, adds a second obligation and recaptures, then invokes both real display
entrypoints through InProcessFacts with request source/refresher tripwires. Unknown
owner publications remain canonically unknown: a title-only mutation in an incomplete
replica fixture is not a valid mutation assertion. This fixture intentionally omits
historical legacy associations and local duplicate inventories; it is not exhaustive
links/native-source or live parity. The source has no native hint subscription yet;
30s external refresh is an explicit expedited shortcut.

STILL REQUIRED before activation: authorized startup roster/HTTP unavailable mapping,
legacy display-refresher per-scope admission drain, canonical strict-reader coexistence,
per-source budget selection (default content20ms is not a justified links budget),
actual b/e materializers and request bindings, rendered fixture+live parity, final
exact-head independent review/CI/ship/natural updater and CPU measurements. Nothing
here activates serve or claims the links thread has disappeared.
