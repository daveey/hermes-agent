# Sync CPU partition

The always-on loop accounting snapshot includes `sync.cpu.local/loop`,
`sync.cpu.board_call/loop`, and `sync.cpu.total/loop`. These measure completed
iterations of the synchronous sync loop with `time.thread_time`, including
selection, handle maintenance, outbox checks, board work, and CPU consumed by
`wait_for_work`. Blocked wall time consumes no thread CPU.

Only synchronous `Hub._call` is excluded from local CPU. This includes request
serialization, HTTP/TLS, retries, redirect fallback, response decoding and error
handling. Nested calls count once. Calls on other threads or outside a service
are not attributed. Attachment preparation and caller-side waiting remain local
(conservative); detached attachment transfers, metrics sender and feed watchers
are separate threads, not falsely subtracted from the calling thread.

`sync.cpu.{local,board_call,total}/board/<slug>` is a nested subset of the loop,
not an additional cost. Existing `sync.board/*` and `sync.parents/*` retain their
inclusive semantics. The total partition scope is slightly wider than the old
board pass, since it includes that pass's bookkeeping. Do not subtract one family
from the other. The asynchronous links pass is also a separate thread: these
counters alone make no assertion about its CPU or the entire gateway process.

## Reading an authorized observation

Use two fresh snapshots from one PID/process-start identity, after deployment and
normal gateway restart. Require a sufficient elapsed interval and unchanged code
identity; retain both raw snapshots. Subtract cumulative CPU and runs, divide CPU
seconds by elapsed seconds, and multiply by 100 for percent of one core. Never
subtract a historical network estimate. The local counter is an exclusive
measurement, not a residual inferred from other threads.

The three counters publish separately, once at service exit. Require equal runs
for the three members at **both** endpoints before reconciling total = local +
Board-call. A snapshot taken during publication is not a usable reconciliation
endpoint. In-flight iterations are not published until completion, so these are
completed-service deltas, not exact integration over snapshot timestamps. Keep
this boundary qualification with the result. A shutdown closes the suspended
iteration in finally; a pre-fork scope is not mixed with a child's CPU clock.

The local-loop measurement is wider than the original per-board envelope but
still thread-scoped. Do not present it as all-thread gateway acceptance. Preserve
any task-specific side-thread accounting requirements and correctness gates.
