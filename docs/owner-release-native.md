# Native original-owner release read-first protocol (in development)

This source is **not a deployed release route**. The Board `owner_release: 1`
capability remains unadvertised until durable HOME application, all authority
consumers, composed transport tests, independent review and deployment are complete.
Neither a canonical consumption certificate nor the local ACK below releases a
capability stop, changes spend, promotes a task, or authorizes an executor.

## Exact native invocation

The existing release command with an explicit `--episode` routes only through the
live owning-chat terminal's launch-private bridge. No bridge means refusal, not
standalone fallback. The parent captures the actual native actor and terminal
policy; a child cannot provide actor, HOME, journal UID or worker authority.
Explicit historical `--capability-episode` selection remains separate from the
current episode. `--seen` integer comment IDs are not supported on this route.

After canonical authorization, the parent reads the private current-valid comments
endpoint, strictly verifies the frozen intent/digest and complete live rows, and
journals an unpredictable nonce bound to the exact inventory. The CLI emits every
row and nonce as escaped JSON (including in text mode) and flushes stdout. Only
then does it submit a private, single-use, launch-local receipt for confirmation.
That receipt is not printed. Confirmation independently rechecks parent policy,
live custody, primary board/journal identity and fresh private comments before
recording emission. A lost/expired receipt requires redisplay of the same nonce
while the inventory is unchanged. Changed comments rotate the nonce and invalidate
all old confirmation bindings.

A later invocation may supply `--seen-release NONCE` alongside the **same exact
action**. It is accepted only against unchanged complete rows already confirmed
emitted on a different invocation. Supplying an unconfirmed nonce redisplays it;
that invocation cannot ACK its own subsequent confirmation. The reply label is
`acknowledged_pending_home`. The accepted ACK now freezes a separate delivery
journal and advances the durable intent to `delivery_pending`. No HOME consume or
application is implemented by this slice.

## Durable canonical carrier

`native_owner_release_deliveries` freezes the complete `task_events` row-upsert
operation before networking: distinct application/intent, carrier-event/delivery
and client-operation IDs, exact payload, HLC, timestamp and replay mode. The carrier
is the existing `operator_call_requested` event with `carrier: owner_release` and
`action: owner_release`; it deliberately has no generic worker caller. HOME must
use its separate private-intent verifier, not exempt it from worker authentication.
The ordinary operator handler currently refuses this unsupported action; publishing
it is **not** consume/application permission. Capability advertisement stays absent.

The native producer resolves its own frozen delivery before current authorization,
TTL or comments reads. Unknown/deleted/conflicting evidence cannot resend or mint a
new identity. Only authoritative `never_existed` with uncompacted history permits
sending the exact stored operation. A reply loss after commit reconciles the exact
canonical event and authenticated origin without resending. HTTP `applied` alone is
not publication proof. A previously confirmed event disappearing fails closed.
Terminal intent stages never send a carrier; missing/corrupt/multiple journals
cannot silently replace it. The intent's atomic `delivery_pending` marker also
fences loss of the delivery table/row. This is recovery integrity, not protection
against a same-UID actor rewriting all local journals.

This cut has one outstanding delivery per intent and refuses every unknown or
multiple historical row. The protocol's later definitive stale-comments refusal +
fresh display/ACK successor path remains unimplemented, as do private application
result publication/read and the HOME application queue. Never overwrite a frozen
delivery to implement that future path. Final escaped row-operation size is bounded
at 256 Ki characters, independently of the 128 KiB source comment bound; some valid
large/escaping-heavy inventories therefore refuse safely before the delivery/HLC
transaction commits rather than producing a permanently unsendable carrier.

## Trust and bounds

Flush confirmation means trusted CLI **emission**, not proof that a human/model
read the output. The parent does not observe stdout. The locator and receipt do
not sandbox arbitrary same-UID code or a keyholding gateway administrator; enrolled
trusted gateway/native CLI code remains the boundary. Comments are untrusted data,
not instructions establishing caller authority.

Private inventories retain Board bounds: at most 512 rows and 128 KiB of
JSON.stringify-compatible UTF-8 row bytes, without truncation. Escaping historical
Unicode in the reply can enlarge the envelope. Release replies therefore support
bounded 2 MiB length/digest framing on their private seqpacket socket, with a total
send deadline and receiver deadline; legacy create wire bytes and 64 KiB bound
remain unchanged. Malformed/truncated/reordered/digest-mismatched streams fail
closed. Over-capacity responses explicitly refuse instead of silently timing out.

Native journal and confirmation tests exercise reopen, changed rows, revoked
custody, earlier-invocation ACK, single-use/expired/foreign-launch receipts, failed
stdout flush and a complete 100 kB inventory through a real CLI subprocess/socket.
Their fake canonical adapter is **not** a composed Board DO-to-HOME deployment proof.

## HOME consume custody (still no application)

The dispatcher now gives native-owner carriers a separate bounded durable intake
and consume queue before worker-caller authentication. A generic operator refusal
or an advanced ordinary operator watermark cannot suppress this queue. Intake
captures at most sixteen payloads of at most 256 KiB per tick; two pending entries
rotate through authenticated processing. A missing local HOME row stays pending
rather than being permanently skipped during replica catch-up. Invalid observed
siblings do not acquire authority to suppress a valid authenticated delivery.

HOME checks the enrolled transport identity, local board/card HOME, canonical live
carrier fields and authenticated original gateway, and the exact private intent.
It freezes a distinct complete consume operation before networking. Immutable
carrier/wire custody and monotonic stages preserve that UID on reopen and reply
loss; no uncertain retry chooses a new identity. Exact private winning consumption
is required to reach `consumed_pending_apply`; copied public events or HTTP success
alone are insufficient. Expired/refused/unavailable evidence stays held. There is
no automatic successor, journal GC or broader-principal grant.

This queue does **not** release historical capability stops, write a local renewal
mandate, change budgets/status, publish an applied result, or confer coverage or
execution permission. Private consumption remains historical only. Applied-only
consumers, atomic HOME application, canonical result transport and full composed
CLI/DO/HOME/deployment proof remain required before capability advertisement.

### Atomicity primitives (not release authority)

`kanban_spend_budget.set_override_in_txn` and
`kanban_db_promote.promote_in_txn` let a certified HOME composer reuse the ordinary
budget fence/comment/event and promotion checks inside one canonical
`kanban_db.write_txn`. Public wrappers still own their transactions, and dry-run
promotion is unchanged. The promotion body rechecks current status, execution
restrictions and parents under the writer and retains the post-precondition CAS.
The internal bodies never commit. A caller must **raise on every returned
promotion refusal after any preceding write**, including a budget write, so the
outer transaction rolls back. An open SQLite transaction alone does not prove
writer ownership; the composer must use `write_txn`, not deferred `BEGIN`.

These bodies do not authenticate a foreign owner, certify scope coverage,
waive an unread decision, or install the missing HOME application. Real SQLite
composition tests prove combined commit and rollback on later refusal or actual
event-insertion failure, not end-to-end release authority.

### Private APPLICATION sidecar (assembler not yet installed)

`kanban_release_application` adds an immutable host-local APPLIED receipt distinct
from the consumed certificate. Only its own canonical `_writer` reservation may
record it; an arbitrary existing transaction is not accepted. Receipt insert,
UPDATE/DELETE and every replacement identity (including rowid) are guarded.
The complete bounded envelope freezes the winning private consumption hash,
exact captured HOME delivery/wire, canonical renewal, local result and every
referenced release/budget/placement event. Reads revalidate both halves through
SQL only and work with execute-only actionability adapters and read-only reopen.
Absent/malformed optional tables fail closed. Result replay uses frozen historical
identity, not a current task-status/clock inference. First receipt insertion also
requires exact local primary HOME identity. Ordinary local `append_event` writes
have a NULL Board origin until authenticated echo: a privately frozen local effect
may advance NULL→that exact HOME without invalidating its historical receipt. No
other fields or foreign origins are normalized, and canonical consumption/renewal
and delivery custody retain exact original stamps. This private rule is not public
row provenance certification; executor transfer remains a separate required gate.

The release-event set must be exactly current episode plus explicitly selected
historical capabilities, with original agent gateway/profile/session and chat
authority; it cannot grant blanket coverage. The result carries a separate
`owner_release_intent` mandate context. It does not invent a comment ID/digest,
attribute a HOME-written comment to the original author, or claim human authority.
Consumed-only certificates have no APPLIED proof. Public result/renewal events
cannot reconstruct a missing private sidecar.

Persisted readers now accept native scope coverage and release effects **only**
through private APPLIED verification. Native-field-family hybrids cannot borrow
legacy comment authority; genuine legacy real-comment/digest renewal stays intact.
`scope_renewal` exposes an explicit intent-backed mandate, `release` validates native
release records even on already-released replay, and actionability verifies native
capability-release effects rather than trusting their HOME label. Worker context
renders the intent reference without a fabricated comment ID. A successor stop
hides the old mandate without removing historical coverage; restricted accounting
floor remains cumulative. Decided-triage maintenance never reuses an application
renewal as permission for another placement.

### Atomic local assembler (not yet connected to production queue)

`kanban_release_assemble.apply` now exercises ONE real application transaction:
actual original-agent budget planning/attribution and budget body, exact selected
release effects, real internal promotion body, result event and private receipt.
Private receipt and actual APPLIED private-history veto run before COMMIT; a later
refusal rolls back task status, budget comment, events and receipt together. This
also refuses omitted historical capability stops rather than clearing them.
Unlimited cards do not receive a fabricated budget override. Dependencies remain
strictly refused for now; no dependency edge is deleted or silently satisfied.
Historical exact result replay is checked both before and inside the writer, so a
concurrent winner or later stop cannot turn replay into a second release.

Promotion admission is an immutable object whose constructor performs the actual
local actor/task/current-episode/selected-history/comment/claim/run/lease/hold
validation against private winning consumption. It binds exact connection, task,
proof bytes, delivery and unique writer invocation. Only the private promotion
composition seam accepts it; the public APIs never do. It projects only positively
identified current scope evidence. Ordinary persisted coverage stays APPLIED-only,
provider safety and administrative stops stay restrictive, and the cumulative
accounting floor never changes. Reusing the object in another writer, connection,
or task is refused.

**The production queue remains consumed-only.** A historical private consumption
GET does not certify the current canonical owner, holds, task or comments after
consumption. Local live checks cannot repair that distributed freshness gap. The
trial queue hook was removed rather than treating a cached consumed certificate as
canonical permission. Canonical current-application/result custody, APPLIED-result
publication/read and certified executor transfer must be composed before connecting
this assembler or advertising capability. Local comments compare every available
logical field, not foreign integer IDs; Board-only version/HLC metadata are not
present in the local schema and still need the canonical application boundary.
No completed real CLI/DO/HOME/executor transport or live recovery proof is claimed.

