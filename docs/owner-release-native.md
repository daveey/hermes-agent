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

Marker erasure cannot downgrade a **retained private release-effect UID** to legacy
HOME/comment authority. The local release and actionability readers inspect only
exact direct UIDs in the same task's immutable HOME `effects` or executor `events`
envelope; receipt validity is not required to deny fallback. Public UID mentions,
notes, other-task evidence and consumption-only receipts do not classify a release.
Optional absent schemas preserve legacy compatibility; malformed same-task custody
holds conservatively. The scan permits at most 64 envelopes, each within its existing
2 MiB HOME / 4 MiB executor cap, and 35 HOME / 38 executor effects; overflow holds,
never truncates to permission. These resource limits may hold a genuine legacy
release following unusually large or damaged native history. They do not establish
indexed executor lookup or amortized diagnostic performance.

Consumption alone has no release-effect identity. An already damaged/imported store
without retained effect-bearing custody can make an erased native release
indistinguishable from a genuine later legacy operator release. This correction does
**not** resolve that integrity boundary or authorize reconstructing receipts. Native
assembly/materialization writes effects and APPLIED atomically; ordinary canonical
immutable-row ingress remains required. Historical/defensive reader tests are not
proof of an admitted production attack or a deployed recovery.

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

The local assembler is not directly connected to consumption. A historical private
consumption GET does not certify current canonical owner, holds, task or comments.
Instead the production queue now uses rollback-only PREPARED, the independently
validated Board final application and certified materialization described below.
Local comments compare logical fields; Board-only version/HLC metadata remain at
that canonical boundary. No completed real CLI/DO/HOME/executor transport or live
recovery proof is claimed, and capability advertisement remains absent.

### Rollback-only prepared proposal

`kanban_release_prepared.prepare` runs that same real `_compose` body inside a
SAVEPOINT under the application writer, captures the complete task before/after,
actual generated events (including wake evidence), budget comment and verified
application envelope, then rolls back **all** trial effects. Only an append-only
proposal survives. Existing task/comment/event/outbox/APPLIED rows remain exactly
unchanged; there is no provisional ready state or capture/sync exemption.

Its immutable replay is historical custody, not current applicability or permission
to materialize. Malformed/corrupt custody refuses rather than generating another
proposal. A later canonical final-application operation must independently validate
current original owner, full canonical comment inventory, task, exact current and
selected historical episodes, holds, dependencies and independent financial limits.
Neither this proposal nor historical consumption authorizes dispatch or increases
an allowance. The Board final operation and HOME certified materialization now
exist; original native completion now retains separate historical custody, while
executor transfer and composed route proof remain incomplete.

### Frozen final wire and certified HOME materialization

`kanban_release_final` freezes the complete distinct `apply_owner_release` operation
and UUID against the exact verified PREPARED proposal before HTTP. An independent
once-set queue digest detects missing/corrupt wire custody and prevents reminting.
Historical saved-wire/private-result reconciliation precedes mutable owner/comment/
TTL checks. Fresh enrolled HOME/board/card identity still gates every network stage.
The bounded private `/application` GET is separate from source parsing: complete
responses allow 8 MiB while proposals retain their existing 2 MiB cap. Strict
validation binds the exact operation, winning private consumption, ordered event/
comment UID and payload inventory, canonical stamps, original actor and protected
finite allowance observation. Public result rows cannot substitute for this GET.

`kanban_release_materialize` imports only canonical comments and events through the
real `apply_remote_changes` writer. It never imports the certificate's historical
task image. Its final in-transaction callback verifies every actual materialized
row, preserves the current task image, refuses unanswered row transport and cursor
races, and inserts immutable private certificate plus APPLIED last. Any refusal
rolls back the complete page; comment integer IDs resolve from portable UIDs.
Post-COMMIT uncertainty reopens the exact receipt without replaying effects.
Completed receipts suppress queue work, not independent holds or execution gates.
The immutable queue stage stays `consumed_pending_apply`; APPLIED determines
historical completion, never a destructive transport-stage overwrite.

Tests cover the actual Board-exported certificate, real bounded Hub HTTP, real
SQLite remote-apply rollback/reopen and reply loss. The SQLite Board adapter is
synthetic: these are **not** composed CLI/socket/DO/HOME/executor or deployment
proof. Authenticated executor proof transfer and full composition remain required
before capability advertisement or live recovery.

### Application v2 rollout and rollback contract (not activation approval)

Deploy readers before writers: Board final-application validation and authenticated
historical GET, HOME/origin private-certificate readers, and executor transfer
readers must understand both v1 and v2 before a Board advertises
`owner_release_application: 2`. In the HOME transport path, that strict integer
capability gates new v2 freezing and every v2 POST. Missing or non-integer advertisement selects v1 only
for a new, unfrozen proposal; it never rewrites a retained proposal or wire.
Keep the separate native `owner_release` advertisement absent until composed
transport, independent review, deployment readback and the existing recovery gates
are complete. This document does not authorize a signer, principal, key, counter,
governance activation or live release.

`prepare` and `freeze` default to v1; their optional `application_version` accepts
only integer 1 or 2, never booleans, floats or strings. A valid replay returns its
exact immutable original version even if the caller requests the other supported
version. V2 adds exactly `accounting = {semantics: core_home_ordinary_v1,
spent_runs: N}` to the proposal, where N is a safe nonnegative integer captured
from the single actual ordinary HOME budget-plan invocation. The trial still
rolls back all effects. The nested application receipt remains v1. The final v2
certificate replaces v1's `allowance` field with exactly either
`governance = {state: ungoverned}` or `{state: governed, allowance: ...}`; governed
allowance validation remains mandatory. V2 accounting is not a new grant or reset
of cumulative spend, and ungoverned is not a waiver of existing independent holds.

For rollback, withdraw v2 advertisement before stopping new v2 sends, but retain
v2 readers and historical GET support for already-frozen or committed records.
Verified completed local history and authenticated remote history resolve without
current advertisement. After remote 404, an unadvertised saved v2 stays held with
its exact UID/digest and sends nothing: never downgrade, remint or replay effects.
Capability failure also holds new v2 sends, not already-verifiable history. Do not
roll back to v1-only readers while any v2 custody remains; preserve immutable
journals, receipts and private materialization rather than deleting or converting
them. Rollback does not clear a later stop or confer executor permission.

### Original native completed-result custody

The captured original parent now resolves a saved completed result or, for a frozen
delivery, the authenticated private `/application` GET before mutable carrier rows,
comments, capability or TTL reads. Only actual HTTP404 permits pending publication;
unknown, malformed, unauthorized and oversized replies remain holds. The complete
certificate must validate internally and match the local original intent, delivery
UID, frozen comment inventory and acknowledged emitted nonce/read journal. Missing
or changed historical read custody is refused rather than repaired with current
comments. Emission is still not proof a person/model read the content.

`kanban_release_result` owns a separate append-only origin result store. ONE writer
commits the complete private certificate, independent immutable journal digest and
terminal applied bookkeeping together; no intermediate consumed clearance exists.
Missing receipt/marker halves, changed bytes and inconsistent stage never remint or
reconstruct the result. SQLite UDF guards on the shared intent journal are restricted
to `UPDATE OF result_digest`, with a separate UDF-free completed-row UPDATE fence:
ordinary new and pending intents can still progress on reopened connections.

The result reports `release_stage=applied`, `historical=true` and
`execution_clearance=false`. Each replay still validates live original parent,
primary board, transport gateway and original frozen local custody. It imports no
HOME queue, task postimage, event, budget, allowance or executor permission. A later
scope/hold is not renewed by returning an earlier result. Actual CLI/socket tests
exercise completion, missing replies, reopened connections and a successor's full
read/ACK/delivery path; their rebound private Board fixture is explicitly not a
composed real DO/HOME/executor test or deployed recovery proof.

