"""Explicit recovery of qualified historical terminal carry rows. [daveey]

An offline owner snapshot is completion evidence, never owner liveness. Only an
operator-registered, exact identity may use it; NULL profiles are not aliases.
The normal chat-link reconciler publishes the resulting release. No task, link,
origin binding or completion event is written by this module.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path

from hermes_cli.kanban_origin_ledger import observe_owner_task, _verified_terminal

_FIELDS = ('id', 'created_on', 'status', 'completed_at', 'current_run_id',
           'goal_mode', 'completion_contract', 'title', 'body', 'created_at')
_IDENTITY = ('gateway', 'profile', 'board', 'id', 'origin_gateway',
             'origin_profile', 'session', 'via', 'key', 'ownership_state')


def _identity(card: dict) -> dict:
    if not isinstance(card, dict):
        raise ValueError('terminal_carry_unqualified')
    identity = {key: card.get(key) for key in _IDENTITY}
    if (any(not isinstance(value, str) or not value for value in identity.values())
            or identity['via'] != 'shepherd' or identity['ownership_state'] != 'qualified'
            or identity['key'] != '/'.join(identity[k] for k in ('gateway', 'profile', 'board', 'id'))):
        raise ValueError('terminal_carry_unqualified')
    if any(not re.fullmatch(r'[A-Za-z0-9_-][A-Za-z0-9_.-]{0,127}', identity[k])
           for k in _IDENTITY if k != 'key'):
        raise ValueError('terminal_carry_invalid_identity')
    return identity


def _ro(path: Path, *, immutable: bool = False):
    conn = sqlite3.connect(path.resolve().as_uri() + ('?mode=ro&immutable=1' if immutable else '?mode=ro'),
                           uri=True, timeout=0.5)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA query_only=ON')
    conn.execute('BEGIN')
    return conn


def _owner_proof(path: Path, expected_hash: str, identity: dict) -> dict:
    # Preserved evidence must be self-contained, not a live WAL-backed database.
    wal = Path(str(path) + '-wal')
    if wal.exists() and wal.stat().st_size:
        raise ValueError('terminal_carry_live_database')
    with path.open('rb') as source:
        if hashlib.file_digest(source, 'sha256').hexdigest() != expected_hash:
            raise ValueError('terminal_carry_snapshot_changed')
    conn = _ro(path, immutable=True)
    try:
        row = conn.execute('SELECT * FROM tasks WHERE id=?', (identity['id'],)).fetchone()
        if row is None or row['created_on'] != identity['gateway']:
            raise ValueError('terminal_carry_wrong_owner')
        # A qualified owner subscription proves the origin, not a NULL-profile sibling.
        destination = '/'.join(identity[k] for k in ('origin_gateway', 'origin_profile', 'session'))
        subscriptions = conn.execute(
            "SELECT chat_id,delivery_mode,delivery_metadata FROM kanban_notify_subs "
            "WHERE task_id=? AND platform='hub'", (identity['id'],)).fetchall()
        origins = set()
        for subscription in subscriptions:
            metadata = json.loads(subscription['delivery_metadata'] or '{}')
            if not isinstance(metadata, dict):
                raise ValueError('terminal_carry_subscription_invalid')
            if subscription['delivery_mode'] == 'notify' and metadata.get('reason') == 'create':
                origins.add(subscription['chat_id'])
        if origins != {destination}:
            raise ValueError('terminal_carry_origin_unproven')
        from hermes_cli.kanban_chat_links import link_uid
        uid = link_uid(identity['id'], identity['origin_gateway'], identity['origin_profile'],
                       identity['session'], 'shepherd')
        links = conn.execute('SELECT * FROM task_chat_links WHERE uid=?', (uid,)).fetchall()
        if (len(links) != 1 or links[0]['task_id'] != identity['id']
                or links[0]['chat_gateway'] != identity['origin_gateway']
                or links[0]['chat_profile'] != identity['origin_profile']
                or links[0]['chat_session'] != identity['session'] or links[0]['kind'] != 'shepherd'):
            raise ValueError('terminal_carry_link_unproven')
        has_intents = conn.execute("SELECT 1 FROM sqlite_master WHERE name='origin_creation_intents'").fetchone()
        bindings = [json.loads(r[0]) for r in conn.execute(
            'SELECT binding_json FROM origin_creation_intents WHERE task_id=?', (identity['id'],))] if has_intents else []
        payload = {k: identity[k] for k in ('id', 'gateway', 'profile', 'board')}
        observed = observe_owner_task(conn, {**payload, 'status': row['status'], 'origin_bindings': bindings}, now=0)
        # This explicit recovery path requires task-bound goal proof even on
        # boards where ordinary legacy projection predates judge receipts.
        if (not _verified_terminal(observed)
                or (row['goal_mode'] and observed['origin_observation'].get('basis') == 'legacy_completed')):
            raise ValueError('terminal_carry_completion_unverified')
        run = conn.execute('SELECT profile FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1',
                           (identity['id'],)).fetchone()
        if run is None or run[0] != identity['profile']:
            raise ValueError('terminal_carry_owner_profile_unproven')
        event = conn.execute("SELECT uid FROM task_events WHERE task_id=? AND kind='completed' ORDER BY id DESC LIMIT 1",
                             (identity['id'],)).fetchone()
        if event is None or not event[0]:
            raise ValueError('terminal_carry_completion_uid_missing')
        return {'task': {k: row[k] for k in _FIELDS}, 'completion_uid': event[0], 'observation': observed}
    finally:
        conn.close()


def _current_matches(conn, identity: dict, proof: dict) -> bool:
    row = conn.execute('SELECT * FROM tasks WHERE id=?', (identity['id'],)).fetchone()
    if row is None or {k: row[k] for k in _FIELDS} != proof['task']:
        return False
    # Numeric event IDs are local to a database. Compare portable completion UIDs.
    events = conn.execute("SELECT uid FROM task_events WHERE task_id=? AND kind='completed' "
                          "ORDER BY created_at DESC,id DESC LIMIT 1", (identity['id'],)).fetchall()
    return len(events) == 1 and events[0][0] == proof['completion_uid']


def register(path: Path, *, owner_db: Path, owner_sha256: str, card: dict,
             replica: Path, state_db: Path, gateway: str, profile: str) -> dict:
    """Register an offline proof after exact local-origin and current-board checks.

    ``path`` is a receipt under this origin home's kanban/terminal-carry directory.
    The caller supplies the preserved owner's independently checked SHA256. This
    operation never makes the snapshot a live collector or changes a task status.
    """
    identity = _identity(card)
    if identity['origin_gateway'] != gateway or identity['origin_profile'] != profile:
        raise ValueError('terminal_carry_wrong_origin')
    from hermes_cli.kanban_db_notify import is_headless_session
    state = _ro(state_db)
    try:
        row = state.execute('SELECT source FROM sessions WHERE id=?', (identity['session'],)).fetchone()
        if row is None or row[0] not in ('desktop', 'cli', 'tui') or is_headless_session(identity['session']):
            raise ValueError('terminal_carry_not_human_chat')
    finally:
        state.close()
    proof = _owner_proof(owner_db, owner_sha256, identity)
    current = _ro(replica)
    try:
        if not _current_matches(current, identity, proof):
            raise ValueError('terminal_carry_generation_changed')
    finally:
        current.close()
    receipt = {'schema': 1, 'identity': identity, 'owner_db': str(owner_db.resolve()),
               'owner_sha256': owner_sha256, 'proof': proof}
    # Use the standard atomic file writer, never edit a production SQLite store.
    from utils import atomic_write_text
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(receipt, sort_keys=True) + '\n')
    return receipt


def reconcile_carry(cards: list[dict]) -> list[dict]:
    """Annotate only retained rows; fresh owner inventory always takes precedence.

    Keep the historical row rather than deleting it, so missing proof or a newer
    Board generation restores its unknown obligation on the next projection.
    """
    from hermes_constants import get_hermes_home
    from hermes_cli import kanban_sync_client as sync
    from hermes_cli.profiles import get_active_profile_name
    from hermes_cli.kanban_origin_adapter import _gateway
    from hermes_cli.kanban_chat_links import _stale_after_s
    import time

    home = get_hermes_home()
    directory = home / 'kanban' / 'terminal-carry'
    cards = [({**card, 'status': 'unknown', 'unfinished': True, 'released': False,
               'terminal_carry_verified': False} if card.get('terminal_carry_verified') else card)
             for card in cards]
    if not directory.is_dir():
        return cards
    profile = get_active_profile_name()
    try:
        gateway = _gateway(home / 'kanban' / 'absent-replica.db')
        paths = list(directory.glob('*.json'))
    except (OSError, ValueError):
        return cards
    receipts = {}
    for path in paths:
        try:
            receipt = json.loads(path.read_text())
            if not isinstance(receipt, dict):
                continue
            identity = _identity(receipt['identity'])
            if receipt.get('schema') != 1 or identity['origin_gateway'] != gateway or identity['origin_profile'] != profile:
                continue
            key = tuple(identity[k] for k in _IDENTITY)
            # Duplicate registrations are ambiguous rather than last-file-wins.
            receipts[key] = None if key in receipts else receipt
        except (OSError, ValueError, KeyError, TypeError):
            continue
    result = []
    for card in cards:
        restored = dict(card)
        if restored.pop('terminal_carry_verified', False):
            restored.update(status='unknown', unfinished=True, released=False)
        try:
            identity = _identity(restored)
            receipt = receipts.get(tuple(identity[k] for k in _IDENTITY))
            if receipt is not None and restored.get('stale') is True:
                proof = _owner_proof(Path(receipt['owner_db']), receipt['owner_sha256'], identity)
                if proof != receipt['proof']:
                    raise ValueError('terminal_carry_proof_changed')
                conn = _ro(sync.shadow_path(identity['board']))
                try:
                    pulled = conn.execute('SELECT pulled_at FROM kanban_sync_state LIMIT 1').fetchone()
                    fresh = pulled and type(pulled[0]) in (int, float) and 0 <= time.time() - pulled[0] <= _stale_after_s()
                    if fresh and _current_matches(conn, identity, proof):
                        restored.update(status=proof['task']['status'], unfinished=False, released=True,
                                        terminal_carry_verified=True)
                finally:
                    conn.close()
        except (OSError, sqlite3.Error, ValueError, KeyError, TypeError):
            pass  # Missing/corrupt evidence preserves the explicit unknown obligation.
        result.append(restored)
    return result
