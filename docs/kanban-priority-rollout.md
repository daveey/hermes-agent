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

## One-shot mixed-origin migration and rollback

The supported executor is the **non-worker operator** `supervisor-requests`
CLI. A dispatched worker or delegated descendant cannot execute it, including
through a shell subprocess. Do not clear lineage, proxy it through SSH, or invoke
Python/SQL directly to evade this boundary. An actual operator must execute the
commands in their own operator session. No automatic worker grant is introduced.

1. Coordinate a quiet migration window across all origins. Before installing any
   cap-aware imports, prepare from the raw canonical Board snapshot (no local
   cap-aware pull). Use a locally owned existing task for `LOCAL_TASK`; do not
   create extra open tasks after capture. `ORIGIN` is the enrolled primary gateway
   name. The board flag is before the subcommand.

   ```sh
   hermes kanban --board default supervisor-requests --priority-operation prepare \
     --writer ORIGIN --task LOCAL_TASK --backup /absolute/fresh-original.json \
     --exception t_01a1ca6310b2af43 --exception t_01a19400313cfbda
   ```

   The requested exceptions are explicitly ordered low to high. Each must resolve
   canonically, including terminal state: missing is an error, not assumed done.
   Open exceptions reserve the top bands (at most 900); their old values are in
   the manifest. Ordinary positive values keep order/ties; nonpositive ordinary
   values are unchanged. Terminal rows are not rewritten. Snapshot pagination
   requires one unchanged feed head. This is an observation, not a global lease.

2. Secure the resulting original backup and attachment before doing anything that
   might clip values. Copy the **exact original bytes** to every other origin,
   then stage them on each primary writer using a fresh backup path:

   ```sh
   hermes kanban --board default supervisor-requests --priority-operation stage \
     --writer ORIGIN --task LOCAL_TASK --manifest /absolute/copied-original.json \
     --backup /absolute/fresh-origin-backup.json
   ```

   Each JSON receipt gives `attachment_id`, `manifest_sha256`, `writer`, and
   `backup`. All origins must report the same digest. Keep all receipts and verify
   every original backup is secured **before any apply or cap-aware install**.
   Staging preserves bytes exactly; it never overwrites a backup. It changes no
   priority. If attachment storage fails, retain the original backup and retry
   staging at a new path.

3. On each origin, apply its local attachment and exact digest:

   ```sh
   hermes kanban --board default supervisor-requests --priority-operation apply \
     --writer ORIGIN --attachment-id LOCAL_ATTACHMENT_ID --digest SHA256
   ```

   This uses the normal writer transaction, events and sync outbox. It refuses
   shadow/different writers, foreign attachment owners, changed manifest bytes,
   invalid ranks, unknown origins, and changed owned open membership/ownership or
   priority values. All rows already at their targets are an idempotent no-op;
   mixed old/new states refuse. Movement among open statuses is safe.
   Each origin is atomic, **not all origins together**. If any origin refuses,
   stop the rollout: do not clip/import/install and do not rewrite the manifest.
   Retain all originals and receipts. Either guarded-restore applied origins or
   deliberately capture and attach a new global plan from the new state.

4. Let normal Board sync publish the outbox, inspect rejection/pending counts,
   and read back canonical priorities against every manifest row. A local command
   receipt is not publication proof. Only after all origins match may the rollout
   owner install through the normal drain/update route. Verify no open stored
   priority exceeds 1000; reserve 901..1000 for deliberate escalation. Verify a
   genuinely eligible ready card's queue/claim under real capacity separately.

5. Guarded rollback uses the same local attachment/digest:

   ```sh
   hermes kanban --board default supervisor-requests --priority-operation restore \
     --writer ORIGIN --attachment-id LOCAL_ATTACHMENT_ID --digest SHA256 \
     --allow-legacy-rollback
   ```

   The explicit flag acknowledges policy rollback when restoring above-cap old
   values; coordinate cap-aware import/deployment policy first or imports may
   immediately clip restored values. Restore requires exact owned membership and
   target-value CAS, just like apply (new cards also cause refusal). It never
   reverts lifecycle state, lease, parents or runs. Publish and read back restore
   through normal sync too.

Legacy terminal priorities remain historical until reopened. The shared descendant
invalidation path bounds the reopened root and every invalidated descendant in the
same transaction, recording each original value in a `reprioritized` event.
Review found both omissions because initial tests exercised input edits and value
CAS, not lifecycle re-entry or membership loss. Five added regressions failed on
the reviewed head and passed after correction (ancestor/descendant audit plus
done/archived departures on apply/undo).

Normalization is intentionally not an implicit connection-time migration: clipping
before saving originals would destroy the very order the operator asked to retain.
Keep the manifest attachment and installed-revision proof with the task.

## Proof

Focused regressions: `test_kanban_priority_cap.py`,
`test_kanban_priority_boundaries.py`, `test_kanban_priority_migration.py`;
existing transitive inheritance, parent-first dispatch, priority-lowering,
ready-wait episode and held-machine tests. The sync test replays an above-cap peer
update twice and verifies the bounded value with no echo outbox growth.
