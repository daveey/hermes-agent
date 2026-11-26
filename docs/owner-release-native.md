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
