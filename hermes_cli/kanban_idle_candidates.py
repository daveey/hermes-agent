"""Memoize structural candidates, never time or process-liveness decisions."""
import sqlite3

from hermes_cli.kanban_blocker_inputs import input_pass


def event_candidates(conn: sqlite3.Connection, statuses: tuple[str, ...], kinds: tuple[str, ...],
                     *, exclude_statuses: bool = False) -> bool:
    """A positive answer always runs its handler, even without local writes."""
    return bool(candidate_task_ids(conn, statuses, kinds, exclude_statuses=exclude_statuses))


def candidate_task_ids(conn: sqlite3.Connection, statuses: tuple[str, ...], kinds: tuple[str, ...],
                       *, exclude_statuses: bool = False) -> tuple[str, ...]:
    """Ids of the tasks whose status is (or, with ``exclude_statuses``, is not) in ``statuses`` and that carry an
    event of one of ``kinds``, sorted.

    [daveey] G2c: a per-task memo (``kanban_task_memo``). The answer for a task reads only its own status and its
    own events, so a write marks exactly the tasks whose answer can move; only those are re-read. The memo it
    replaces was keyed on the whole-board revision (plus a BEGIN and ``sqlite_master`` scan per call), which moved
    on nearly every hub tick. Inside a caller's transaction the answer includes its uncommitted writes and is not
    stored.
    """
    from hermes_cli import kanban_task_memo as memo

    statuses, kinds = tuple(statuses), tuple(kinds)
    operator = 'NOT IN' if exclude_statuses else 'IN'
    # Drive from the event-kind index (a handful of rows) rather than probing it once per task in the status set.
    sql = (f"SELECT t.id FROM tasks t WHERE t.status {operator} ({','.join('?' * len(statuses))}) AND t.id IN "
           f"(SELECT e.task_id FROM task_events e WHERE e.kind IN ({','.join('?' * len(kinds))}))")

    def full(c):
        return {row[0]: True for row in c.execute(sql, (*statuses, *kinds)).fetchall()}

    def partial(c, ids):
        # Driven from the ids (primary key), each probing the (kind, task_id) event index.
        return {row[0]: True for row in c.execute(
            f"SELECT t.id FROM tasks t WHERE t.id IN ({','.join('?' * len(ids))}) "
            f"AND t.status {operator} ({','.join('?' * len(statuses))}) AND EXISTS (SELECT 1 FROM task_events e "
            f"WHERE e.task_id = t.id AND e.kind IN ({','.join('?' * len(kinds))}))",
            (*ids, *statuses, *kinds)).fetchall()}

    st = memo.state(f"idle_candidates:{statuses!r}:{kinds!r}:{bool(exclude_statuses)}", conn, full=full,
                    partial=partial, columns={'tasks': ('status',), 'task_events': ('kind', 'task_id')},
                    event_kinds=kinds)
    return st.view('sorted', lambda values: tuple(sorted(values)))


@input_pass
def sweep_shared_blockers(conn, result) -> None:
    """Skip proven-empty lanes; positive candidates still obey time-based policy.

    G2b: the three lanes share one blocker-input scope (``kanban_blocker_inputs``): a lane's write, a
    peer's commit or a config alias change still invalidates every retained input. G2c: the lanes also share one
    ``blocker_states`` walk under that key (each lane gets a private copy and applies check clears and the fence
    itself); it is never kept past this pass.

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
    except Exception:  # health: allow BLE001 -- a failed check must not suppress independent maintenance lanes
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
