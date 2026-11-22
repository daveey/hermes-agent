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
                "json_extract(ps.payload,'$.source_event_uid'))))")
    runs = ("EXISTS (SELECT 1 FROM task_events ps WHERE ps.kind='decision_protected_stopped' "
            "AND ps.task_id=OLD.task_id AND OLD.uid=json_extract(ps.payload,'$.source_run_uid'))")
    links = f"OLD.parent_id IN ({protected}) OR OLD.child_id IN ({protected})"
    changes = {
        'event_update': ('BEFORE UPDATE ON task_events', evidence),
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
        conn.execute(f"CREATE TRIGGER IF NOT EXISTS protect_stopped_{name} {when} WHEN {predicate} "
                     "BEGIN SELECT RAISE(ABORT,'protect_evidence_immutable'); END")
