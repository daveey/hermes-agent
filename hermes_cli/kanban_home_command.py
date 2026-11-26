"""Capture-time sidecars for immutable typed intent; never enrich during send. [daveey]

Semantic request JSON and its attested hash stay unchanged. The separate replay
fence is captured once in the same writer transaction, and NULL legacy custody
stays unknown forever. Board ingress fences the full wire before semantic work.
"""
from __future__ import annotations

import json

from hermes_cli import kanban_home_generation as epochs
from hermes_cli.sqlite_util import add_column_if_missing

class Pending(ValueError):
    """Temporary custody refusal before submission; not a failed transmission."""


_TABLES = frozenset({'kanban_failure_commands', 'kanban_actionability_commands',
                    'kanban_protect_commands', 'containment_delivery', 'kanban_home_delivery1'})


def install(conn, table: str) -> None:
    if table not in _TABLES:
        raise ValueError('unsupported home command custody')
    add_column_if_missing(conn, table, 'home_generations', 'home_generations TEXT')
    conn.execute(f'CREATE TRIGGER IF NOT EXISTS home_epoch1_{table} '
                 f'BEFORE UPDATE OF home_generations ON {table} '
                 'WHEN NEW.home_generations IS NOT OLD.home_generations '
                 "BEGIN SELECT RAISE(ABORT,'captured command home generation is immutable'); END")
    # REPLACE may delete a conflicting row without firing its DELETE trigger.
    # Include all unique identities, not just the primary request identifier.
    identities = []
    for index in conn.execute(f'PRAGMA index_list({table})'):
        if not index[2]:
            continue
        quoted = '"' + index[1].replace('"', '""') + '"'
        columns = [row[2] for row in conn.execute(f'PRAGMA index_info({quoted})')]
        if not columns or any(column is None for column in columns) or index[4]:
            raise ValueError('unsupported command epoch identity')
        identities.append(' AND '.join(f'"{col}"=NEW."{col}"' for col in columns))
    if not identities:
        raise ValueError('command epoch identity missing')
    conflict = ' OR '.join(f'({identity})' for identity in identities)
    conn.execute(f'CREATE TRIGGER IF NOT EXISTS home_epoch1_{table}_insert '
                 f'BEFORE INSERT ON {table} WHEN EXISTS(SELECT 1 FROM {table} '
                 f'WHERE ({conflict}) AND home_generations IS NOT NEW.home_generations) '
                 "BEGIN SELECT RAISE(ABORT,'captured command home generation is immutable'); END")


def touched(wire: dict) -> set[str]:
    ids = {wire.get(k) for k in ('task', 'parent', 'child', 'actor_task')}
    payload = wire.get('payload')
    if isinstance(payload, dict):
        if wire.get('op') == 'actionability_record' and wire.get('phase') == 'escalation':
            owner = payload.get('owner', {})
            if owner.get('kind') == 'task':
                ids.update(owner.get('refs', []))
        if wire.get('op') == 'renew_scope' and payload.get('authority_kind') == 'creator_task':
            ids.add(payload.get('authority_ref'))
    return {v for v in ids if isinstance(v, str) and v}


def capture(conn, wire: dict) -> str | None:
    if not conn.in_transaction:
        raise ValueError('home command capture requires writer transaction')
    from hermes_cli.kanban_home_transfer import frozen
    tasks = touched(wire)
    if any(frozen(conn, task) for task in tasks):
        raise Pending('home transfer pending')
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='kanban_home_generations1'").fetchone():
        return None
    values = {}
    for task in sorted(tasks):
        row = conn.execute('SELECT nonce FROM kanban_home_generations1 WHERE task_id=?', (task,)).fetchone()
        if row:
            values[task] = row[0]
    return json.dumps(values, sort_keys=True, separators=(',', ':')) if values else None


def from_evidence(task: str, row: dict) -> str | None:
    """Bind a NEW nonjournaled foreign appendage to its authenticated snapshot.

    The caller must obtain this row from the real Hub before minting the op.
    Existing/replayed commands must never use this to refresh their epoch.
    """
    if not isinstance(row, dict) or row.get('id') != task:
        raise ValueError('home evidence identity mismatch')
    if row.get('home_transfer_pending') is not None:
        raise Pending('home transfer pending')
    nonce = row.get('home_generation')
    if nonce is None:
        return None
    if not isinstance(nonce, str) or not nonce:
        raise ValueError('home evidence epoch invalid')
    return json.dumps({task: nonce}, sort_keys=True, separators=(',', ':'))


def outbound(owned: dict, semantic: dict) -> dict:
    """Read only the durable sidecar; no lookup of today's task/epoch is allowed."""
    if owned.get('capture_pending'):
        raise Pending('home capture still pending; decision is not a transport request')
    return epochs.add_captured(owned, semantic)
