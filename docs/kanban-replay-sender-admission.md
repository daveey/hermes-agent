# Sender admission: local exclusion is not migration readiness

This is the dormant C sender slice. It does not activate certification, install
writer gates on a live board, or establish that old processes have drained.

## Public entry and detach

`kanban_sync_client.push` exports the replay sender's public entry. Its callers
include the ordinary sync round and direct retain, source-artifact, scope-renewal
and priority publication paths. The round lock alone does not serialize those
direct callers' HTTP sessions.

The public entry now acquires a distinct, nonblocking per-board sender lock before
transport selection and holds it through all session, send, settlement and ACK
work. A round may hold its own round lock and then acquire this sender lock.
Detach acquires round then sender before changing persisted mode and removing
capture triggers. Contention raises `SyncRoundBusy`; it neither waits on network
I/O nor mutates custody. A preopened connection attempting to send after detach
commits is refused before any peer call. Nonpersistent boards cannot send.

These are two different locks, not recursive admission. No process-local bypass,
runtime write hook, SQLite authorizer or reentrant writer reservation was added.
Older code that does not acquire the new lock remains an external-drain problem.

## Evidence

`test_kanban_replay_sender_process` spawns an independent process using the real
production Hub and a loopback HTTP peer. The peer accepts a session, or accepts an
operation, and withholds its HTTP response. While that child remains alive in the
exchange, a competing public sender and detach are each refused, with no competing
peer calls and an unchanged current database image. Releasing the response lets
the original child complete; a later round-held push and detach both progress.
The peer is controlled test infrastructure, not proof of the deployed receiver.

`test_kanban_sync_writer_process` preopens a separate process's SQLite connection,
then explicitly activates or upgrades the gate in a disposable fixture. Missing,
revision-0 and revision-1 functions refuse writes under minimum 2, preserving the
database image; revision 2 proceeds. The matrix covers both fresh activation and
an existing revision-1 gate, with the child connection opened before the upgrade.
This proves database downgrade refusal, not that a fleet's old processes or
already-read network bytes have drained.

The registered build revision is now 2: it distinguishes this sender's audited
pending provenance and whole-public-exchange exclusion from revision-1 builds.
It does not certify original captures. Merely opening a connection or updating
core neither installs nor raises a persisted gate; only explicit `activate`
inside the caller's transaction does so. The raise is monotone, preserves higher
minimums, rolls back with its outer transaction, and survives detach/reopen.
No production activation caller has been added.

The existing stale-session takeover control now invokes the internal strict
engine for its deliberately unfenced predecessor and the public entry for its
successor. Its fake peer injects the stale-session refusal; it is not independent
receiver-implementation proof. Exact pending-wire identity and settlement checks
remain intact.

## Frozen C interfaces (sender-only, dormant)

Creator disposition 290086 preserves core sender → receiver → integration merge
order. The sender fence/revision-2 proof above is accepted. Actual external
readiness is **not** a prerequisite this sender pretends to have proved.

### Migration evidence v1

`kanban_replay_migration.push(conn, hub, board, gateway, *, expectation,
public_key, receipt, now_ms, primary, max_batches=25, before_head=False, seen=None)`
is an **explicit dormant entry**, with no production caller. It acquires the same
sender fence, checks local admission/journal custody, verifies the receipt, checks
local Board/incarnation/principal/journal bindings, then enters the existing
sender. A missing/invalid receipt refuses before even capability/inspect I/O and
never selects legacy. Ordinary `kanban_sync_client.push` is unchanged: this is not
an activation, enrollment, live minimum-writer change or implicit migration.

The trust inputs are independent authenticated migration-owner context, **not
fields copied out of a receipt**. `public_key` is the 32-byte Ed25519 public key
independently pinned to `authority`, `key_id`, serving Board/incarnation and the
trusted Hub endpoint. No receipt-supplied key or `authenticated` flag is accepted.
Integration must provide the existing immutable authenticated Hub identity and
Board-scoped trust configuration, a fresh migration challenge after restart or
inventory change, authoritative complete inventory readback, and the trusted
current time. No trust root is installed here. This API deliberately trusts its
integration caller, not arbitrary caller assertions about the Board.

The exact `expectation` is a JSON object with **only** these fields:

```json
{
  "protocol": "sender_migration_evidence1",
  "authority": "independently-pinned-Board-authority",
  "key_id": "independently-pinned-key-id",
  "board": "board-slug",
  "board_incarnation": "serving-Board-incarnation",
  "gateway_principal": "authenticated-enrollment-principal",
  "journal_id": "bound-local-journal",
  "migration_id": "migration-identity",
  "challenge": "fresh-independent-challenge",
  "inventory": {
    "revision": 3,
    "coverage": "complete_current_and_former",
    "principals": ["current-principal", "former-principal"]
  }
}
```

All strings are nonempty. Revision is a positive safe JSON integer, not a bool.
Principals are the sorted, unique, nonempty **complete possible-exposure set**,
including the local current principal and all former enrollment/credential
principals. This is the receiver's authoritative inventory revision/readback,
not a set derived from whichever local candidate histories happened to survive.
Missing history stays UNKNOWN; this gate does not certify original provenance.

`receipt` is canonical RFC8785 JSON bytes (maximum 262144 bytes), with only
`payload` and `signature`. `payload` contains the exact expectation fields plus:

```json
{
  "issued_at_ms": 1000,
  "expires_at_ms": 61000,
  "readback_position": 12,
  "barriers": [
    {"principal": "current-principal", "barrier_id": "durable-barrier-1", "position": 10},
    {"principal": "former-principal", "barrier_id": "durable-barrier-2", "position": 11}
  ]
}
```

`signature` is standard base64 of Ed25519 signing the literal bytes
`b'hermes.sender_migration_evidence1\x00' + rfc8785.dumps(payload)` (the
`\x00` denotes one NUL byte). This domain/version attests **established durable
Board-serialized ingress fences**, not barrier requests, revocation wishes,
enrollment, paused gateways, timeouts or locally free locks. Every barrier must
remain effective against old ingress through the subsequent inspection/session
transition. Positions are positive safe JSON integers in the serving Board's
serialization order, **not sender operation sequences**; each is <= the signed
inventory/readiness readback position. Barrier principals must equal the complete
inventory list exactly, with no omission, duplicate or extra principal. Readback
is after establishment under that same Board authority and migration identity.

Freshness is `issued_at_ms <= now_ms < expires_at_ms` and a maximum 60000ms lifetime.
This is **admission freshness**, not a lease that permits fences to expire during
a send; actual barriers must persist. Reuse under the identical migration context
does not allocate another migration or make a receipt single-use. Integration
must obtain fresh context/readback on restart, changed inventory or readiness
loss; receiver readiness and ingress fencing remain authoritative. No durable
challenge-consumption store or remote barrier implementation is claimed here.

Fixtures sign with disposable keys. Tests reject missing, partial, forged,
stale, wrong-identity/inventory, noncanonical, oversized and malformed coverage
before peer I/O, and valid complete evidence reaches the unchanged sender. This
proves the **consumer**, not the authority or completeness of a real inventory.

### Durable diagnostic producer and delivery readback

`episodes.replace` now calls `kanban_replay_diagnostics.synchronize` in the same
writer transaction. Once the existing 300000ms reachable threshold is reached,
one durable `notice_key` is created per current `(Board incarnation, task,
origin, evidence fingerprint)` episode. The current observation/episode remains
the evidence source; retries and database reopen retain the same key and receipt.
There is no operation ledger. The observer's existing complete <=16-origin
membership validation bounds each task. Resolved or changed episodes retire old
keys, **not** mark them delivered; a later new episode, even if evidence recurs,
gets a new key. This deliberate episode distinction is not restart dedup failure.

`pending(conn)` exposes only current, due, undelivered evidence. Integration must
resolve the **existing authorized business owner**, then call
`bind_owner(conn, notice_key, gateway=..., profile=..., chat=...)`. The destination
is immutable per episode; there is no fallback to a managing chat or held card.
This call neither sends nor wakes anything. It returns the stable read descriptor:
`{id: notice_key+'-read', to_gw, profile, chat, keys: [notice_key]}`.

The frozen delivery receipt is the existing owner-transcript `notice-receipts`
envelope authenticated by `kanban_notify_reconcile_exec._evidence` (195–207):

- stored request body exactly echoes `id`, `to_gw`, `coll='sessions'`,
  `key=profile+'/'+chat`, `action='notice-receipts'`, `args.keys=notice_key`;
- trusted canonical `hub.db.sent` readback is `state='done'`;
- result has `code='notice_receipts'`, matching profile/session/keys,
  `session_exists=true`, and `delivered` contains that exact key.

`record_delivery(conn, notice_key)` reads through the existing canonical `_sent`
reader, validates that envelope, and revalidates current episode/evidence/owner
under the writer before persisting the receipt. Queue/sent/running/unknown flags,
missing sessions and absent delivered keys leave it pending. Mismatched envelopes
raise `ValueError` and preserve custody; integration must surface that refusal,
not substitute success. Repeated successful receipt consumption is a no-op.
This names `hub.db.sent` and the canonical owner transcript path as trusted
custody; a caller-supplied dictionary is **not** a production delivery API.

The indexed `kanban_replay_diagnostic1` table joins the recovery no-fill policy:
readable current custody survives; missing diagnostic/receipt rows are LOSS, not
stale-backup reconstruction. Real page-damage fixtures exercise that path.

## Explicitly unproven external acceptance and owners

- Receiver **t_01a2053828328f1b**: real authenticated migration identity and inventory
  readback; complete current+former principal barriers matching this contract;
  external old-process/in-flight drainage, restart readiness and persisted fencing.
  Local writer2/locks/process death never substitute for these.
- Integration **t_01a2053842e6e0eb**: provision/wire the trusted migration context and
  prove actual receiver receipts satisfy this dormant consumer before activation;
  transport pending diagnostic episodes through the existing owner transcript path
  and prove real delivered readback/dedup. This sender has not delivered a notice.
- D on this sender still requires aggregate fault/throughput and condition-mutation
  audit, fresh independent exact-head review, green required CI and canonical ship.
  Nothing is activated or deployed here. Section 9 rollback and original held-card
  acceptance remain in force; no courier/runtime hook/authorizer/reentrancy changes.
