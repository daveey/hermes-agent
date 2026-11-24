"""Dispatcher-owned retirement of stored pre-model-neutral specifications.

Only task policy columns change. Execution receipts, leases and live clients are
not policy, and are deliberately never read or rewritten by this maintenance.
Ordinary UPDATEs use the same sync capture/field-HLC path as set-model and edit.
"""
from __future__ import annotations

import json

from agent.model_routing import is_class_selector
from hermes_cli import kanban_db as kb
from hermes_cli.kanban_model_neutral import _REQUIRED, retained_line, validate_text
from hermes_cli.kanban_wake_check import foreign_copy, own_gateway

FIELDS = ('model_override', 'provider_override', 'model_pin_mandatory')
REASON = 'model_policy_no_pins'
AUTHOR = 'kanban-dispatcher'
PREFIX = 'Historical run instruction (superseded and not active): '
_scan_cursor: dict[str, str] = {}


def owns_policy(conn, task_id):
    """Legacy unsynced files are local; identity-incomplete synced files fail closed."""
    if own_gateway(conn) is None and conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='kanban_sync_state'").fetchone():
        return False
    return not foreign_copy(conn, task_id)


def retire_selectors(conn, row):
    """CAS the exact stored selector triple, leaving concurrent supported edits intact."""
    old = tuple(row[field] for field in FIELDS)
    model, provider, _mandatory = old
    new = (model if model and is_class_selector(model) else None,
           provider if provider == 'auto' else None, None)
    if old == new:
        return False
    with kb.write_txn(conn):
        if not owns_policy(conn, row['id']):
            return False
        cur = conn.execute(
            "UPDATE tasks SET model_override=?,provider_override=?,model_pin_mandatory=? "
            "WHERE id=? AND model_override IS ? AND provider_override IS ? AND model_pin_mandatory IS ? "
            "AND status IN ('todo','triage','ready','scheduled','blocked','running','review')",
            (*new, row['id'], *old))
        if cur.rowcount != 1:
            return False
        kb._append_event(conn, row['id'], 'model_override_cleared',
                         {'reason': REASON, 'previous': dict(zip(FIELDS, old)), 'current': dict(zip(FIELDS, new))})
        kb.add_comment(conn, row['id'], AUTHOR,
                       'Stored model/provider restrictions and mandatory-pin state retired '
                       '(model_policy_no_pins). Class/capability routing remains active; '
                       'any already-started run and its execution history are unchanged.')
    kb.notify_task_updated(conn, row['id'], FIELDS)
    return True


def retired_body(body):
    """Quote refused lines as history and keep their other obligations active.

    A refused line is followed by itself with only the named route neutralized, so
    non-model gates sharing it (CI, review, merge rules) never become inactive. A
    nameless pure directive (``require_model: true``) is quoted alone. A line with no
    acceptable neutral form is left unchanged for an owner edit rather than
    inactivating text the recognizer cannot attribute.
    """
    out = []
    for line in (body or '').splitlines(keepends=True):
        text = line.rstrip('\r\n\v\f\x1c\x1d\x1e\x85\u2028\u2029')
        ending = line[len(text):]
        try:
            validate_text(text)
        except ValueError:
            if _REQUIRED.fullmatch(text):
                text = PREFIX + json.dumps(text, ensure_ascii=False)
            else:
                text = retained_line(text, ending or '\n') or text
        out.append(text + ending)
    return ''.join(out)


def retire_body(conn, row):
    """Use supported body editing, including its audit and post-commit observer."""
    from hermes_cli.kanban_db_edit import edit_task
    body = retired_body(row['body'])
    if body == (row['body'] or ''):
        return False
    return edit_task(conn, row['id'], body=body, expected_body=row['body'], home_open_only=True,
                     audit_comment=(AUTHOR, 'Superseded model/provider mandates were retained as JSON historical '
                                    'evidence (model_policy_no_pins); each such line stays active after it '
                                    'with only the named route made neutral, and all other text is unchanged.'))


def sweep(conn, *, limit=64):
    """Bounded rotating owner-only scan; one broken card never starves siblings."""
    from hermes_cli.kanban_db_connect import _main_db_file
    key = str(_main_db_file(conn))
    own = own_gateway(conn)
    owner_sql = ' AND (created_on IS NULL OR created_on=?)' if own else ''
    query = (
        "SELECT id,body,model_override,provider_override,model_pin_mandatory FROM tasks "
        "WHERE status IN ('todo','triage','ready','scheduled','blocked','running','review')" + owner_sql
        + ' AND id {} ? ORDER BY id LIMIT ?')
    size = max(0, min(limit, 256))
    cursor = _scan_cursor.get(key, '')
    params = (own,) if own else ()
    rows = conn.execute(query.format('>'), (*params, cursor, size)).fetchall()
    if len(rows) < size:
        rows += conn.execute(query.format('<='), (*params, cursor, size - len(rows))).fetchall()
    changed = 0
    for row in rows:
        _scan_cursor[key] = row['id']
        repaired = False
        for repair in (retire_selectors, retire_body):
            try:
                repaired = bool(repair(conn, row)) or repaired
            except Exception:  # health: allow BLE001 -- per-card maintenance fault containment; retry next rotation
                kb._log.exception('kanban: model policy retirement failed for %s (%s)', row['id'], repair.__name__)
        changed += repaired
    if len(_scan_cursor) > 1024:
        del _scan_cursor[next(iter(_scan_cursor))]
    return changed
