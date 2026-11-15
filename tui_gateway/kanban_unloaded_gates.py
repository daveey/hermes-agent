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
import hashlib
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
    # The consumer re-offers only children reaped inside RETRY_WINDOW_S (reap_notices._REAPED_SQL). Older reaped
    # rows (73 on the hub) can never become work again: the window only moves forward, so a row leaves it and
    # enters only through a write, which moves the stamp. Without the predicate they kept the pass hot forever.
    from tui_gateway.reap_notices import RETRY_WINDOW_S
    empty = conn.execute("SELECT 1 FROM sessions WHERE parent_session_id IS NOT NULL "
                         "AND end_reason='startup_orphan_reap' AND ended_at >= ? LIMIT 1",
                         (time.time() - RETRY_WINDOW_S,)).fetchone() is None
    if empty and not transactional:
        # Bound retained identities when profiles/readers turn over. Connection
        # identity also prevents a replacement store inheriting an empty proof.
        if len(_killed_empty_keys) >= 32:
            _killed_empty_keys.clear()
        _killed_empty_keys[conn] = stamp
    return empty


def _revision(conn) -> tuple:
    return (conn, conn.execute('PRAGMA data_version').fetchone()[0], conn.total_changes)


def _notices(live: frozenset = frozenset()) -> Inputs:
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
            # The sweep skips a sub whose chat is live (``chat_id in live_ids``), and the live set is part of the
            # key, so such a sub is not pending: the chat unloading moves the key. An in-flight claim stays
            # pending (dead-holder recovery is a process-liveness decision, not structure). The board's own
            # revision is NOT in the key: a live board is rewritten by bookkeeping all the time, and the
            # emptiness of the candidate set is re-evaluated by every probe.
            orphans = []
            for sub in subscriptions(conn, _KANBAN_NOTIFY_KINDS):
                chat = str(sub.get('chat_id') or '')
                if sub.get('inflight_from') is None and chat in live:
                    continue
                if sub.get('inflight_from') is None and _orphan_chat(conn, sub, chat):
                    orphans.append((str(sub.get('task_id')), chat, sub.get('last_event_id')))
                    continue
                return Inputs()
            # An orphan is part of the key: a new one (or one whose cursor moved) runs the sweep once.
            keys.append((str(path), tuple(sorted(orphans, key=repr))))
    return Inputs(tuple(keys), pending=False)


def _orphan_chat(conn, sub: dict, chat: str) -> bool:
    """True when the sweep can do nothing for this sub until one of its inputs changes: its chat is in NO profile
    store, so the delivery path ends at ``session_db(chat) -> (None, None)`` ("no store holds this chat: leave the
    cursor alone, the chat may still appear"). A chat appearing in any store, the card being archived (that path
    drops the sub) or a placement placeholder id (dropped) is NOT an orphan: those run. One such sub whose chat was
    deleted six days ago held the whole pass hot on the hub, opening every profile store each pass (t_01a1dfa94f2251cd).
    Any read failure raises: the probe's caller treats that as unknown (``Inputs()``)."""
    from hermes_cli.kanban_placement import is_placeholder_session
    from tui_gateway.kanban_notice_session_store import profile_store, reader_of

    if not chat or is_placeholder_session(chat):
        return False
    status = conn.execute('SELECT status FROM tasks WHERE id = ?', (sub.get('task_id'),)).fetchone()
    if status is None or status[0] == 'archived':
        return False
    for path in _profile_paths():
        with profile_store(path) as store:
            if store is not None and reader_of(store)._read_one('SELECT 1 FROM sessions WHERE id = ?', (chat,)) is not None:
                return False
    return True


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
    for path in _profile_paths():
        with profile_store(path) as store:
            if store is None:
                # Missing stores cannot own notice rows; appearance changes the key.
                keys.append((str(path), None))
                continue
            db = reader_of(store)
            sql = ng.candidates_sql(db).format(last_active=_sql_session_last_active('s'))
            digest = hashlib.blake2b(digest_size=16)
            for row in rows(db, sql):
                meta = _decode(row['display_metadata'])
                closeout = meta.get('closeout')
                digest.update(repr((row['sid'], row['msg_id'], row['timestamp'], row['last_active'],
                                    row['display_metadata'])).encode())
                last = row['last_active']
                if (not (isinstance(closeout, dict) and closeout.get('state') == 'deferred')
                        and (last is None or (isinstance(last, (int, float)) and last < now - ng.LOOKBACK_S))):
                    # ``candidates()`` never offers a non-deferred row whose chat was last active before
                    # LOOKBACK_S. Time only moves it further out; a write that revives it changes
                    # ``last_active`` and so the digest. A 3-day-old notice with no close-out held the whole
                    # pass hot on the hub (73 s of CPU per 600 s window, H2 run 132120).
                    continue
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
                    # Match the consumer's code-change recovery deadline; a
                    # persisted wall backoff can shorten after a new build.
                    from tui_gateway.notice_retry import effective_due
                    retry = effective_due(closeout)
                    timestamp = row['timestamp']
                    if (not isinstance(retry, (int, float)) or isinstance(retry, bool)
                            or not isinstance(timestamp, (int, float))
                            or not math.isfinite(retry) or not math.isfinite(timestamp)):
                        return Inputs()
                    deadline = max(retry, timestamp + ng.GRACE_S)
                    if deadline <= now:
                        return Inputs()
                    due = min(due, deadline)
            # The key is the candidate rows themselves, not the store's revision: every chat message on a live
            # install commits to state.db, and none of those changes what this pass would do.
            keys.append((str(path), digest.hexdigest()))
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
            if kind == 'delegation':
                if not ledger_quiet(path):
                    return Inputs()
            else:
                # No age predicate: positive reaped rows stay hot, including busy
                # parents and previously delivered notices. Their lineage receipt
                # is deliberately not interpreted by this admission layer.
                if not _killed_empty(conn):
                    return Inputs()
            # Quiet/empty is re-proved on every probe; the store's revision is not part of the key (see _notices).
            keys.append((str(path), 'quiet'))
    from tui_gateway.delegation_unloaded import RECOVER_EVERY_S
    return Inputs(tuple(keys), pending=False,
                  due=time.time() + RECOVER_EVERY_S if kind == 'delegation' else float('inf'))


def _remote(live: frozenset = frozenset()) -> Inputs:
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
    with remote._woken_lock:
        if remote._woken:
            return Inputs()
    ledger = remote.ledger_path()
    ledger_stamp = None
    if ledger.exists():
        with reader(ledger) as conn:
            ledger_stamp = _revision(conn)
            # Holder-death and stale-claim recovery: only a claim WITH a holder is ever settled by the sweep.
            if conn.execute('SELECT 1 FROM notices WHERE delivered=0 AND holder_pid IS NOT NULL LIMIT 1').fetchone():
                return Inputs()
            if cards and not _cards_settled(conn, cards, live):
                return Inputs()
    elif cards:
        return Inputs()
    # The key is what the pass would read, not the replica's commit counter: the hub's replica commits on every
    # sync, and none of those commits changed a card this host owes a notice for (t_01a1dfa94f2251cd).
    digest = hashlib.sha256()
    for owner, key, task, links in cards:
        digest.update(repr((owner, key, remote._notice_state(task),
                            tuple((link.get('profile'), link.get('session')) for link in links))).encode())
    return Inputs((str(path), me, digest.hexdigest(), str(ledger), ledger_stamp), pending=False)


def _cards_settled(conn, cards, live) -> bool:
    """True when every notice the sweep could deliver already has a settled ledger row, so the pass has nothing to
    deliver, claim, wait on or count. Anything unproven (a link without a settled row, an open chat, whose poller
    the sweep notes) keeps the pass hot. Blank and placement-placeholder links are dropped by the delivery
    itself, with no effect."""
    from hermes_cli.kanban_placement import is_placeholder_session
    from tui_gateway import kanban_remote_notices as remote

    for owner, key, task, links in cards:
        ref, state = f'{owner}/{key}', remote._notice_state(task)
        for link in links:
            profile, sid = link.get('profile'), link.get('session')
            if not profile or not sid or is_placeholder_session(sid):
                continue
            if sid in live or not remote._ledger_settled(conn, (sid,), ref, state):
                return False
    return True


def probe(kind: str, *, every_store: bool = True, live: frozenset | set = frozenset()) -> Inputs:
    """Fail open; never infer pending state from a delivery function's return."""
    try:
        if kind == 'card_change_pass':
            # No cheap, complete key exists for the REMOTE half (alias TTL, owner freshness, ledger, config,
            # last-good state: computing it as a probe cost ~4x the pass, v9 replay), so the sub-pass always runs.
            # Its LOCAL half is skipped inside the pass by the durable ``task_events`` cursor against the
            # board's retained reader (``notice_fanout._board_idle``): no per-pass board connect, and a changed
            # outcome is picked up on the very next pass.
            return Inputs()
        if kind == 'notices':
            return _notices(frozenset(live))
        if kind == 'guarantee_pass':
            return _guarantee()
        if kind == 'remote_notices':
            return _remote(frozenset(live))
        if kind in ('delegation', 'killed_notices'):
            return _session_empty(kind)
    except Exception:  # health: allow BLE001 -- admission failure must retain the recovery path
        logger.debug('unloaded %s admission unavailable', kind, exc_info=True)
    return Inputs()


def run(kind: str, work, *args, live: set[str], every_store: bool = True, **kwargs):
    """Each pass owns its key; pending/busy/slots never acquire a skip stamp."""
    with accounting.pass_('unloaded.' + kind) as count:
        inputs = probe(kind, every_store=every_store, live=live) if every_store else Inputs()
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
