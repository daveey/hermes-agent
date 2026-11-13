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
A running non-worker dispatcher is not authorization for a worker to enqueue
board-wide changes: existing supervisor request families remain creator-scoped.
The rollout owner must arrange genuine operator execution on **each** origin;
without that operator, stop before preparing or applying and request their action.
The commands below are an operator handoff, not a worker-executable runbook.
Digest binding and membership/value CAS provide freshness; there is no time-based
grant or expiry. Implementation tests do not establish live operator authorization.

### One-command operator coordinator

Run this only from a genuine operator context, with this command surface installed
on every origin. The local and remote CLIs retain the worker/delegated-lineage
fences. The coordinator checks lineage before reading configuration or starting
SSH; remote staging checks it before reading stdin. SSH is transport for an
already authorized operator, never a worker authorization mechanism.

**Run 3 prerequisite:** every participating origin, including the local writer,
must run core containing PR #1623's landed commit
`abf75b3f2ce7358a2944856c657bd2ea13be9567` before any prepare/stage. Older code
refuses a no-flag restore outright when the manifest contains above-cap originals,
even when apply never committed and restore should be a no-op. Having the command
surface or updating only the coordinator is not enough.

On each origin, identify the installed core used by its actual command (the
host-map `argv`, or the local coordinator), then perform this read-only check:

```sh
git -C /absolute/installed-core merge-base --is-ancestor \
  abf75b3f2ce7358a2944856c657bd2ea13be9567 HEAD
```

Require exit 0 from every origin and retain its installed HEAD with the run
report. A missing commit, non-Git install, or nonzero result is not proof: stop
before staging and resolve the installed-version evidence. Check the executing
runtime, not an unrelated updated checkout. The runner does not automate this
version check: its configurable command prefix can wrap a different installation,
and the host map does not identify the runtime's Git root. The read-only `origins`
operation below verifies coverage only, not this version prerequisite.

Create an explicit JSON mapping for **every origin in the captured manifest**.
Entries for other enrolled origins that own no captured row are allowed; the
coordinator reports them as `skipped` and never contacts them, so a map listing
every enrolled gateway keeps working when one drains:

```json
{
  "origin-a": {
    "host": "operator@origin-a.example.com",
    "argv": ["hermes", "kanban", "--board", "default", "supervisor-requests"],
    "task": "EXISTING_LOCAL_TASK_A",
    "backup": "/absolute/fresh-origin-a.json"
  },
  "origin-b": {
    "host": "operator@origin-b.example.com",
    "argv": ["/absolute/hermes", "kanban", "--board", "default", "supervisor-requests"],
    "task": "EXISTING_LOCAL_TASK_B",
    "backup": "/absolute/fresh-origin-b.json"
  }
}
```

`argv` is the complete remote command prefix, including board and
`supervisor-requests`; arguments are individually shell-quoted for SSH. Hosts are
never inferred or hardcoded. The local writer also needs an entry, but executes
in-process rather than via SSH. Backup paths must be fresh absolute paths, and
attachment tasks must already belong to their corresponding writers.

```sh
hermes kanban --board default supervisor-requests --priority-operation migrate \
  --writer origin-a --host-map-file /absolute/operator-host-map.json \
  --exception EXCEPTION_LOW --exception EXCEPTION_HIGH
```

Alternatively pass the JSON directly with `--host-map '{...}'` (exactly one map
flag). `--allow-legacy-rollback` makes the automatic guarded recovery restore
above-cap originals exactly (a policy rollback). Without it, recovery restores
them at the cap (1000), which ranks identically because effective priority is
capped at read time, so a guarded restore never raises a card above the cap.
Prefer omitting it.

After a capped restore, a later restore with `--allow-legacy-rollback` is a no-op:
`at_value(1000, old)` already counts as restored. Adding the flag cannot lift those
values back to exact originals. Choose the rollback policy before the first
restore; do not try to force a write or bypass CAS to change that outcome.

Apply and restore treat a stored value of exactly 1000 as holding an above-cap
manifest original (clamp equivalence, `kanban_priority.at_value`): the sync pull
bounds every received priority in place with no event or outbox op, so an
origin's replica can read 1000 while the Board, and therefore the manifest, keeps
the raw legacy value. Any other difference is still a refusing edit.
Do not pass separate task, backup, manifest, attachment or digest flags to migrate.

Before a run, check the map against the live origins with the read-only
`origins` operation (same operator fence, same map flags, no `--allow-legacy-rollback`):

```sh
hermes kanban --board default supervisor-requests --priority-operation origins \
  --writer origin-a --host-map-file /absolute/operator-host-map.json
```

It captures the canonical inventory and prints
`{"origins": {name: open-row count}, "missing": [...], "extra": [...], "errors": [...]}`.
It exits 1 when an origin has no map entry or the writer is unmapped or owns no
rows; `extra` entries alone pass. It writes no backup, attachment or priority,
and does not use the backup paths, so they stay fresh for the real run. Hosts are
still never inferred: fix a `missing` origin by adding its entry by hand.

The coordinator captures one global raw manifest and checks host-map coverage on
it **before writing anything**. A coverage mismatch (an origin missing from the
map, or a local writer that is unmapped or owns no rows) exits 1 with `missing`,
`extra` and `errors` in the report and consumes **no backup path and no
attachment**: fix the map and rerun with the same paths. Only then does it stage
that same captured manifest locally (backup plus attachment, so the staged digest
is that of the checked capture), send the **exact UTF-8 bytes** over SSH stdin with
`stage --manifest -` to every covered origin,
and validate every origin's attachment, writer, inventory and identical SHA256
receipt **before any apply**. Staging failure or digest mismatch writes no
priorities, but local preparation and any completed staging consume their fresh
backup paths if later staging fails. Retain
those backups and choose new paths before retrying. It then applies each origin,
publishes each normal outbox via the
supported `kanban_sync_client.push` API (no snapshot pull), rejects pending or
rejected publication, and runs the canonical read-only verifier. Success requires
canonical verification, not merely local apply receipts.

A later apply refusal, SSH timeout, invalid response, or **any exception from the
in-process local writer** (for example `sqlite3.OperationalError: database is
locked` under live dispatch) stops further applies and
attempts guarded restore on **every attempted origin**, including the uncertain
origin, in reverse order. A restore that raises on one origin is recorded and the
remaining origins are still restored. The JSON report separates `attempted` origins from
acknowledged, validated `applied` receipts and retains restore errors; the command
exits nonzero even if recovery succeeds. The same CAS makes restore a no-op if
apply never committed. Once all attempted origins have successful restore
receipts it also publishes and attempts canonical `verify-restore`. Restore
refusal never causes a forced write: residual uncertain outcomes require manual
inspection of the origin's attachment, receipts and current state before resuming.
Do not treat an absent apply receipt as proof of no write. Publication or canonical
verification failure also exits nonzero, retaining receipts/backups for operator
recovery, but does **not** trigger automatic rollback; automatic guarded restore
is limited to apply failures.
Save stdout as the durable operation report. No credentials or lineage are cleared.

### Individual operator operations

1. Before installing any cap-aware imports, prepare from the raw canonical Board
   snapshot (no local cap-aware pull). No global quiet window or dispatcher freeze
   is required: bounded capture observation and transactional apply/restore guards
   determine safe progress under churn. Use a locally owned existing task for
   `LOCAL_TASK`. `ORIGIN` is the enrolled primary gateway name. The board flag is
   before the subcommand.

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
   retains the first feed head, then scans changes through a finite final observed
   head. Heartbeats, events, runs and unrelated task fields may advance normally;
   open-status transitions are replayed. Changes to task membership, openness,
   ownership or priority refuse capture/verification and require a fresh attempt.
   This observation is not a global lease; no all-origin dispatcher freeze is required.

2. Secure the resulting original backup and attachment before doing anything that
   might clip values. Copy the **exact original bytes** to every other origin,
   then stage them on each primary writer using a fresh backup path:

   ```sh
   hermes kanban --board default supervisor-requests --priority-operation stage \
     --writer ORIGIN --task LOCAL_TASK --manifest /absolute/copied-original.json \
     --backup /absolute/fresh-origin-backup.json
   ```

   Each JSON receipt gives `attachment_id`, `manifest_sha256`, `writer`,
   `backup`, and `origins` (every required writer with its open-row count).
   Use that map, not a hard-coded host list, to arrange operator sessions and
   stage/apply on every listed origin. All origins must report the same digest. Keep all receipts and verify
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

4. Let normal Board sync publish the outbox and inspect rejection/pending counts.
   Then run the read-only canonical verifier with the original bytes and digest:

   ```sh
   hermes kanban --board default supervisor-requests --priority-operation verify \
     --writer ORIGIN --manifest /absolute/fresh-original.json --digest SHA256
   ```

   It requires exact global open membership, ownership and target values through
   the final observed feed head; no local import or write occurs. Use `verify-restore` to compare
   original values after rollback. A local command receipt is not publication
   proof. Only after all origins match may the rollout
   owner install through the normal drain/update route. Verify no open stored
   priority exceeds 1000; reserve 901..1000 for deliberate escalation. Verify a
   genuinely eligible ready card's queue/claim under real capacity separately.

5. Guarded rollback uses the same local attachment/digest:

   ```sh
   hermes kanban --board default supervisor-requests --priority-operation restore \
     --writer ORIGIN --attachment-id LOCAL_ATTACHMENT_ID --digest SHA256
   ```

   Without `--allow-legacy-rollback` an above-cap original is restored at the cap
   (1000, same effective rank). The explicit flag restores the exact legacy value
   (policy rollback) only when a restore write is still needed; after a capped
   restore, adding the flag is a no-op and cannot recover exact originals.
   The sync pull may clip an exact restore again on any replica. Restore
   requires exact owned membership and
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
