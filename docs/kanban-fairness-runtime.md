# Fairness runtime receipts (Phase A integration)

The runtime observes actual urgency reads; it does not select owners, change scoring,
or fetch additional policy inputs. The existing observer remains the source of owner
history. These receipts are not evidence that a numeric fairness gate passed.

## Configuration

`kanban.fairness_capture` defaults to:

```yaml
enabled: false
capture_id: null
directory: null
max_bytes: 536870912
```

Parsing is side-effect free. Disabled capture starts no thread and activates no
journal. Enabled configuration alone is not an activation command. Activation must
supply the profile home, board, source gateway, board UID and clock uncertainty;
unknown uncertainty remains null, not zero. An enabled capture ID must be a safe
1–128 character path component. An explicit directory must be absolute. The default
is the profile's durable `kanban/evidence/<capture_id>`, never scratch. A byte limit
must be a positive integer no greater than 536870912; booleans are not integers here.

**Integration status:** the internal `activate()` API exists; production descriptor
arming, process-startup binding and assembled export are still integration work.
Do not use this document as an operational activation recipe.

## Actual reads and privacy

The consumer records the actual returned owner IDs, the read outcome and source
validity. Successful reads, expiry, missing files, invalid requests, malformed JSON,
oversize reads and IO failures are distinct observations. The production reader's
existing permissive behavior is retained, even where evidence validation is stricter.

Exact bytes are retained only for source documents passing the frozen allowlist.
Unknown fields, duplicate keys, nonfinite numbers and arbitrary malformed input are
not stored verbatim. Those observations retain the digest and observed byte count,
are redacted, and remain incomplete evidence. A digest of inaccessible bytes does
not establish the exact-byte acceptance gate. For oversize sources the digest and
`raw_size` cover the bounded prefix actually read (128 KiB plus one sentinel byte),
not the unseen whole file. Capture failure never changes the reader's result.

## Queue, storage and shutdown

Submissions do no filesystem/network IO and never wait for the writer. Queue limits
are 1024 records and 8 MiB of serialized payload; each payload is at most 256 KiB.
The writer drains at most 128 records / 1 MiB of payload per append call. These are
payload accounting bounds, not claims about Python RSS. Queue overflow records loss;
uncountable loss or an in-flight producer/reader at shutdown invalidates the tail.
Producer custody includes the rejection path, not only successful queue insertion.

All runtime epochs beneath a capture share a locked byte budget, including existing
files and temporary replacement peaks. Evidence is never deleted to make room.
Budget failure can prevent even a gap receipt; an absent clean tail remains a gap.
The lock is advisory and does not prevent unrelated external writes. Unsupported
locking platforms reject activation. Assembled-export enforcement is separate work.

Process shutdown shares one two-second allowance across all active profile/board
consumers. The writer first drains and seals data, then waits for a positive in-memory
publication decision from the closing caller. The caller may authorize only before
its deadline and with no known lost or in-flight observations. A timeout before this
decision is irrevocable: a subsequently completed data seal cannot certify the tail.

`Session.close()` returns **publication authorization**, not durable-publication proof.
After authorization it waits only for the remainder of the same allowance. Slow
receipt IO can finish later; a process exit may instead leave the receipt absent.
Only loading the durable `runtime-close.json` bound to the opening identity and final
seal establishes a clean tail. Missing, malformed, mismatched or late authorization
receipts remain `runtime_tail` gaps. The `completed_ns` value is the caller's decision
instant after data sealing, not the time receipt IO completed. No caller-side IO is
used to try to guarantee both bounded shutdown and durable acknowledgement.
