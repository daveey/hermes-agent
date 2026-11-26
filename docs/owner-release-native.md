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
`acknowledged_pending_home`, but the durable intent remains `read_first_pending`.
No consume or HOME carrier is implemented by this slice.

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
