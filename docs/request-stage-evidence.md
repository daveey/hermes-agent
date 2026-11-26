# Request-stage evidence (local diagnostics)

This observer localizes Desktop JSON poll delays; it **does not repair transport**,
retry a request, reset an Agent, or select an SSH route. Both implementations
observe every request on these exact paths (all methods, no completion-rate cap):

- `/api/plugins/user-steps/requests`
- `/api/plugins/hub-sync/status`
- `/api/plugins/session-mentions/result` (including result POSTs; payloads remain excluded)
- `/api/plugins/kanban/board`
- `/api/sessions`
- `/api/health`

No request/response bodies, queries, URLs, auth headers, exception text, hostnames,
connection names or session identifiers enter the journal. A random 32-hex
`X-Hermes-Req-Id` identifies **one HTTP attempt**. Retries receive new IDs. A
Desktop `call_id` connects attempts to an outer API call; a coalesced caller may
share another call's underlying request. Do not count logical calls as HTTP attempts.

## Reading the records

Both sides write JSONL in their resolved Hermes home's `logs/`, with per-process
filenames: `request-stages-<pid>.jsonl` (backend) and
`request-stages-desktop-<pid>.jsonl` (Desktop). Each retains a current file plus
three rotations, 16 MiB each. Archive needed generations promptly: the journal
is bounded evidence, not a durable audit store. Filenames distinguish overlapping
backend generations; never inspect only the newest PID during rolling handoff.

Every row includes PID, a random boot identity, increasing `seq`, wall-clock UTC,
a monotonic clock, cumulative `dropped` and `io_lost`. Backend clocks use
nanoseconds, Desktop clocks milliseconds. Never compare monotonic clocks across
processes. Establish clock skew separately before cross-host wall-clock comparisons.
A background writer emits idle `coverage` rows about once per second. A blocked
writer also blocks coverage persistence, not requests. A missing/stale heartbeat,
a gap in sequence, nonzero/increasing loss counts, process termination or missing
rotations means **coverage unknown**, never “the hub did not receive it.” Loss
counts repeat in later rows after a writer recovers. No writer progress means no
claim about unpersisted records. Producers use a 4096-record bounded queue;
Desktop also has at most one 128-record batch in flight. No producer waits for disk.

### Desktop

- `api_call` / `api_end` / `api_error`: wrap the main-process API dispatcher,
  including registry, connection/down-gate, dial and coalescing waits. The
  power-settle admission before this dispatcher and renderer-side IPC queueing
  are outside this span. The observer records no exception text. Rejection identity is unchanged.
- `call`: one fetch attempt, before native grant minting. Its `port` is the
  selected backend origin's numeric port, useful for mapping SSH loopback listeners.
- `agent_queue`: the `ClientRequest` exists, before writing/end.
- `socket_assigned`: Node Agent supplied a socket; includes reuse boolean.
- `connected`: local/remote numeric socket ports are now available.
- `write_finished`: Node flushed to its **local** socket, NOT hub receipt.
- `response_start`, `response_end`, `response_close`: Node parsed headers/body;
  bytes count encoded HTTP payload, before JSON/delta/gzip decoding.
- `deadline`, `error`, `request_close`: deadline/transport lifecycle, no retry
  policy changes. A deadline before socket assignment is local Agent queueing;
  a long api_call→call interval is upstream of the HTTP attempt.

The observer covers Node's authenticated and public JSON helpers. Cookie-based
Electron OAuth HTTP and WebSockets are outside this journal; the incident's SSH
JSON polls use Node. A call with no attempt may be coalesced, rejected in a gate,
or use an uninstrumented transport; absence alone does not discriminate them.

### Backend

- `ingress`: outermost user ASGI middleware, before CORS/auth/session/body gates.
  It cannot observe bytes stalled before ASGI dispatch or an event loop unable
  to run. It is not a packet-arrival timestamp.
- `handler_dispatch`: innermost middleware immediately before router dispatch,
  after those gates. This is **not** a sync endpoint's threadpool-slot acquisition
  or lock acquisition; router/threadpool waits are in the following interval.
- `response_start`: before awaiting the server's send of response headers.
- `response_start_sent`: that send returned.
- `response_body`: before each body send, cumulative encoded payload bytes and
  final-chunk boolean. A blocked final send remains visible without an end event.
- `response_end`: final body send returned. This is handed to the ASGI server,
  **not** client receipt/TCP acknowledgement. Kernel/SSH return queueing can follow it.
- `app_exit`: finally, including errors/cancellation; not success by itself.

Missing, malformed or duplicated request-ID headers get a backend-generated ID
with `client_id=false` (legacy Desktop compatibility). Valid shared IDs have
`client_id=true`. A legacy request can only be matched by method/path/time with
explicit ambiguity; concurrent same-path requests cannot be uniquely paired.
No endpoint/query/body behavior changes and no header is echoed to a client.

## Deployment / incident gate

Ship through ordinary independent exact-head review and required CI. Verify the
**serving backend PID's boot commit**, not just checkout HEAD. Begin a bounded
natural hub-side sample once that side is live; do not wait for Desktop. It may
rule in measured backend delay, but unmatched legacy requests remain ambiguous.
Desktop tracing becomes available through its natural bundle updater; verify the
actual installed bundle stamp and new process evidence, never launch or restart
Desktop to obtain the sample. Match the same request ID on both sides before
claiming a precise outbound/return localization. Correlate ASGI send waits/bytes
with naturally observed TCP queue deltas, not cumulative retransmits alone.

A subsequent transport change still needs its real HTTP/WS recovery/no-mutation-
replay harness. Do not run a fresh 24h recovery soak until a fix exists. Rollback:
revert the instrumentation commit through the same ship gates; no config edits or
Mac-side manual activation are required.
