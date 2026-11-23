# Isolated native sessions capture

Native sessions list, sidebar and stamps captures run in a fresh, owner-custodied
canonical subprocess, not in the serve interpreter. This removes competition for
the backend's GIL across canonical SQLite/policy/annotation calls. It does not
change the canonical SQL, ordering, policies or seven annotation implementations.

The ChangeFeed still owns capture and publication. Requests do no source I/O.
One helper per runtime may be active; selectors remain bounded by SessionsSource.
A private stdin/stdout protocol permits one request (16 KiB) and one response
(4 MiB), each with a four-byte network-order length. The parent checks length
before allocation, exact framing, JSON shape, EOF and child exit. Stderr is not
buffered. Child library stdout is redirected away from the protocol.

The five-second monotonic deadline includes command resolution, spawn, imports,
canonical reads, serialization and transfer. A fault raises CaptureUnavailable
with a bounded reason; SessionsSource publishes unavailable (or marks prior
real evidence stale), never an empty successful list. Existing source freshness,
retry, fairness and capture-duty controls remain unchanged. Each retry gets a
fresh child, so no inherited worker policy/annotation cache survives a change.

spawn_owned supplies process-group/owner-death custody and drain admission.
Timeout and runtime close kill/reap the child and clean remaining group members.
Failed teardown retains custody and refuses replacement. Shutdown cancels the
helper before joining the bootstrap/feed. A naturally rolled successor creates
its own runtime; the predecessor never transfers a live helper or DB handle.

Tests include real framed children, crash/truncation/oversize/invalid JSON,
timeout, close during capture, restart, missing executable, high file descriptors,
a real canonical store and all seven annotation calls. A separate regression
creates twelve stores with over 24,000 rows and exercises three fresh canonical
workers while a CPU spinner runs in the parent. In-process annotation Event
fixtures explicitly inject a reader to preserve their original holder/drain
fault boundary; they do not claim subprocess proof.

Capture success is NOT proof of HTTP availability within five seconds of unit
start. Listener startup, update-admission fences, proxy identity and natural
rollout must be measured independently. Keep operational containment until the
live gate passes; do not force a busy backend restart or change its budget.
