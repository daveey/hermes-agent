# Bounded Kanban priority rollout

## Failure chain

Problem: stored hub priorities escalated past 10000 while ready work waited on a
finite worker pool. An absolute bump merely moved the escalation threshold.

- Creation and editing accepted unbounded integers (`kanban_db.create_task`,
  `kanban_db_edit.edit_task`). Therefore repeated raises persisted indefinitely.
- Parent/direct-chat notices recommended raising every waiter without checking
  capacity or lane rank (`kanban_parent_unclaimed._options`, `kanban_chat_wait`).
- Transitive inheritance donated the maximum waiter priority at read time
  (`kanban_db_dispatch_queries.EFFECTIVE_PRIORITY`). It did **not** rewrite parent
  priorities; it made escalation affect the prerequisite queue as intended.

Root (happened): unbounded priority input plus capacity-blind escalation advice.
Root (not caught): ordering and notice tests explicitly expected unconditional
bump advice, but had no cap/median contracts.
Recurrence: queue-wait advice introduced for the earlier unclaimed-parent and
human-go incidents remained in place after transitive inheritance was added.

## Policy

- Cap newly created, explicitly edited, remotely imported and effective inherited
  priorities at **1000**. Negative deprioritization remains supported.
- `hermes kanban edit ID --priority-bump 10` computes a **relative**, bounded raise
  inside the write transaction. It records previous/requested/applied values.
  This chooses the relative-bump alternative: it is not a TTL or an expiring boost.
- Ready-wait notices recommend placement/capacity first. A relative bump is offered
  only when captured effective priority is below the median of the same ready,
  assignee and placement lane. Missing evidence means no bump recommendation.
- Inheritance, parent-first tie-breaking and age ordering remain intact. Two cards
  at 1000 can still have work ahead; priority never creates capacity.

## One-shot migration and rollback

Run on the origin writer, **not a worker replica**. Do not move its live checkout
or restart a busy gateway by hand. Use the normal reviewed core-update route.

1. Before installing cap-aware import code, coordinate the snapshot/application
   window with the origin operator. Capture all open priorities through
   `kanban_priority_migration.plan(conn)` from the reviewed worktree. Save with
   `save_manifest(Path(absolute_path), manifest)`; it uses exclusive creation and
   fsync. Attach this JSON to the owner task **before** applying it.
2. `apply(conn, Path(absolute_path))` normalizes distinct positive ranks to 1..900,
   preserving ordering and ties and leaving nonpositive values unchanged. The
   highest two requested cards therefore remain in the highest two distinct bands.
   Reserve 901..1000 for deliberate subsequent escalation. Terminal rows are not
   rewritten. The operation refuses a different board/gateway, shadow mode,
   foreign-owned open rows, new open cards or any changed recorded priority.
   On refusal capture and attach a **new** manifest; never overwrite the original.
3. Publish the origin deltas through normal Board sync and read back exact target
   values. Install on the hub using normal drain/update coordination. Ensure old
   clients which can write priorities are upgraded; imported legacy values are
   bounded locally, but this does not remotely rewrite a legacy sender's database.
4. Verify no open stored priority exceeds 1000. With a genuinely free slot and
   other eligible effective priorities below 1000, bump one authorized ready card
   to 1000 and verify `queue.ahead == 0`, then observe its claim. Do not manufacture
   capacity, disturb running work or claim this fixture is live proof.
5. Rollback: `apply(conn, manifest_path, undo=True)` restores prior values only
   when all recorded current values still match. Restoring above-cap legacy values
   additionally requires `allow_legacy_rollback=True`: this is an explicit rollback
   of the priority policy and must be coordinated with deployment, not normal
   editing. No lifecycle state, lease, parent edge or run is reverted.

Normalization is intentionally not an implicit connection-time migration: clipping
before saving originals would destroy the very order the operator asked to retain.
Keep the manifest attachment and installed-revision proof with the task.

## Proof

Focused regressions: `test_kanban_priority_cap.py`,
`test_kanban_priority_boundaries.py`, `test_kanban_priority_migration.py`;
existing transitive inheritance, parent-first dispatch, priority-lowering,
ready-wait episode and held-machine tests. The sync test replays an above-cap peer
update twice and verifies the bounded value with no echo outbox growth.
