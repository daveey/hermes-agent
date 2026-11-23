# In-process sessions display facts

`HERMES_FACTS_SESSIONS` defaults ON for Linux serve startup. `0`, `off`, `false`,
or `no` selects the existing legacy coalesced HTTP path at the next natural boot.
Other platforms remain legacy because the current native watch is Linux-only.
Never restart a live serve to activate this change.

## Data path

The app owns one `SessionsRuntime`, `ChangeFeed`, native source watch, immutable
`FactStore`, and the existing `InProcessFacts` delivery/expiry workers. Startup
captures the launch-authorized profile roster and installs an app-local HTTP
binding. A default sessions view must be published before release to facts mode.
The startup hook belongs inside the annotation-owner lifespan: sessions capture
must stop before its annotation inputs shut down.

Session/config native hints invalidate the demanded views. The source adapter
captures the canonical synchronous list/sidebar/stamps readers on the feed owner,
including SQL filters/counts/pins/deep offsets and seven annotation families.
This deliberately preserves existing serializers; it is NOT a pure reducer that
performs I/O and is NOT a per-row `SessionFact` implementation. Source vectors
identify these display captures, not a coherent multi-database cursor. Existing
schema/source-vector/dedicated-host guarantees remain in the follow-through scope.

HTTP only selects the startup binding, reads immutable bytes, touches in-memory
LRU state, or enqueues a cold demand. Cold or stale views return retryable 503 and
never scan synchronously. Invalid list enums retain 400. Named profiles outside
the discovered authorized roster return 404. The roster reconciles in background;
new profiles join that roster on a 30s reconciliation and get native watches at
the next boot. Their intervening updates use reconciliation, not native freshness.

## Bounds and failure behavior

- At most 32 retained selectors per endpoint; read-touch LRU makes stamps churn
  independent of list capacity. Encoded retained bodies share a 12 MiB cap.
- Individual captures are limited to 4 MiB and checked against a five-second
  elapsed budget. Oversized/slow views are unavailable, not truncated successes.
  The elapsed check does not interrupt an in-flight Python/SQLite reader.
- Dirty selectors are served fairly even under ongoing writes. Failed selectors
  retain old bytes as stale and retry after five seconds without poisoning peers.
- Capture is paced to at most five passes/second and no more than 50% serial
  capture duty. This is wall-time duty, not a CPU guarantee or live performance
  result. The live routed 20-sample p95 proof is still required.
- Quiet external annotations/time/roster reconcile every 30s plus queue/capture
  latency. `is_active` additionally schedules its captured 300s boundary. This is
  not an assertion of subsecond upstream freshness. Native annotation binding
  composition must be verified against the separately owned d activation.
- Facts expire after 60s if publication stalls. The retained legacy route remains
  available for explicit rollback, never as a hidden fallback on a facts miss.
- Shutdown first closes facts admission through the existing consumer drain,
  then joins capture/watch, releases binding/handle, and stops local workers.
  Failed joins retain app custody and prevent a replacement owner from starting.

## Evidence

`test_sessions_native_startup.py` exercises actual SessionDB writes, canonical
capture, mounted HTTP list/sidebar/stamps, real app lifespan/middleware, OFF and
reentry, and warmed request I/O tripwires. Comparison freezes the wall clock so
annotation observation timestamps are compared exactly rather than discarded.
`test_sessions_native_source.py` covers scheduling, failure isolation, LRU,
per-endpoint/byte limits, quiet reconciliation, deadline scheduling and closure.
The native watcher bypass mutant must fail the real-write visibility assertion.
Fixture success does not prove the deployed process, routed latency, complete
native annotation coverage, or the combined cost target.
