"""Conservative per-subpass admission for the unloaded host loop (H2/C11).

These are structural *empty proofs*, not delivery-result caches. A returned []
can mean busy, a lost claim, a swallowed error, or a future retry. Positive and
unknown inputs always run. Fanout independently compares durable cursors and
remote acknowledgements; a delivery return value is never a success receipt.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import logging
import math
import time

from hermes_cli import loop_accounting as accounting

logger = logging.getLogger(__name__)


_profile_census: ContextVar[dict | None] = ContextVar('unloaded_profile_census', default=None)


@contextmanager
def profile_census():
    """Reuse successful profile inventories only inside this host-pass scope.

    Nested passes get independent inventories; resetting the token restores the
    outer pass even when a probe/work item raises. No readers or decisions are
    retained here. Delivery work keeps its existing discovery behavior.
    """
    token = _profile_census.set({})
    try:
        yield
    finally:
        _profile_census.reset(token)


@dataclass(frozen=True)
class Inputs:
    key: tuple = ()
    pending: bool = True
    due: float = float('inf')


_last: dict[str, tuple] = {}
_killed_empty_keys: dict[object, tuple] = {}


def reset_for_tests() -> None:
    _last.clear()
    _killed_empty_keys.clear()


def _killed_empty(conn) -> bool:
    """Cache only structural emptiness, never a clock or delivery decision."""
    stamp = _revision(conn)
    transactional = conn.in_transaction
    if not transactional and _killed_empty_keys.get(conn) == stamp:
        return True
    _killed_empty_keys.pop(conn, None)
    empty = conn.execute("SELECT 1 FROM sessions WHERE parent_session_id IS NOT NULL "
                         "AND end_reason='startup_orphan_reap' LIMIT 1").fetchone() is None
    if empty and not transactional:
        # Bound retained identities when profiles/readers turn over. Connection
        # identity also prevents a replacement store inheriting an empty proof.
        if len(_killed_empty_keys) >= 32:
            _killed_empty_keys.clear()
        _killed_empty_keys[conn] = stamp
    return empty


def _revision(conn) -> tuple:
    return (conn, conn.execute('PRAGMA data_version').fetchone()[0], conn.total_changes)


def _notices() -> Inputs:
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_reader_retention import reader
    from tui_gateway.kanban_unloaded_candidates import _on_connection as subscriptions
    from tui_gateway.session_notifications import _KANBAN_NOTIFY_KINDS

    # Do not use _board_slugs' error-to-[] fallback as an empty proof.
    boards = kb.list_boards(include_archived=False)
    paths = {kb.kanban_db_path(b.get('slug') or kb.DEFAULT_BOARD).resolve() for b in boards}
    keys = []
    for path in sorted(paths):
        with reader(path) as conn:
            stamp = _revision(conn)  # capture BEFORE reading/working; never bless a later commit
            if subscriptions(conn, _KANBAN_NOTIFY_KINDS):
                return Inputs()
            keys.append((str(path), stamp))
    return Inputs(tuple(keys), pending=False)


def _profile_paths(every_store: bool = True) -> list:
    from pathlib import Path
    from hermes_constants import get_hermes_home
    from tui_gateway import server

    census = _profile_census.get()
    if census is not None and every_store in census:
        return list(census[every_store])
    paths = [Path(get_hermes_home()) / 'state.db']
    proven = not every_store
    if every_store:
        for profile in server._readopt_wake_profiles():
            if profile:
                proven = True
                home = server._profile_home(profile)
                if home is None:
                    raise ValueError('unknown profile home')
                paths.append(Path(home) / 'state.db')
    result = tuple(sorted(set(p.resolve() for p in paths)))
    if census is not None and proven:
        # The legacy discovery helper suppresses filesystem errors into [None].
        # Do not retain launch-only results without a success certificate.
        # Publish only after discovery AND all path resolution succeeded.
        census[every_store] = result
    return list(result)


def _guarantee() -> Inputs:
    from hermes_state_common import _sql_session_last_active
    from tui_gateway.kanban_notice_session_store import profile_store, reader_of
    from tui_gateway.notice_candidates import rows
    from tui_gateway import notice_guarantee as ng
    from tui_gateway.notice_fanout import _decode

    keys, due = [], float('inf')
    now = time.time()
    sql = ng._CANDIDATES_SQL.format(last_active=_sql_session_last_active('s'))
    for path in _profile_paths():
        with profile_store(path) as store:
            if store is None:
                # Missing stores cannot own notice rows; appearance changes the key.
                keys.append((str(path), None))
                continue
            db = reader_of(store)
            stamp = db._read_retrying_ioerr(_revision)
            for row in rows(db, sql):
                meta = _decode(row['display_metadata'])
                closeout = meta.get('closeout')
                # Held/live and budget-only candidates remain hot: neither an
                # active-session lease nor an hourly charge requires a DB write.
                if closeout is None or ng._legacy_busy(meta):
                    return Inputs()
                if not isinstance(closeout, dict):
                    return Inputs()
                if closeout.get('state') == 'deferred':
                    # C11: even a timed busy refusal remains a pending deferral.
                    # Admission can recover without a database revision.
                    if closeout.get('busy_attempts'):
                        return Inputs()
                    retry = closeout.get('retry_at')
                    timestamp = row['timestamp']
                    if (not isinstance(retry, (int, float)) or isinstance(retry, bool)
                            or not isinstance(timestamp, (int, float))
                            or not math.isfinite(retry) or not math.isfinite(timestamp)):
                        return Inputs()
                    deadline = max(retry, timestamp + ng.GRACE_S)
                    if deadline <= now:
                        return Inputs()
                    due = min(due, deadline)
            keys.append((str(path), stamp))
    return Inputs(tuple(keys), pending=False, due=due)


def _session_empty(kind: str) -> Inputs:
    from hermes_cli.kanban_reader_retention import reader
    from tools.async_delegation_idle import ledger_quiet

    keys = []
    for path in _profile_paths():
        # Missing delegation ledgers do not require creation; a new file changes
        # the key. Unreadable/existing-but-incompatible stores fail open.
        if not path.exists():
            keys.append((str(path), None))
            continue
        with reader(path) as conn:
            stamp = _revision(conn)
            if kind == 'delegation':
                if not ledger_quiet(path):
                    return Inputs()
            else:
                # No age predicate: positive reaped rows stay hot, including busy
                # parents and previously delivered notices. Their lineage receipt
                # is deliberately not interpreted by this admission layer.
                if not _killed_empty(conn):
                    return Inputs()
            keys.append((str(path), stamp))
    from tui_gateway.delegation_unloaded import RECOVER_EVERY_S
    return Inputs(tuple(keys), pending=False,
                  due=time.time() + RECOVER_EVERY_S if kind == 'delegation' else float('inf'))


def _remote() -> Inputs:
    from hermes_cli.kanban_origin import _hub_db, gateway_names
    from hermes_cli.kanban_reader_retention import reader
    from tui_gateway import kanban_remote_notices as remote
    from tui_gateway.kanban_replica_observer import fingerprint

    path = _hub_db()
    if path is None:
        return Inputs((None,), pending=False)
    me = gateway_names()[0]
    stamp = fingerprint(path)
    if not me or stamp is None:
        return Inputs()
    cards, _states = remote._sweep_snapshot(path, me)
    # Even already delivered terminal cards are conservatively hot. Freshness,
    # live unloads, ledger releases and _woken running flags are not DB commits.
    if cards:
        return Inputs()
    with remote._woken_lock:
        if remote._woken:
            return Inputs()
    ledger = remote.ledger_path()
    ledger_stamp = None
    if ledger.exists():
        with reader(ledger) as conn:
            ledger_stamp = _revision(conn)
            if conn.execute('SELECT 1 FROM notices WHERE delivered=0 LIMIT 1').fetchone():
                return Inputs()  # preserve holder-death and stale-claim recovery
    return Inputs((str(path), me, stamp, str(ledger), ledger_stamp), pending=False)


def probe(kind: str, *, every_store: bool = True) -> Inputs:
    """Fail open; never infer pending state from a delivery function's return."""
    try:
        if kind == 'card_change_pass':
            # No cheap, complete fanout key exists: the strict remote projection
            # depends on alias TTL, owner freshness, ledger, config and last-good
            # state, and computing it as a probe cost ~4x the pass it would skip
            # (v9 replay at the production 30 s cadence). Always run.
            return Inputs()
        if kind == 'notices':
            return _notices()
        if kind == 'guarantee_pass':
            return _guarantee()
        if kind == 'remote_notices':
            return _remote()
        if kind in ('delegation', 'killed_notices'):
            return _session_empty(kind)
    except Exception:  # health: allow BLE001 -- admission failure must retain the recovery path
        logger.debug('unloaded %s admission unavailable', kind, exc_info=True)
    return Inputs()


def run(kind: str, work, *args, live: set[str], every_store: bool = True, **kwargs):
    """Each pass owns its key; pending/busy/slots never acquire a skip stamp."""
    with accounting.pass_('unloaded.' + kind) as count:
        inputs = probe(kind, every_store=every_store) if every_store else Inputs()
        key = (frozenset(live), every_store, inputs.key)
        previous = _last.get(kind)
        if (not inputs.pending and previous is not None and previous[0] == key
                and time.time() < min(inputs.due, previous[1])):
            count.skip()
            return None
        # Forget BEFORE work. A raised operation must not leave an earlier stamp.
        _last.pop(kind, None)
        count.fallback() if inputs.pending else count.change()
        result = work(*args, **kwargs)
        # Only an independent structural proof can be retained. In particular,
        # swallowed per-row errors on positive inputs cannot stamp success.
        if not inputs.pending and time.time() < inputs.due:
            _last[kind] = (key, inputs.due)
        if isinstance(result, (list, tuple)):
            count.rows(len(result))
        return result
