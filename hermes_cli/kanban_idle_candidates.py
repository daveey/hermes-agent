"""Memoize structural candidates, never time or process-liveness decisions."""
from pathlib import Path
import sqlite3

from hermes_cli import kanban_activity_revision as revision


def event_candidates(conn: sqlite3.Connection, statuses: tuple[str, ...], kinds: tuple[str, ...],
                     *, exclude_statuses: bool = False) -> bool:
    """A positive answer always runs its handler, even without local writes."""
    return bool(candidate_task_ids(conn, statuses, kinds, exclude_statuses=exclude_statuses))


def candidate_task_ids(conn: sqlite3.Connection, statuses: tuple[str, ...], kinds: tuple[str, ...],
                       *, exclude_statuses: bool = False) -> tuple[str, ...]:
    """Retain an observer so fresh dispatcher connections share the structural memo.

    Caller transactions see their uncommitted writes. File/WAL generation fences
    belong to the shared reader; no stat-only cache of commit-visible data.
    """
    from hermes_cli.kanban_reader_retention import reader

    if not conn.in_transaction:
        path = conn.execute('PRAGMA database_list').fetchone()[2]
        if path:
            try:
                with reader(Path(path).resolve()) as observer:
                    return _on_connection(observer, statuses, kinds, exclude_statuses)
            except (sqlite3.Error, OSError):
                pass  # use the caller's connection if the optional observer is unavailable
    return _on_connection(conn, statuses, kinds, exclude_statuses)


def _on_connection(conn, statuses, kinds, exclude_statuses=False):
    stamp = None
    if not conn.in_transaction and hasattr(conn, '__dict__'):
        try:
            stamp = revision.read_key(conn, ('tasks', 'task_events'))
        except sqlite3.Error:
            stamp = None  # a failed optimization must not suppress the real query
    key = (statuses, kinds, exclude_statuses)
    cache = getattr(conn, '_idle_candidates', {})
    if stamp is not None and cache.get(key, (None,))[0] == stamp:
        return cache[key][1]
    operator = 'NOT IN' if exclude_statuses else 'IN'
    # [daveey] G2: drive from the event-kind index (a handful of rows) rather than probing it once per task in the
    # status set (~6k done/archived cards on the hub board, ~5 ms a call, 5 calls a tick). Same rows.
    found = tuple(row[0] for row in conn.execute(
        f"SELECT t.id FROM tasks t WHERE t.status {operator} ({','.join('?' * len(statuses))}) AND t.id IN "
        f"(SELECT e.task_id FROM task_events e WHERE e.kind IN ({','.join('?' * len(kinds))}))",
        (*statuses, *kinds),
    ).fetchall())
    if stamp is not None:
        if len(cache) >= 16:
            cache.clear()
        cache[key] = stamp, found
        conn._idle_candidates = cache
    return found


def sweep_shared_blockers(conn, result) -> None:
    """Skip proven-empty lanes; positive candidates still obey time-based policy.

    [daveey] G2: each lane also skips while the board's content revision, this gateway's id and the config hold
    and none of its clocks ran out (``kanban_sweep_gate``). Both lanes read only content rows (``task_runs``
    outcomes and metadata, statuses, events). Their clocks: the probe interval of a blocker the probe lane deferred,
    the 14-day lookback, whose oldest assertion leaving the window reruns both lanes, and the route recheck of an
    open provider-scoped blocker (``ROUTE_RECHECK_SECONDS``): the probe lane's scope test reads routes, whose
    config/wall inputs are not in the key."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_shared_blockers import probe_stranded_blockers, release_cleared_blockers
    from hermes_cli.kanban_sweep_gate import config_key, gated
    from hermes_cli.kanban_wake_check import own_gateway

    def probe(c):
        due: list = []  # lookback expiry of the oldest assertion + each deferred blocker's next probe moment
        out = probe_stranded_blockers(c, due_out=due)
        return out, min(due, default=None)

    def release(c):
        due: list = []
        return release_cleared_blockers(c, oldest_out=due), min(due, default=None)

    from hermes_cli.kanban_shared_blocker_checks import run_due_checks

    def check(c):
        due: list = []
        return run_due_checks(c, due_out=due), min(due, default=None)

    try:
        gated("dispatch.shared_blocker_checks", conn, check, conn, skipped=([], None),
              extra=lambda c: (own_gateway(c), config_key()), run_due=lambda r: r[1])
    except (OSError, sqlite3.Error, ValueError):
        kb._log.warning('kanban: shared blocker checks failed', exc_info=True)

    for statuses, handler, field in (
        (('blocked', 'scheduled', 'ready', 'review'), release, 'shared_blocker_released'),
        (('blocked',), probe, 'shared_blocker_probed'),
    ):
        try:
            if conn.execute(f"SELECT 1 FROM tasks WHERE status IN ({','.join('?' * len(statuses))}) LIMIT 1",
                            statuses).fetchone():
                out, _due = gated(f"dispatch.{field}", conn, handler, conn, skipped=([], None),
                                  extra=lambda c: (own_gateway(c), config_key()), run_due=lambda r: r[1])
                setattr(result, field, out)
        except Exception:  # health: allow BLE001 -- each independent maintenance source must leave the tick alive
            kb._log.warning('kanban: %s failed', field, exc_info=True)
