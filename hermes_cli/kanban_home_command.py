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
    identity_columns = set()
    for index in conn.execute(f'PRAGMA index_list({table})'):
        if not index[2]:
            continue
        quoted = '"' + index[1].replace('"', '""') + '"'
        columns = [row[2] for row in conn.execute(f'PRAGMA index_info({quoted})')]
        if not columns or any(column is None for column in columns) or index[4]:
            raise ValueError('unsupported command epoch identity')
        identity_columns.update(columns)
        identities.append(' AND '.join(f'"{col}"=NEW."{col}"' for col in columns))
    if not identities:
        raise ValueError('command epoch identity missing')
    # No command writer rekeys a captured request. Refuse identity changes before
    # UPDATE OR REPLACE can implicitly delete a different epoch-bearing row.
    changed = ' OR '.join(f'NEW."{col}" IS NOT OLD."{col}"' for col in sorted(identity_columns))
    identity_name = f'home_epoch1_{table}_identity'
    identity_sql = (f'CREATE TRIGGER {identity_name} BEFORE UPDATE ON {table} WHEN {changed} '
                    "BEGIN SELECT RAISE(ABORT,'captured command identity is immutable'); END")
    stored = conn.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (identity_name,)).fetchone()
    if not stored or stored[0] != identity_sql:
        conn.execute(f'DROP TRIGGER IF EXISTS {identity_name}')
        conn.execute(identity_sql)
    conflict = ' OR '.join(f'({identity})' for identity in identities)
    # SQLite hides the conflict mode from triggers. INSERT OR IGNORE must stay a
    # no-op, while INSERT OR REPLACE must not swap an epoch. So BEFORE INSERT
    # only remembers the epochs of rows this insert would conflict with; an
    # IGNOREd insert never reaches AFTER INSERT, whereas a REPLACE does, and
    # then refuses if any deleted row carried a different epoch. A stale memo
    # is harmless: the next BEFORE INSERT for the table clears it first.
    conn.execute('CREATE TABLE IF NOT EXISTS kanban_home_epoch_memo1 '
                 '(tbl TEXT NOT NULL, epoch TEXT)')
    name = f'home_epoch1_{table}_insert'
    old = conn.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (name,)).fetchone()
    if old and 'kanban_home_epoch_memo1' not in (old[0] or ''):
        # Upgrade/reconnect: the first design aborted even INSERT OR IGNORE.
        conn.execute(f'DROP TRIGGER {name}')
    conn.execute(f'CREATE TRIGGER IF NOT EXISTS {name} BEFORE INSERT ON {table} '
                 f"BEGIN DELETE FROM kanban_home_epoch_memo1 WHERE tbl='{table}'; "
                 f"INSERT INTO kanban_home_epoch_memo1(tbl,epoch) SELECT '{table}',home_generations "
                 f'FROM {table} WHERE {conflict}; END')
    conn.execute(f'CREATE TRIGGER IF NOT EXISTS home_epoch1_{table}_after AFTER INSERT ON {table} '
                 f"BEGIN SELECT CASE WHEN EXISTS(SELECT 1 FROM kanban_home_epoch_memo1 WHERE tbl='{table}' "
                 "AND epoch IS NOT NEW.home_generations) "
                 "THEN RAISE(ABORT,'captured command home generation is immutable') END; "
                 f"DELETE FROM kanban_home_epoch_memo1 WHERE tbl='{table}'; END")


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
