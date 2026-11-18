# Passive local-backend stall attribution

`hermes_cli.loop_accounting` adds two bounded diagnostic sections to its existing
version-1 process snapshot. They identify a measurement gap, not an incident fix.
A stack captured after a control-beat delay, or a task awaiting an executor,
does not identify the callback that blocked the loop.

## Reading an existing backend

Use the PID of the affected backend, not a new Python process. Its existing writer
atomically replaces `<root Hermes home>/run/loop-accounting/<PID>.json` at
`loop_accounting.interval_s` (default 60 seconds). No new polling thread is added.
For example, with `ROOT_HOME` and `PID` set to that backend's root home and PID:

```sh
jq '{pid, process_start_time, started_at, written_at, diagnostic_code,
     slow_callbacks, http}' "$ROOT_HOME/run/loop-accounting/$PID.json"
```

`pid`, `process_start_time`, and `started_at` identify the process. `diagnostic_code`
contains `files` and `sha256`, a once-per-process SHA-256 over the diagnostic source
filenames and bytes (not a Git revision). This permits a comparison with the
installed source without assuming a later checkout update changed a running process.
A source file unreadable at first sampling gives `sha256: null`.

For a 30-second investigation the incident owner can set the existing
`loop_accounting.interval_s` setting to 30 before backend startup. Do not treat two
reads of the same file as a measurement interval. Copy two distinct snapshots;
compare process identity and code digest, then subtract cumulative counters using
`http.monotonic_ns` for the **actual** elapsed interval. Scheduler/writer delay may
make this longer than 30 seconds: retain both timestamps, never label it exactly
30 seconds merely because that was the requested cadence. No request generator is
needed; synthetic fixture traffic is not ambient production evidence.

## `slow_callbacks`

- `threshold_s`: 0.5, inclusive threshold, measured by `perf_counter` around each
  `asyncio.Handle._run` invocation (one coroutine step, not its awaited lifetime).
- `slow_callbacks`: cumulative count above threshold.
- `identities`: cumulative slow counts for at most 64 code identities plus `other`.
- `identity_overflow`: observations whose new identity exceeded that bound.
- `recent`: latest 64 records, each containing `identity`, timezone-aware UTC
  `occurred_at` (callback start estimated from completion wall clock minus measured
  duration), `wall_s`, and `inclusive_cpu_s` (`thread_time`, including nested scopes).
- `records_dropped`: cumulative records evicted from the recent ring.
- `emission`: `existing snapshot cadence only`. No per-callback logging or separate
  emission loop: a burst cannot produce more writes than the existing writer.

Code identity is `checkout-relative-file:first-line:code-qualname`. Partial
functions are unwrapped without reading arguments. Task steps use coroutine code,
never task names or frames. Foreign/native/callable-object code uses `other`.
Names/paths have bounded lengths; no repr, locals, request arguments or exceptions
are copied. The existing `loops["loop_cb/..."]` CPU counters remain **exclusive of
nested accounted work**. Inclusive diagnostic CPU is never added to residual totals.
A sleeping callback has large wall time and little CPU; time suspended awaiting
an executor is not charged as callback wall time. OS scheduling delay can also
increase wall time: a record is attribution evidence, not proof of why it blocked.

## `http`

`unit` is **ASGI body bytes**, not network wire bytes, compression on the other
side of the wrapper, headers, TLS or framing. WebSockets and lifespan are excluded.

`totals` and each row in `routes` have these integer fields:

| Field | Meaning |
|---|---|
| `requests_started` | Global: wrapper entry. Route: request classification. |
| `response_finished` | Final `http.response.body` successfully sent. |
| `failed` | Exception or return without final body (unless disconnected/cancelled). |
| `cancelled` | Cancellation, or disconnect followed by incomplete clean return. |
| `inflight` | Started minus the three terminal outcomes (a gauge). |
| `request_body_bytes` | Bytes returned by application-consumed `http.request` messages. |
| `response_body_bytes` | Body bytes only after successful underlying `send`. |

`unclassified_inflight` is a gauge. At every snapshot:
`totals.inflight == unclassified_inflight + sum(routes[*].inflight)`.
`monotonic_ns` and `time_ns` are captured with the counters under their lock.

The outer wrapper has no resolved route. Its fixed allowlist matches literal paths
and explicit one-segment placeholders; keys are constants such as
`GET /api/sessions/{session_id}/messages`, never raw URL/path/query/header/user IDs.
At most 64 named keys plus `other` exist. A matched request is classified only after
successful 2xx `response.start`; auth failures, redirects, 404s, unmatched paths and
termination before headers use `other`. Until classification, global counters count
immediately and only an integer consumed-byte count is buffered for later route
attribution. Route request bytes/starts can therefore fall in a later interval than
their global observation. **No cumulative counter is decremented to reclassify.**
Use global deltas for exact observation-time totals, classified route deltas with
this boundary caveat for per-path attribution. Inflight gauges may decrease.

Unread bodies are never drained. Chunk order, streaming, backpressure, errors and
cancellation propagate unchanged. A failure after a successfully finished response
does not count a second terminal outcome. An app that omits the required final body
is not called finished, even for HEAD/204.

## Fixture proof and bounds

`tests/hermes_cli/test_loop_callback_latency.py` exercises controlled sleeping vs
executor-await, inclusive vs residual CPU, exception behavior, code identity privacy,
overflow and snapshots. `test_http_body_accounting.py` exercises actual ASGI message
streams, failed sends, unread bodies, concurrency, long-lived streams, cancellation,
status normalization and monotonic deltas across classification.

Memory is bounded by 64 slow identities plus `other`, 64 recent records, and at most
65 HTTP rows. Existing CPU-accounting caches retain their existing bounds. Each
active HTTP invocation adds fixed-size state and integer byte counts, not bodies or
an ever-growing request registry; live invocation memory scales with actual inflight
requests. Counters are cumulative Python integers. Forked processes reset diagnostics.

An isolated Linux/Python 3.14 fixture (five trials of 10,000 operations; median thread
CPU, no server or database) measured a no-op callback at 101 ns uninstrumented versus
1,725 ns with **both existing CPU and new wall instrumentation**, and a consumed
5-byte/sent 5-byte two-chunk ASGI request at 407 ns plain versus 10,635 ns observed.
These are fixture costs, not production load or incident-recovery proof. The controlled
sleep sample recorded `wall_s: 0.5101`, `inclusive_cpu_s: 0.000037`; its request totals
were started=1, finished=1, consumed=5, sent=5, inflight=0. Full machine-generated
benchmark/snapshot evidence is carried by the implementation task.
