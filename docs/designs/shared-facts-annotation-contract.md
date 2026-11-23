# Session-row annotation contract (read-only inventory)
Inventory baseline: ad074af728. All refs are hermes_cli/ unless noted; line numbers refer to that baseline.

This is a serializer inventory, not evidence of complete production source coverage.
The shared owner publishes per-family canonical output patches in `SessionFact.annotations`.
Consumer startup receives an authorized handle/scope and lifecycle callbacks;
request integration installs `kanban_annotation_facts.use(binding)`. No request acquires
IPC, discovers a profile, or falls back to legacy when an active facts binding is missing.
The host coordinator must drain all participating prewarm/read paths before activation;
`kanban_annotation_host.suspend_holder` joins the old holder and fences racing starts,
and `resume_holder(role)` provides idempotent rollback after subscriptions close.
These seams do not yet prove the production multiprocess owner or source coverage.

Call order (web_routers/sessions.py:341-347, web_routers/profiles.py:522-528,581-583,681-683, tui_gateway/methods_projects.py:392-394):
kanban(display=True on profiles/tui) -> goal -> cron -> background(process) -> turn -> subagent -> review.
Order matters only for `execution_observations` (shared field; each producer replaces only its own sources).
Fact patches carry only that family's contribution, not the fully composed row's
observations. Scalar-only goal/cron/review patches must omit the field. Other
families require an array of mapping items with their declared source tags;
kanban additionally admits `kanban.replica_run` while retaining legacy's prior
replica-run entries. Invalid contributions reject the entire row batch before
any mutation, rather than overwriting or duplicating another family's evidence.

Default convention: "absent when zero/false" (exceptions include `kanban_parked=0` and an explicit empty
`execution_observations` list). Every annotator pops its own keys when it has nothing, EXCEPT on
its own whole-read failure, where it returns rows untouched (stale stamps on a reused row survive) — see per-annotator.

## 1. kanban (kanban_session_activity.annotate_kanban_activity:1853; kanban_activity_identity; kanban_open_asks)
| field | type | presence |
|---|---|---|
| kanban_observed_at | float epoch s | present on every success row (= min(time.time()-3, copy_at)); popped on unknown |
| kanban_unknown | true | present only on failed/unavailable read (mark_unknown:98); popped on success |
| kanban_active | int>0 | absent when 0; + len(pending plugin close-outs) |
| kanban_blocked, kanban_unseen, kanban_attention, kanban_remote_stale | int>0 | absent when 0 |
| kanban_parked | int>=0 | present (even 0) iff kanban_active truthy, else absent |
| kanban_held | int>0 | absent when 0 (independent of kanban_active) |
| kanban_stalled | int>0 | present iff n>0 AND kanban_active |
| kanban_waiting_on | list[str] sorted, cap 50 | absent when empty |
| kanban_blocked_on | list[str] sorted owner-qualified keys, cap 50 | absent when empty |
| needs_you | list[{kind:"card", ref:str ("" padding), question:""}] | absent when empty; padded to at least kanban_blocked; refs may exceed the count |
| kanban_external, kanban_external_unknown, kanban_external_down | int>0 | absent when 0 (kanban_external_activity.stamp_rows:80) |
| kanban_external_cards | list, cap 10 | absent when empty — see §8 |
| execution_observations | list | sources kanban.card / kanban.counts / kanban.replica_run — see §9 |
Unknown path (mark_unknown + count_snapshot(row, ())): pops all counts, kanban_observed_at, waiting_on, blocked_on,
needs_you; sets kanban_unknown=True; execution_observations gets carried history + kanban.counts sentinel.
NOTE: unknown path returns BEFORE stamp_boards, so kanban_external* are NOT popped/updated on unknown.

## 2. goal (goal_session_activity.annotate_goal_activity:140)
goal_active: true | absent. Whole-read failure -> rows untouched. Per-DB errors -> that DB contributes nothing.

## 3. cron (cron_session_activity.annotate_cron_activity:120)
cron_active: true | absent. Same failure semantics.

## 4. background (process_session_activity.annotate_background_activity:193)
background_active: int>0 | absent. background_processes: list (cap 8, sorted by (started_at or 0, id)) | absent.
Item (_detail:92): {id: str, command: str (whitespace-collapsed, redacted, <=160 chars with "…", default
"background process"), started_at?: float (only if >0), pid?: int (only if int>0)}. Fallback item when detail
missing: {id, command:"background process"}. id = registry id, or checkpoint session_id, or str(pid) when no session_id.
Row with "profile" filters out processes whose evidence profile differs.
execution_observations: replace(row, {process.registry, process.checkpoint}, …) one per proc with evidence.
Collection failure -> rows untouched (incl. no execution_observations change).

## 5. turn (turn_session_activity.annotate_turn_activity:219)
turn_live: true | absent. Can be true WITHOUT any observation (update-paused gateway/TUI turns hold no lease).
execution_observations: replace(row, {turn.lease}, one per live lease of matching roots).
Failure of lease read (sqlite/OSError) -> rows untouched.

## 6. subagent (subagent_session_activity.annotate_subagent_activity:57)
No scalar field. Only execution_observations: replace(row, {subagent.activity, subagent.registry}, …).
Store failure (per profile): previous subagent obs re-emitted with state=unknown, unknown_reason=
"subagent_store_unavailable" (raw spread, NOT re-projected) + one identity-less unknown sentinel
(kind subagent, no work_id/generation; owner session = row id).

## 7. review (review_session_activity.annotate_review_wait:271)
review_wait: "stuck"|"parked" | absent; review_wait_pr: "https://github.com/<owner>/<repo>/pull/<n>" (lowercased
owner/repo) | absent. Always set/popped as a pair. Exception anywhere -> loop stops, remaining rows untouched.
Lookups async: first poll may be absent, stamped on a later poll.

## 8. kanban_external_cards item (kanban_external_activity.session_external:58-71; kanban_external_state._external:97)
{task_id: str, title: str ("" if unknown), external: {
   service: str (display name, e.g. "GitHub Actions" / "external probe" / "external service"),
   adapter: str ("github_run"|"github_pr_checks"|"shell"|…),
   job: "queued"|"running"|"succeeded"|"failed"|"cancelled"|"unknown",
   health: "ok"|"degraded"|"down"|"unreachable"|"auth_failed"|"rate_limited"|"probe_broken"|"host_silent"|"watch_missing",
   reason: str,
   url: str|null,
   observed_at: float|null (null before first poll),
   next_due_at: float|null (null before first poll),
   blocker_id?: str   (only while an open service-down shared blocker holds it; kanban_external_watch._name_blocker:192)
   triage_card?: str|null (with blocker_id),
   state: "external_running"|"external_unknown"|"external_down" }}
Sort: (external.observed_at or 0, task_id) desc, per board then re-sorted across boards/lineage; dedup by task_id.
Counts: external_down -> counts[2]; else counts[0] (+counts[1] if external_unknown). Cards are always a subset of
parked scheduled cards with awaiting.kind in ci/deploy/external on LOCAL boards only. Read errors -> {} (absent).
TS mirror: apps/desktop/src/types/hermes-session-activity.ts:71-84 (declares adapter/next_due_at/observed_at/reason/
url optional, external nullable, title optional; does not declare state/blocker_id/triage_card).
TIME-DEPENDENT: state/health/reason derive from the reader's clock (5 s memo / board signature + TTL).

## 9. execution_observations[] item (output of execution_observations.project:373)
Envelope keys after project (adapt:83 + _progress:207 + _last_known:341):
- version: 1 (copied; anything else -> unknown "legacy_or_unsupported_version")
- identity: object (missing entirely on kanban.counts sentinel). Keys:
  turn/process/subagent (_identity:48): kind, work_id always present (work_id may be null); gateway/profile/session/
  generation OMITTED when None. card_raw: kind, work_id; gateway/profile/board/run_id omitted when None.
  execution_source.raw/unknown_raw, receipt, replica raw: gateway/profile/board PRESENT possibly null; run_id or
  generation added when known.
  generation formats: turn "holder-sha256:<hex64>"; process "pid=<pid>:start=<tick>"; subagent "session=<child id>";
  card: event generation string. run_id: int or str (task_runs uid/id).
- owner: {gateway, profile, session?, assignee?}. turn/process/subagent omit None keys; card sources always carry
  gateway/profile (may be null), assignee/session only if truthy. Absent on counts sentinel.
- state: running|queued|waiting|needs_user|done|failed|cancelled|unknown (always present)
- freshness: "current"|"stale"|"unknown" (always)
- unknown_reason: str, present iff state=="unknown" (popped on current). Values seen: legacy_or_unsupported_version,
  missing_qualified_identity, missing_qualified_owner, missing_evidence_references, missing_source_identity,
  missing_or_invalid_source_timing, clock_inconsistent, invalid_required_sources, missing_required_source_timing,
  invalid_required_source_class, source_expired (freshness=stale), unsupported_state, source_unknown, <producer
  reason passthrough: source_unavailable, adapter_error, owner_observation_unavailable, bounded_history_incomplete,
  subagent_not_running, missing_subagent_child_or_parent, subagent_store_unavailable>, unsupported_work_kind,
  missing_human_request, incomplete_wait_condition, invalid_human_wait, unverified_completion,
  invalid_requested_identity, execution_identity_mismatch.
- last_known_state: str (non-unknown state) | null (always present; "done" only with valid receipt)
- source: str (kanban.card|kanban.counts|kanban.replica_run|turn.lease|process.registry|process.checkpoint|
  subagent.activity; legacy subagent.registry only replaced). process source may be None-ish only if entry lacks it.
- source_revision: str | null (null for clockless card rows/unknown_raw; absent on counts sentinel).
  turn "session_turn_leases/<cid>/<generation>@<acquired_at>"; process "<source>/<id>@<observed>";
  subagent "subagent.activity/<child>@<observed>"; card "tasks/<id>/last_heartbeat_at/<at>" | "task_events/<id>" |
  "task_runs/<run>/last_heartbeat_at/<hb>" | generation | receipt "<ref>#event:<uid|id>"; replica run["ref"].
- evidence_refs: list[str] (may be [] for clockless card; absent on counts sentinel)
- observed_at: epoch|null (setdefault None); valid_until: epoch|null — on valid path clamped to min(valid_until,
  observed_at+180) (sidebar class). turn valid_until = lease expires_at (clamped); process/subagent observed+180;
  card at+180; receipt observed+DONE_TTL_SECONDS (clamped to 180).
- age_seconds: null on early-unknown paths, else now-observed_at (TIME-DEPENDENT; volatile)
- last_activity: epoch|null (always present). subagent: child activity clock; card: row activity_at.
- last_activity_kind: str|null — card sources only (kanban.card, kanban.replica_run, receipt "completed").
- last_progress, last_progress_kind, last_progress_ref: always present; all null unless a valid progress triple.
- wait: null | {owner, target{…identity minus run_id}, since, due_at, kind, predicate, targets?} — card
  execution_source only; null on other producers; absent on counts sentinel.
- human_request: optional dict (card sources), resolved_check/resolved_check_every stripped.
- delivery_receipt: optional {verified:true, disposition:"delivered", evidence_ref, identity} (done cards).
- retained_human_requests, historical_delivery_receipts: optional lists; only via count_snapshot _carry_history
  (kanban_unknown rows) — project() itself does not create them.
- generation_portable: optional bool (card running/waiting; replica true).
- phase_since: optional epoch|null (card queued/waiting). dependency_states: optional [{work_id,status}] (todo w/ parents).
- diagnostics / required_sources: in schemas/execution-observation-v1.schema.json, NOT emitted by these row producers.

Merge rules (who owns which entries of the shared array):
- kanban: _merge (kanban_execution_observations:193) drops prior kanban.card + kanban.counts only (NOT
  kanban.replica_run — a re-annotated reused row could keep a stale replica_run entry). Key set to [] when
  kanban_active (keep_empty, "current producer" signal) else popped when empty.
- turn/process/subagent: replace (turn_process_execution_observations:163) drops prior entries whose source in own
  set; pops key when result empty. Non-dict entries are kept.
- Order within array = prior kept entries, then producer entries in producer order (turn: lease order; process:
  sorted proc id; subagent: SQL row order; kanban: sorted count keys then carried history then sentinel).
- count_snapshot (kanban_execution_observations:300): one obs per key in active/blocked buckets plus "done:" evidence
  keys; key parse "gw/profile/board/tid" | "gw/board/tid" | "board/tid" | "tid".
- Schema vs emitted divergence: schema requires source_revision/evidence_refs minItems 1/epoch timing, but emitted
  unknown observations carry null/[]/missing — consumers must accept projected-unknown shapes.

## 10. Other emitters of the same item shape
process_session_activity.annotate_registry_entries:289 (process.list entries get execution_observations [obs]).

## Tests proving parity/contract (tests/hermes_cli/ unless noted)
- test_execution_observation_golden.py (test_shared_semantics/select/summarize; fixture schemas/fixtures/
  execution-observations-v1.json shared with apps/desktop/src/lib/execution-observations.test.ts) — Py/TS projector parity.
- test_execution_observations.py (16) — adapt/project unknown reasons, identity rules, legacy retention.
- test_turn_process_execution_observations.py (19): :127 turn replace keeps others; :280 process append preserves card
  obs; :210 asserts background_processes item keys <= {id,command,started_at,pid}; :243 pid-only/birthless unknown;
  :306 sandbox entry unknown but counted; :363 subagent unknown w/o identity; :87 lease not renewed.
- test_process_session_activity.py :88 rows list id/command/age + stale background_processes popped (:95-103).
- test_subagent_session_activity.py :37 registry vs sqlite byte-identical & merge; :100 failed/missing store unknown.
- test_kanban_external_activity.py :34 counts subsets + card external keys non-empty, absent when none; :60 cap 10
  newest-first; :88 null check times before first poll; :157 cached board byte-identical; :202 card gone stays gone.
- test_kanban_external_watch.py :141 every hover field; test_kanban_external_review.py:181 blocker_id/triage_card.
- test_kanban_execution_observations.py :128 counts unknown without zero fabrication; :75 ask survives unknown.
- test_kanban_session_activity*.py, test_kanban_busy_board_activity.py, test_kanban_activity_scope.py — kanban counts.
- test_goal_session_activity.py, test_cron_session_activity.py :45 (clears stale stamps), test_review_session_activity.py
  :125 (clears stale stamp, survives fetch errors), test_turn_session_activity.py.
- Desktop consumers: apps/desktop/src/store/{session-dot-state,live-work-roster,execution-observation-row,
  sidebar-automated,subagents,composer-status}.ts (+ .test.ts).

## Pure canonical extraction seams

These functions are consumed by the retained legacy annotators as well as the
owner's future materializer. They mutate caller-owned rows in place, preserving
field absence and source-partitioned execution observations. Supply detached
captured inputs, not lazy rows/connections. Capture failure is a source-health
failure, not an empty collection. `now` is the captured wall-clock seconds.

| Module | Callable | Captured input |
|---|---|---|
| `goal_session_activity` | `project_goal_activity(rows, active)` | Set of active goal-owner session IDs, including lineage IDs |
| `cron_session_activity` | `project_cron_activity(rows, owners)` | Set of live cron-owner session IDs |
| `turn_session_activity` | `project_turn_activity(rows, *, live, leases, gateway, now)` | `live`: ID set; `leases`: ID → ordered lease dicts with `profile` and the canonical turn-observation fields |
| `process_session_activity` | `project_background_activity(rows, *, owned, details, evidence, gateway, local_profile, now)` | `owned`: session ID → process-ID set; `details`: process ID → public detail dict; `evidence`: process ID → captured canonical process evidence, including `owners` set and recorded profile |
| `subagent_session_activity` | `project_subagent_activity(rows, *, by_profile, gateway, now)` | profile → `(children, error)`; children are `(child_id, parent_id, activity_at, started_at)` tuples; error is `None` or canonical source error |
| `review_session_activity` | `project_review_row(row, *, reviews, now)` | Ordered `((owner, repo, number), info)` pairs; info is `parse_pr`'s captured value or `None` |

| `kanban_activity_projection` | `project_kanban_activity(rows, *, parts, observed_at, now, external=None)` | `parts`: exactly 12 detached maps as specified below; `observed_at`: conservative capture date; `external`: captured external contribution map, `{}` clears fields, `None` preserves them |
| `kanban_activity_projection` | `project_kanban_unknown(rows, *, now)` | Same unknown-path projection as legacy, preserving external fields and independent observations |

All seven seams are executable and exercised under the no-I/O guard. Kanban
`parts` is `(active, blocked, unseen, remote_stale, waiting, parked, held, stalled,
card_keys, extra, attention, execution)`. Count maps are session-ID → integer;
`waiting` and `extra` map session IDs to bare/local/authority-qualified obligation
IDs; `card_keys` maps sessions to `(qualified_key, bucket, parked_bool)` triples;
`execution` maps sessions to key → canonical raw execution observation. The
legacy caller resolves cached copy time and attention before invoking the pure
function. `observed_at` is the exact conservative source timestamp (legacy
`min(time.time()-_CACHE_TTL_S, copy_at)` when copy_at exists), not reducer `now`.
Caller rows carry `id`, optional `_lineage_root_id`/`_lineage_ids` and any prior
stamps; no source input is mutated. Explicit `now` supplies observation expiry.

`kanban_external_activity.stamp_rows(rows, by_session, ids_of)` remains the pure
external serializer, called by the new projector when `external` is supplied.
Its map is session-ID → `{counts: [external, unknown, down], cards: [...]}`;
card shape/order is §8. Its DB-backed `stamp_boards` is NOT pure. Source owners
must capture this separate input; `{}` is not a substitute for source failure.
The unknown projector intentionally does not clear the previous external fields,
matching legacy's early-return semantics. `annotate_kanban_activity` itself still
captures sources in legacy mode and must never be registered as a pure reducer.

## Lifecycle integration (inert until producer wiring)

- `AnnotationFacts` drains admitted bound AND unbound legacy annotator calls
  across profiles before stopping legacy work. New legacy calls fail retryably
  while any process binding is transferring or on facts. The host coordinator
  must withdraw **all** authorized profile bindings before release.
- Mode callbacks fence older generations even on failure. A rollback or failed
  stop aborts its generation: replayed facts callbacks cannot reactivate it;
  retrying legacy rollback at that generation remains allowed.
- `suspend_holder(timeout=...)` fences racing holder starts, suppresses/drains
  annotation prewarm across profile contexts, then joins the holder. Suspend and
  resume serialize on a separate transfer lock; warmup does not hold that lock.
- A partial stop invokes rollback even when stop raised. A failed rollback keeps
  the request fence and retries on the next legacy callback, including same-mode
  callbacks. Never remove custody merely because a join timed out.
- The boot thread also warms the project tree, owned by consumer (e). After BOTH
  annotations and tree admission are withdrawn, the global coordinator must join
  `kanban_annotation_prewarm.boot_thread()` before activation. Annotation-only
  suppression does not prove the combined boot thread is stopped.
- `resume_holder(role)` restores prewarm admission and starts the legacy holder
  once. Invoke process-wide restart only after all scoped facts subscriptions
  stop; per-profile callbacks alone are not the global rollback barrier.
- Serializer tests compare all seven nonempty canonical serializers using
  controlled source fixtures, and guard real fact-backed entrypoints against I/O.
- `test_annotation_fact_transport.py` runs a real FactHost owner and three real
  replicas (two same-profile, one isolated profile), controlled SQLite captures,
  exact canonical kanban patches, source-read counts across repeated requests,
  mutation, singleton exclusion, owner crash/unavailable/new epoch recovery,
  and one-shot subscription rollback. The lifecycle facade is explicitly
  TEST ONLY because raw ReplicaV1 lacks coordinator binding. This proves neither
  real waiting-source adapter registration/quiet recovery nor production startup,
  global cutover, or actual legacy-holder rollback; those remain integration gates.
