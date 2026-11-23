"""Owner-only local-board capture; pure rendering shares the legacy serializer.

Capture is deliberately not a request adapter. The caller owns a short-lived
read transaction and its authorized profile context. Diagnostics may consult
external config/replicas; those are bounded-refresh evidence, not an atomic
cross-database snapshot. No shadow merging or catalog discovery is claimed here.
"""
from collections.abc import Mapping
import time

from hermes_cli import kanban_db
from plugins.kanban.dashboard import board_read


def capture(conn, *, tenant=None, include_archived=False, workflow_template_id=None,
            current_step_key=None, done_limit=80):
    """Capture actual canonical related rows, then discard all retained memo objects.

    Call on the auxiliary capture worker, never on the shared feed. Failure
    propagates for source health; it is not an authoritative empty board.
    """
    if not conn.in_transaction:
        raise ValueError('board capture requires a caller-owned read transaction')
    observed_at = time.time()
    rows = board_read.materialize(conn, tenant, include_archived, workflow_template_id,
                                  current_step_key, done_limit)
    return capture_rows(rows, include_archived=include_archived, observed_at=observed_at)


def capture_rows(rows, *, include_archived, observed_at):
    """Detach related-row materialization before publishing any owner input."""
    diagnostics = board_read.diagnostics_from_inputs(rows['diag_inputs'])
    ages = {}
    for task, _fields, _extra in rows['cards']:
        try:
            ages[task.id] = kanban_db.task_age(task)
        except Exception:  # health: allow BLE001 -- preserve canonical malformed age fallback
            ages[task.id] = {'created_age_seconds': None, 'started_age_seconds': None,
                             'time_to_complete_seconds': None}
    cards = []
    for _task, fields, extra in rows['cards']:
        fields = dict(fields)
        board_read.preview_list_body(fields)  # idempotent canonical preview before bounded input publication
        cards.append((fields, extra))
    return {
        'rows': {'cards': cards,
                 'done_total': rows['done_total'], 'tenants': rows['tenants'],
                 'latest_event_id': rows['latest_event_id']},
        'include_archived': include_archived, 'diagnostics_per_task': diagnostics,
        'ages': ages,
        'assignees': sorted(set(kanban_db.list_profiles_on_disk()) | rows['task_assignees']),
        'now': int(time.time()), 'observed_at': observed_at,
    }


def _plain(value):
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def project(captured):
    """Thaw frozen inputs once; no SQL, discovery, diagnostics or clock reads."""
    value = _plain(captured)
    return board_read.project_fields(value['rows'], value['include_archived'],
                                    diagnostics_per_task=value['diagnostics_per_task'],
                                    ages=value['ages'], assignees=value['assignees'], now=value['now'])
