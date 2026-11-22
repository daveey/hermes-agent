"""Primary neutral releases and durable post-feed effect receipts. [daveey]"""
import json

from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_failure_release as release


def reason_for(dead):
    """Use final annotations, not the initial exit-code classification."""
    if dead.host_reboot:
        return None if dead.reboot_culprit else "reboot_bystander"
    if dead.owner_down:
        return "owner_down"
    if dead.owner_unavailable:
        return "owner_unavailable"
    if dead.owner_restart:
        return "owner_restart"
    return "rate_limited" if dead.rate_limited else None


def queue(conn, row, dead):
    reason = reason_for(dead)
    if sync.board_mode(conn) != "primary" or reason is None:
        return False
    release.queue(conn, row["id"], dead.run_id, outcome=reason,
                  error=dead.error_text, extra=dict(dead.event_payload))
    if not release.queued(conn, dead.run_id):
        return True  # Another command owns custody; never attach neutral side effects to retain.
    conn.execute("CREATE TABLE IF NOT EXISTS kanban_neutral_effects ("
                 "run_uid TEXT PRIMARY KEY REFERENCES kanban_failure_commands(run_uid) ON DELETE CASCADE, "
                 "event_kind TEXT NOT NULL, hook TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0)")
    hook = {"task_id": row["id"], "assignee": row["assignee"], "run_id": dead.run_id,
            "worker_pid": row["worker_pid"], "exit_kind": dead.kind, "exit_code": dead.code,
            "outcome": dead.run_outcome, "retry_status": dead.event_payload["retry_status"]}
    conn.execute("INSERT OR IGNORE INTO kanban_neutral_effects (run_uid,event_kind,hook) VALUES (?,?,?)",
                 (sync.run_uid(conn, dead.run_id), dead.event_kind, json.dumps(hook)))
    return True


def pending(conn):
    """Read-only idle gate: avoid write transactions when there are no effects."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='kanban_neutral_effects'").fetchone():
        return False
    return conn.execute("SELECT 1 FROM kanban_neutral_effects e JOIN kanban_failure_commands c USING(run_uid) "
                        f"WHERE e.done=0 AND {release.release_predicate(conn)} LIMIT 1").fetchone() is not None


def drain(conn):
    """Return newly durable best-effort observer payloads, never on an ACK alone.

    The marker commits before observer calls (as with ordinary reaping hooks).
    An observer is best-effort, not an exactly-once external delivery contract.
    """
    from hermes_cli import kanban_db as kb
    if not pending(conn):
        return []
    hooks, quotas = [], []
    with kb.write_txn(conn, allow_nested=True):
        for row in conn.execute("SELECT e.*, c.task_id, r.id AS run_id, r.outcome FROM kanban_neutral_effects e "
                                "JOIN kanban_failure_commands c USING(run_uid) "
                                "JOIN task_runs r ON r.uid=e.run_uid AND r.task_id=c.task_id "
                                f"WHERE e.done=0 AND r.ended_at IS NOT NULL AND {release.release_predicate(conn)}").fetchall():
            hook = json.loads(row["hook"])
            expired = conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND run_id=? AND kind='reclaimed'",
                                   (row["task_id"], row["run_id"])).fetchone()
            if row["outcome"] != hook["outcome"] or expired:
                # Restart and alarm both end as reclaimed; the alarm event disambiguates.
                # Board expiry won the fence: never replay the losing neutral observation.
                conn.execute("UPDATE kanban_neutral_effects SET done=1 WHERE run_uid=?", (row["run_uid"],))
                continue
            event = conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND run_id=? AND kind=?",
                                 (row["task_id"], row["run_id"], row["event_kind"])).fetchone()
            if event is None:
                continue
            hook["run_id"] = row["run_id"]
            if hook["outcome"] == "rate_limited":
                from hermes_cli.kanban_quota_release import prepare
                quotas.append((row['run_uid'], hook, prepare(conn, row)))
                continue
            hooks.append(hook)
            conn.execute("UPDATE kanban_neutral_effects SET done=1 WHERE run_uid=?", (row["run_uid"],))
    for uid, hook, state in quotas:
        from hermes_cli.kanban_quota_release import finish
        try:
            finish(state)
        except OSError:
            kb._log.warning('kanban: quota wall write deferred for %s', hook['task_id'], exc_info=True)
            continue
        with kb.write_txn(conn):
            conn.execute("UPDATE kanban_neutral_effects SET done=1 WHERE run_uid=?", (uid,))
        hooks.append(hook)
    return hooks
