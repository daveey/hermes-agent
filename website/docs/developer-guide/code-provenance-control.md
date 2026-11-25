# Private code-provenance diagnostics

`code-provenance` is a protocol-1 **private control-socket** verb. It has no HTTP
route, session ticket right, workflow operation or authorization receipt. The
existing transport authenticates the same-UID Unix peer (or native pipe peer).
The caller cannot supply or override that identity. Control dispatch treats this
one verb as passive: it does not reserve work, arm a quiet episode, acquire a
turn lease, restore a session, enroll/reload a plugin or change an authority epoch.

The installed bootstrap ledger is the only source. Missing/unsupported ledgers
refuse; the reader never calls `install`, resets unknowns or reconstructs execution
history from current files. Inventory may latch additional sticky refusals, exactly
as an ordinary integrity observation can. An unsupported **launch route** remains
readable with its original refusal; an unsupported ledger implementation does not.
A diagnostic export cannot make capsule owner/child verification succeed.

## Requests

Capture, with exact keys:

```json
{"protocol":1,"id":1,"verb":"code-provenance","params":{"schema":1,"op":"capture","expected":{"pid":123,"start_time":456,"instance_id":"opaque-runtime-instance"},"refresh_inventory":true}}
```

Page, with exact keys:

```json
{"protocol":1,"id":2,"verb":"code-provenance","params":{"schema":1,"op":"page","snapshot_id":"64-lowercase-hex-characters","section":"unknowns","offset":0,"limit":128}}
```

Booleans are not integers. Unknown keys, unknown sections, malformed offsets and
limits refuse. The echoed envelope ID is absent/null, a signed 64-bit integer, or
a string of at most 128 characters. Protocol/schema must be integer 1. No request
may select a different peer or disable inventory.

Successful result objects always have `purpose: "diagnostic_only"`. `complete:
true` means the retained metadata snapshot was completely encoded within bounds,
**not full provenance or trusted/clean code**. `full_provenance` is always false;
`incomplete` explicitly names unavailable historical provenance. Error results have
`complete: false` and no usable snapshot token. Invalid/authentication envelopes
are rejected by the existing outer transport error envelope.

## Sections and provenance

- `unknowns`: every raw sticky reason, unfiltered.
- `quiet_blocking`, `quiet_extension`: partitions derived from that same locked view.
- `components`: registered canonical roots, one row per reviewed relative SHA256
  file-map entry, and core boot-tree relative Git blob identities (not disk hashes).
- `dependencies`: every registered root and every retained installer receipt,
  including unused receipts. Each distribution is qualified by its physical root.
- `executed_units`: retained core/dependency execution and native-load metadata.
- `producers`: retained available generator and unknown-producer source metadata.
  A producer receipt does not authorize its output.
- `metadata`: actual ledger boot SHA/root/route, recorded interpreter/trusted seed
  identities, exact boot-registry Git blob identity or explicit unavailability,
  fresh disk HEAD/error, index limits and explicit unavailable history.

Only named metadata fields with JSON-safe scalar values are copied. Arbitrary
receipt keys, code objects, source, constants, frame locals, environment, selected
configuration and credentials are not exported. Root/file/receipt membership is
not inferred from an installation census. Disk HEAD reads bounded Git metadata;
it does not establish a clean worktree or repair boot identity. A missing field
is never manufactured from disk. The summary includes the capture's generation,
monotonic begin/end, process/start/instance identity, current identity/generation,
raw count/digest and per-section counts/digests. Current generation is a lock-free
scalar observation and can advance again immediately; it is not an integrity gate.

## Bounds and consistency

One immutable in-memory snapshot per authenticated peer; no persistent server
snapshot files. New capture replaces that peer's prior token. Tokens are bound to
peer, installed ledger and process/start/instance. They expire 30 seconds after
publication; expiry is checked on every page and stale storage is lazily removed.
A new live generation does not mutate old pages. Page requests never inventory or
recapture. Restart, replacement ledger, cross-peer use and expiry refuse.

Capture has a fixed two-second budget, including bounded acquisition of the existing
ledger lock, inventory, fresh disk metadata and encoding. One capture is in flight
globally, including publication. Capture runs off-loop on a single-flight-owned
daemon thread, not an accumulating executor queue. Identify and already-captured
pages do not wait for that worker or its inventory lock. Python cannot cancel one
blocked filesystem syscall: the caller receives `capture_timeout`, but global
custody remains held until the actual worker ends. Further captures return busy;
late positive results are discarded. No production deadline/cap is increased.
A root that exceeds this budget needs a measured, separately reviewed owner decision.

Snapshot accounting is incremental with a 16 MiB encoded cap, including fixed
manifest headroom. Escaped scalars are preflighted in bounded chunks before whole
entry encoding. An entry above 240 KiB is rejected rather than split or dropped;
this leaves envelope space below the 256 KiB full-page cap. Pages also contain at
most 128 entries and remain below the existing 512 KiB transport response limit.
Overflow, inventory/lock errors and oversized entries are explicit incomplete
failures, never truncated successful captures. Empty exhausted pages have `next:
null`; otherwise `next` is exactly the first not-yet-returned offset.

## Canonical encoding and supported collector

Canonical JSON uses `ensure_ascii=True`, sorted object keys, compact `,`/`:`
separators and `allow_nan=False`. Each section is ordered lexicographically by
canonical entry bytes; identical retained entries remain distinct. Its digest is
SHA256 of its complete canonical JSON **array**, including `[`/`]` and separators;
empty sections hash `[]`. Counts include every entry. `raw` equals the `unknowns`
manifest. Hashes cover metadata, not an authorization claim.

`hermes_cli.code_provenance_client.collect(home, expected)` uses authenticated
transport, a five-second per-request deadline and a 512 KiB actual-byte read cap.
It rejects mixed snapshots, changed manifests, cursor gaps, aggregate overflow and
count/digest mismatches. The ordinary ticket client's smaller 64 KiB response cap
is unchanged. To emit a verified document to stdout after normal deployment:

```sh
python -m hermes_cli.code_provenance_client /path/to/gateway/home
```

The client identifies the existing process first, then collects all sections within
the short TTL. It never changes deadlines or retries a failed capture silently.
Fixture success is not a production-root capture, native proof or activation.
