"""Persistent unsynced stop-evidence fences; never a release or sync writer.

SQL-only triggers survive reopened local connections. Synced homes use canonical
Board ingress guards and authenticated replica reconciliation instead.
"""
from __future__ import annotations


def install(conn):
    protected = "SELECT task_id FROM task_events WHERE kind='decision_protected_stopped'"
    evidence = ("EXISTS (SELECT 1 FROM task_events ps WHERE ps.kind='decision_protected_stopped' "
                "AND ps.task_id=OLD.task_id AND (OLD.kind='decision_protected_stopped' "
                "OR OLD.uid=json_extract(ps.payload,'$.source_event_uid') "
                "OR OLD.uid=json_extract(ps.payload,'$.episode_uid') "
                "OR (OLD.kind='decision_hold_restricted' AND json_extract(OLD.payload,'$.source_event_uid')="
                "json_extract(ps.payload,'$.source_event_uid')) "
                "OR OLD.id=json_extract(ps.payload,'$.episode_row_id') "
                "OR OLD.id=json_extract(ps.payload,'$.restriction_row_id')))")
    # Lazy uid bootstrap on unsynced boards assigns NULL -> uid only; any other change stays fenced.
    columns = {r[1] for r in conn.execute('PRAGMA table_info(task_events)')}
    origin_same = ' AND NEW.origin_gateway IS OLD.origin_gateway' if 'origin_gateway' in columns else ''
    uid_only = ("(OLD.uid IS NULL AND NEW.uid IS NOT NULL AND NEW.id IS OLD.id "
                "AND NEW.task_id IS OLD.task_id AND NEW.kind IS OLD.kind "
                "AND NEW.payload IS OLD.payload AND NEW.run_id IS OLD.run_id AND NEW.created_at IS OLD.created_at"
                + origin_same + ")")
    runs = ("EXISTS (SELECT 1 FROM task_events ps WHERE ps.kind='decision_protected_stopped' "
            "AND ps.task_id=OLD.task_id AND OLD.uid=json_extract(ps.payload,'$.source_run_uid'))")
    links = f"OLD.parent_id IN ({protected}) OR OLD.child_id IN ({protected})"
    changes = {
        # REPLACE does not fire DELETE triggers with SQLite's default recursive_triggers=OFF.
        'event_insert': ('BEFORE INSERT ON task_events',
                         'EXISTS (SELECT 1 FROM task_events AS OLD WHERE '
                         f'(OLD.id=NEW.id OR OLD.uid=NEW.uid) AND ({evidence}))'),
        'event_update': ('BEFORE UPDATE ON task_events', f'({evidence}) AND NOT {uid_only}'),
        'event_delete': ('BEFORE DELETE ON task_events', evidence),
        'run_update': ('BEFORE UPDATE ON task_runs', runs),
        'run_delete': ('BEFORE DELETE ON task_runs', runs),
        'link_update': ('BEFORE UPDATE ON task_links', links),
        'link_delete': ('BEFORE DELETE ON task_links', links),
        'task_delete': ('BEFORE DELETE ON tasks', f'OLD.id IN ({protected})'),
        'task_update': ('BEFORE UPDATE ON tasks', f'OLD.id IN ({protected}) AND ('
                        "NEW.status NOT IN ('triage','archived') OR NEW.current_run_id IS NOT NULL "
                        'OR NEW.claim_lock IS NOT NULL OR NEW.claim_expires IS NOT NULL '
                        'OR NEW.title IS NOT OLD.title OR NEW.body IS NOT OLD.body '
                        'OR NEW.assignee IS NOT OLD.assignee)'),
    }
    for name, (when, predicate) in changes.items():
        trigger = f'protect_stopped_{name}'
        ddl = (f"CREATE TRIGGER {trigger} {when} WHEN {predicate} "
               "BEGIN SELECT RAISE(ABORT,'protect_evidence_immutable'); END")
        old = conn.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (trigger,)).fetchone()
        if old and old[0].replace('IF NOT EXISTS ', '') == ddl:
            continue
        # Runs inside the caller's write transaction; no connection sees an unfenced gap.
        conn.execute(f'DROP TRIGGER IF EXISTS {trigger}')
        conn.execute(ddl)
