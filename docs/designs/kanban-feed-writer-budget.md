# Feed writer reservations

`pull()` applies ordered feed pages in prefixes of at most 32 rows. Each prefix
commits its rows and cursor together. Intermediate cursors are feed `seq` values;
the final prefix commits the response's `next` (which may skip partition-filtered
rows). Non-increasing/missing sequences are accepted only when the complete page
fits one reservation; they cannot be used as resumable prefix cursors.

Each reservation has a 250 ms monotonic budget. Python checks between rows and at
transaction exit complement SQLite's progress callback every 1,000 VM opcodes.
An over-budget prefix rolls back, halves its row limit, and retries in order.
A single over-budget row fails the pull without advancing its cursor. The next
round retries that row, not an uncommitted suffix. This is a cooperative bound on
application/VM work, not a real-time guarantee against OS descheduling, one slow
SQLite opcode, rollback I/O, or COMMIT/fsync. No network I/O runs in a feed writer
transaction. The atomic repair/snapshot API remains available unchanged.

Schema columns are prepared outside each prefix reservation (landed page-writer
cache) and reused for that reservation.
A 100 ms scheduling opportunity after each reservation avoids immediately retaking
 the writer ahead of an already waiting dispatcher, including SQLite's busy-handler
 backoff (a 5 ms gap can fall entirely between its retries). SQLite has no FIFO guarantee;
the contention regression checks measured dispatcher acquisition, while the
separate dispatcher-side timeout remains the ultimate admission-wait bound.

Before prefixes begin, authenticated containment receipt observations establish
conservative holds for the entire page. A stop event in a later prefix must not
allow an earlier ready task row or its post-commit hooks to expose claimability.
A crash after a prefix retains both the cursor and these durable holds; a release
only becomes effective through the existing certified event application.

## Outbox service during catch-up

A sync round still pushes before pulling. It additionally offers the same send
policy between feed prefixes and before fetching each page whenever five seconds
have elapsed since the last service. Therefore applying a large page cannot
suppress a send opportunity beyond five seconds plus one bounded reservation
(and its post-commit observers). HTTP fetch/certification and snapshots retain
their existing transport deadlines; these are not preempted by the VM budget.
The opportunities apply during replica and live-file pulls, and never occur
inside their writer transactions. Send counts accumulate into the round result.

These sends deliberately retain `before_head=True`, terminal status protection,
FIFO, rejection resolution and reserved-key handling. A run row intentionally
held until head is not made safe to push by a timer. Thus the bound is on
**eligible push opportunities**, not acknowledgement of every outbox row despite
an unavailable Board, retained safety hold or transport failure. Catch-up keeps
its existing 20-page budget and immediate next-round selection. The 30-minute
live acceptance must separately establish actual oldest pending age below 300 s;
unit timing alone is not a delivery claim.
