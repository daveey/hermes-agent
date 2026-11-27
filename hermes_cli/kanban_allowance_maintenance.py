"""Home-owned exhaustion recovery before dispatch capacity gates. [daveey]

Private Board observations never authorize execution. Exhaustion is monotonic for
one exact override; a successor override invalidates it. Cadence/cursor state is
only bounded process-local I/O pacing, never a spending counter or durable proof.
"""
from collections import OrderedDict
import time

from hermes_cli import kanban_allowance_observation as observation

BATCH = 8
CADENCE = 60.0
MAX_BOARDS = 64
_CURSORS = OrderedDict()


HELD_NOTE = ("The Board still holds this card's reviewer allowance ({state}, review {review}), so the newer budget "
             "does not let it run: only a human operator's `hermes kanban release` replaces that allowance.")


def triage(conn, task_id, observed):
    from hermes_cli.kanban_wake_check import foreign_copy
    if observed is None or observed.remaining != 0 or foreign_copy(conn, task_id):
        return False
    note = "The Board's ten fresh-lease reviewer allowance is exhausted; the owner must decide the next round."
    return _hold(conn, task_id, lambda: observation.current(conn, task_id, observed), note,
                 {**observed.expected, "remaining": 0, "board": observed.board})


def triage_stuck(conn, task_id, held):
    """Decided but stuck: the Board refuses every grant of a card whose newest budget is generic."""
    from hermes_cli.kanban_wake_check import foreign_copy
    if held is None or foreign_copy(conn, task_id):
        return False
    return _hold(conn, task_id, lambda: observation.still_stuck(conn, task_id, held),
                 HELD_NOTE.format(state=held.state, review=held.review_uid),
                 {"basis": observation.allowance.BASIS, "review_uid": held.review_uid, "state": held.state,
                  "board": held.board, "superseded": False})


def _hold(conn, task_id, valid, note, evidence):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_spend_budget as budget
    from hermes_cli import kanban_spend_decision as decision
    from hermes_cli.kanban_wake_check import foreign_copy
    from hermes_cli.kanban_home_transfer import frozen
    with kb.write_txn(conn):
        # Recheck under the writer after the Board read.
        if frozen(conn, task_id) or not valid() or foreign_copy(conn, task_id):
            return False
        if decision.pending(conn, task_id):
            return False  # preserve that episode; ordinary decision maintenance owns its notice
        now = int(time.time())
        changed = conn.execute(
            "UPDATE tasks SET status='triage',claim_lock=NULL,claim_expires=NULL,worker_pid=NULL "
            "WHERE id=? AND status IN ('ready','review') AND current_run_id IS NULL AND " + budget._UNCLAIMED,
            (task_id, now)).rowcount
        if changed != 1:
            return False
        spent = budget.spend(conn, task_id, as_of=now + 1)
        limits = budget.card_limits(conn, task_id, budget.limits())
        kb._append_event(conn, task_id, budget.EVENT, {
            "limit": "runs", "runs": spent.runs, "since": spent.since, "tokens": spent.tokens,
            "runs_with_usage": spent.reported, "cost_usd": spent.cost_usd, "override": True,
            "limits": {"runs": limits.runs, "cache_read_tokens": limits.cache_read_tokens}, "reason": note,
            "allowance": evidence})
        kb.add_comment(conn, task_id, budget.AUTHOR, budget._comment(task_id, note), system=True)
        decision.safe_ensure_producer(conn, task_id)
    return True


def sweep(conn):
    """At most eight reads and five seconds per minute per DB, outside write transactions."""
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli.kanban_wake_check import foreign_copy
    from hermes_cli.kanban_home_transfer import frozen
    if conn.in_transaction or sync.board_mode(conn) != "primary":
        return []
    state = sync.sync_state(conn)
    if not state:
        return []
    path = conn.execute("SELECT file FROM pragma_database_list WHERE name='main'").fetchone()[0]
    key = (path, state["board_id"])
    now = time.monotonic()
    after, next_at = _CURSORS.get(key, ("", 0.0))
    if now < next_at:
        return []
    _CURSORS[key] = (after, now + CADENCE)
    _CURSORS.move_to_end(key)
    while len(_CURSORS) > MAX_BOARDS:
        _CURSORS.popitem(last=False)
    rows = conn.execute(
        "SELECT t.id FROM tasks t WHERE t.status IN ('ready','review') AND t.current_run_id IS NULL "
        "AND t.id>? AND EXISTS (SELECT 1 FROM task_events e WHERE e.task_id=t.id "
        "AND e.kind='spend_budget_set' AND json_valid(e.payload) "
        "AND json_extract(e.payload,'$.runs_basis')='board_lease_allowance_v1') ORDER BY t.id LIMIT ?",
        (after, BATCH)).fetchall()
    deadline, held = now + observation.READ_SECONDS, []
    for (task_id,) in rows:
        if time.monotonic() >= deadline:
            break
        after = task_id
        if frozen(conn, task_id) or foreign_copy(conn, task_id):
            continue
        # One read per card: an exhausted exact allowance, else a generic newest budget the Board still refuses.
        exhausted = triage(conn, task_id, observation.read(conn, task_id, deadline=deadline))
        if exhausted or (not frozen(conn, task_id) and triage_stuck(
                conn, task_id, observation.stuck(conn, task_id, deadline=deadline))):
            held.append(task_id)
    # Wrap only after exhausting this bounded page, not after a deadline-shortened pass.
    if not rows or (len(rows) < BATCH and after == rows[-1][0]):
        after = ""
    _CURSORS[key] = (after, now + CADENCE)
    return held
