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
from pathlib import Path
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
    _replica_watch.clear()
    _remote_memo.clear()
    _row_prints.clear()


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
                if sub.get('inflight_from') is not None and _claim_held(conn, sub, _KANBAN_NOTIFY_KINDS):
                    # A live holder's fresh claim with nothing deliverable past its cursor: the sweep does nothing
                    # for it until the holder dies, the claim ages out or an event arrives, and every probe
                    # re-evaluates all three. Three of these held the hub's pass hot every 30 s (t_01a1dfa94f2251cd).
                    orphans.append((str(sub.get('task_id')), chat, 'held', sub.get('inflight_from'),
                                    sub.get('last_event_id'), sub.get('inflight_pid'), sub.get('inflight_start'),
                                    sub.get('inflight_at')))
                    continue
                if sub.get('inflight_from') is None and chat in live:
                    continue
                if sub.get('inflight_from') is None and _orphan_chat(conn, sub, chat):
                    orphans.append((str(sub.get('task_id')), chat, sub.get('last_event_id')))
                    continue
                return Inputs()
            # An orphan is part of the key: a new one (or one whose cursor moved) runs the sweep once.
            keys.append((str(path), tuple(sorted(orphans, key=repr))))
    return Inputs(tuple(keys), pending=False)


def _claim_held(conn, sub: dict, kinds) -> bool:
    """True when this in-flight claim needs nothing from the sweep right now: its holder is alive, the claim is
    younger than ``NOTIFY_CLAIM_STALE_S`` and still holds a deliverable event (``recover_orphaned_notify_claims``
    would leave it alone: ``_recovery_due`` is None), and no event of ``kinds`` lies past its cursor (so
    ``unseen_events_for_sub`` is empty and ``_sweep_sub`` returns before delivering). Any doubt is False (hot)."""
    from hermes_cli import kanban_db_notify as kbn

    key = kbn._sub_key(sub['task_id'], sub['platform'], sub['chat_id'], sub.get('thread_id'))
    if kbn._recovery_due(conn, sub, key) is not None:
        return False
    marks = ','.join('?' * len(kinds)) if kinds else ''
    return conn.execute('SELECT 1 FROM task_events WHERE task_id = ? AND id > ?'
                        + (f' AND kind IN ({marks})' if marks else '') + ' LIMIT 1',
                        (sub['task_id'], int(sub['last_event_id']), *(kinds or ()))).fetchone() is None


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
                last = row['last_active']
                # ``candidates()`` reads last_active only through the lookback test below, so the key carries that
                # verdict, not the raw time: a chat that is talking commits it every few seconds, and the raw value
                # re-armed the pass every 30 s (t_01a1dfa94f2251cd). The verdict is re-evaluated by every probe.
                recent = isinstance(last, (int, float)) and last >= now - ng.LOOKBACK_S
                digest.update(repr((row['sid'], row['msg_id'], row['timestamp'], recent,
                                    row['display_metadata'])).encode())
                if (not (isinstance(closeout, dict) and closeout.get('state') == 'deferred')
                        and (last is None or (isinstance(last, (int, float)) and last < now - ng.LOOKBACK_S))):
                    # ``candidates()`` never offers a non-deferred row whose chat was last active before
                    # LOOKBACK_S. Time only moves it further out; a write that revives it changes
                    # ``last_active`` and so the digest. A 3-day-old notice with no close-out held the whole
                    # pass hot on the hub (73 s of CPU per 600 s window, H2 run 132120).
                    continue
                # A notice still inside GRACE_S is not offered by ``candidates()`` (its poller has time): not work
                # until ``timestamp + GRACE_S``, which is a clock, not a write. The hub's constant stream of fresh
                # done-notices kept this pass hot for five of every six minutes (t_01a1dfa94f2251cd).
                stamped = row['timestamp']
                if (closeout is None and isinstance(stamped, (int, float)) and not isinstance(stamped, bool)
                        and math.isfinite(stamped) and stamped > now - ng.GRACE_S):
                    due = min(due, stamped + ng.GRACE_S)
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


# replica path -> (signature, token, swept refs): the last replica task rows this host examined for changes that
# ``observe``/the sweep can act on. ``token`` moves only on such a change (see ``_replica_token``).
_replica_watch: dict = {}
# The non-pending ``_remote`` answer with the exact inputs it was computed from: an unchanged pass reads none of the
# replica's cards again.
_remote_memo: dict = {}


# The notice-relevant print of every terminal/withdrawn/swept replica task row this host last examined, keyed
# ``(replica path, gw, key)``. A terminal row is rewritten all the time on the hub (owner-side bookkeeping: a lease, a
# heartbeat's mirror, a re-sync of an unchanged card); only a CHANGED print can change what ``observe`` or the sweep do.
_row_prints: dict = {}
_ROW_PRINTS_MAX = 20000


def _drop_row_prints(path: str) -> None:
    for ident in [i for i in _row_prints if i[0] == path]:
        del _row_prints[ident]


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
    # Commit counters are deliberately excluded, but replacement, observer recovery and same-inode restore
    # signals must still retire a semantic proof. A normal checkpoint may conservatively rearm it too.
    identity = (getattr(stamp, 'ident', None), getattr(stamp, 'generation', None),
                getattr(stamp, 'signature', None))
    if _remote_memo.get('identity') != identity:
        _replica_watch.pop(str(path), None)
        _drop_row_prints(str(path))
        _remote_memo.pop('inputs', None)
        _remote_memo.pop('cards', None)
        _remote_memo['identity'] = identity
    now = time.time()
    if now < _remote_memo.get('clock', now):
        _remote_memo.pop('inputs', None)
        _remote_memo.pop('cards', None)
        _remote_memo['clock_epoch'] = _remote_memo.get('clock_epoch', 0) + 1
    _remote_memo['clock'] = now
    epoch = (identity, _remote_memo.get('clock_epoch', 0))
    with remote._woken_lock:
        if remote._woken:
            return Inputs()
    ledger = remote.ledger_path()
    ledger_stamp = None
    held: set = set()
    if ledger.exists():
        with reader(ledger) as conn:
            ledger_stamp = _revision(conn)
            # Holder-death and stale-claim recovery: only a claim WITH a holder is ever settled by the sweep. A
            # claim whose holder is ANOTHER live process is left alone by every path of the sweep (it reads "skip"
            # until that holder acks or dies), and liveness is re-read by every probe; this process's own claim, a
            # dead holder and an unreadable row are work. One live foreign claim held the hub's pass hot every 30 s
            # (t_01a1dfa94f2251cd).
            for row in conn.execute('SELECT session, ref, state, holder_pid, holder_start FROM notices '
                                    'WHERE delivered=0 AND holder_pid IS NOT NULL').fetchall():
                if remote._holder_is_me(row[3]) or not remote._holder_alive(row[3], row[4]):
                    return Inputs()
                held.add(tuple(row))
    # The key is what the pass would read, not the replica's commit counter: the hub's replica commits several
    # task-row writes a second, and almost none touches a card this host owes a notice for (t_01a1dfa94f2251cd).
    # ``observe`` records this host's first sighting of EVERY terminal state, linked or not, and a later link must
    # not resurrect a notice that expired unseen: so a task row that is terminal, withdrawn or swept is never
    # skipped. Unreadable = unknown = hot.
    token = _replica_token(path, me)
    if token is None:
        return Inputs()
    memo_key = (str(path), me, token, str(ledger), ledger_stamp, live, epoch, frozenset(held))
    hit = _remote_memo.get('inputs')
    if hit is not None and hit[0] == memo_key:
        # An archived chat un-archiving moves no replica row, no ledger row: re-read each parked chat per probe.
        if all(_chat_inert(profile, sid) for profile, sid in hit[2]):
            return hit[1]
        return Inputs()
    _remote_memo.pop('inputs', None)
    # The sweep's cards are a function of the replica rows the token certifies: a ledger commit (the sweep's own
    # observations, a poller's claim) or a live-set change re-judges them below, it does not re-read the replica.
    cards_key = (str(path), me, token, epoch)
    cached = _remote_memo.get('cards')
    if cached is not None and cached[0] == cards_key:
        cards = cached[1]
    else:
        _remote_memo.pop('cards', None)
        cards, _states = remote._sweep_snapshot(path, me)
        # Never trust a snapshot across a replacement or a write the sweep could act on. The hub's replica commits
        # several rows a second, almost none of them a card this host owes anything for: comparing the raw commit
        # stamp made the cold read (about 2 s of CPU) lose that race every time, so the proof was never published
        # and every pass paid it again (t_01a1dfa94f2251cd). The semantic token is what the proof is keyed on.
        after = fingerprint(path)
        if after is None or (getattr(after, 'ident', None), getattr(after, 'generation', None),
                             getattr(after, 'signature', None)) != identity or _replica_token(path, me) != token:
            return Inputs()
        _remote_memo['cards'] = (cards_key, cards)
    watch = _replica_watch.get(str(path))
    if watch is not None:  # a later non-terminal write to one of these (a reopened card) must not be skipped
        _replica_watch[str(path)] = (*watch[:2], frozenset(f'{owner}/{key}' for owner, key, _t, _l in cards))
    parked: set = set()
    if ledger_stamp is None:
        if cards:
            return Inputs()
    else:
        with reader(ledger) as conn:
            if cards and not _cards_settled(conn, cards, live, str(Path(path).resolve()),
                                            held=frozenset((r[0], r[1], r[2]) for r in held), parked=parked):
                return Inputs()
    digest = hashlib.sha256()
    digest.update(repr((token, epoch)).encode())
    for owner, key, task, links in cards:
        digest.update(repr((owner, key, remote._notice_state(task),
                            tuple((link.get('profile'), link.get('session')) for link in links))).encode())
    # The ledger's commit counter is not part of the key: ``_cards_settled`` above judges every owed link against the
    # ledger on EVERY probe, so a ledger write that un-settles anything makes the probe pending, and one that does not
    # leaves the pass nothing to do. The sweep's own observation rows used to re-arm it once per pass it ran.
    result = Inputs((str(path), me, digest.hexdigest(), str(ledger), frozenset(held), frozenset(parked)),
                    pending=False)
    _remote_memo['inputs'] = (memo_key, result, frozenset(parked))
    return result


def _replica_tasks_signature(path):
    """The replica's task rows as ``observe`` reads them (count, newest seq, seq total): unchanged across the sync
    pulls that commit only cursors or other collections. None when unreadable."""
    import contextlib
    import sqlite3

    from tui_gateway.kanban_remote_notices import _ro
    try:
        with contextlib.closing(_ro(path)) as conn:
            return tuple(conn.execute("SELECT COUNT(*), MAX(seq), TOTAL(seq) FROM replica WHERE coll='tasks'").fetchone())
    except (OSError, sqlite3.Error, TypeError):
        return None


def _replica_rows_after(path, seq):
    """``[(gw, key, task or None)]`` for the task rows written after ``seq`` (the hub numbers every write from one
    sequence; ``task`` None = undecodable). Raises ``sqlite3.Error``/``OSError`` when unreadable."""
    import contextlib
    import json

    from tui_gateway.kanban_remote_notices import _ro
    with contextlib.closing(_ro(path)) as conn:
        rows = conn.execute("SELECT gw, key, data FROM replica WHERE coll='tasks' AND seq > ?", (seq,)).fetchall()
    out = []
    for gw, key, raw in rows:
        try:
            task = json.loads(raw)
        except (TypeError, ValueError):
            task = None
        out.append((gw, key, task if isinstance(task, dict) else None))
    return out


def _row_print(task, me):
    """What ``observe`` and the sweep read of a terminal/withdrawn task row, or None when it cannot be told: the
    notice state (status and its time), the freshness time, whether the owner published a completion notice, the
    chats of this gateway it links and its withdrawal. A rewrite that changes none of these changes nothing either
    reads (the delivery text is only built for a notice that is not settled, which keeps the probe pending)."""
    from hermes_cli.kanban_withdrawal_notice import envelope
    from tui_gateway import kanban_remote_notices as remote
    if not me or not isinstance(task, dict):
        return None
    try:
        notice = task.get('completion_notice')
        withdrawal = envelope(task)
        return (task.get('status'), remote._notice_state(task), remote._fresh_at(task),
                isinstance(notice, dict) and 'event_id' in notice,
                tuple(remote._linked_chats(task, me)), repr(withdrawal) if withdrawal is not None else None)
    except Exception:  # health: allow BLE001 -- a row that cannot be read is unknown: it moves the token
        return None


def _substantive(gw, key, task, swept, path=None, me=None) -> bool:
    """A replica task row whose write the remote sweep (or ``observe``) may act on."""
    from hermes_cli.kanban_withdrawal_notice import envelope
    from tui_gateway.kanban_remote_notices import _notifiable
    if task is None:
        return True
    ident = (str(path), gw, key)
    if me is not None and gw == me:
        return False  # ``observe`` and the sweep read every gateway's rows but this one's own
    terminal = _notifiable(task) or not task.get('status')
    if not (f'{gw}/{key}' in swept or envelope(task) is not None or terminal):
        _row_prints.pop(ident, None)  # left the terminal set: its next terminal write is a new state
        return False
    print_ = _row_print(task, me)
    if print_ is None or path is None:
        return True
    if _row_prints.get(ident) == print_:
        return False
    if len(_row_prints) >= _ROW_PRINTS_MAX:
        _row_prints.clear()
    _row_prints[ident] = print_
    return True


def _moved_since(path, old, sig, swept, me=None):
    """True when a task row written between signatures ``old`` and ``sig`` may matter; None when unreadable."""
    if any(v is None for v in (*old, *sig)) or sig[1] < old[1] or sig[0] < old[0]:
        return True  # an empty table, a vanished row or a sequence that went backwards: no basis to skip
    try:
        rows = _replica_rows_after(path, old[1])
    except Exception:  # health: allow BLE001 -- unreadable rows are unknown: hot
        return None
    # Judge EVERY row (no short-circuit): each one updates its remembered print.
    moved = [_substantive(gw, key, task, swept, path, me) for gw, key, task in rows]
    return sig[0] - old[0] > len(rows) or any(moved)


def _replica_token(path, me=None):
    """A counter that moves only when a replica task row changes in a way ``observe``/the sweep can act on, else
    None (unknown). The first sight is 0: the first pass runs anyway."""
    sig = _replica_tasks_signature(path)
    if sig is None:
        return None
    prev = _replica_watch.get(str(path))
    if prev is None:
        _replica_watch[str(path)] = (sig, 0, frozenset())
        return 0
    old, token, swept = prev
    if sig != old:
        moved = _moved_since(path, old, sig, swept, me)
        if moved is None:
            return None
        token += bool(moved)
        _replica_watch[str(path)] = (sig, token, swept)
    return token


def _chat_inert(profile, sid) -> bool:
    """True when ``_deliver_link`` returns for this chat before it claims or writes anything, and will keep doing so
    until the chat itself changes: it is archived or moved to another gateway (``chat_skip_reason``). Eleven of the
    hub's twelve unsettled links were of archived chats, held for the whole 72 h late window, and kept the pass
    hot and cold every 30 s (t_01a1dfa94f2251cd). Unreadable or missing is not proof: False."""
    from tui_gateway import kanban_remote_notices as remote
    from tui_gateway.notice_fanout import chat_skip_reason
    try:
        with remote._session_db(profile, sid) as (db, _tip):
            return db is not None and chat_skip_reason(db, sid) is not None
    except Exception:  # health: allow BLE001 -- unknown is hot
        return False


def _cards_settled(conn, cards, live, source: str = '', now: float | None = None, held=frozenset(),
                   parked: set | None = None) -> bool:
    """True when every notice the sweep could deliver already has a settled ledger row, so the pass has nothing to
    deliver, claim, wait on or count. Anything unproven (a link without a settled row, an open chat, whose poller
    the sweep notes) keeps the pass hot. Blank and placement-placeholder links are dropped by the delivery
    itself, with no effect."""
    from hermes_cli.kanban_placement import is_placeholder_session
    from tui_gateway import kanban_remote_notices as remote

    now = time.time() if now is None else now
    for owner, key, task, links in cards:
        ref, state = f'{owner}/{key}', remote._notice_state(task)
        for link in links:
            profile, sid = link.get('profile'), link.get('session')
            if not profile or not sid or is_placeholder_session(sid):
                continue
            if (sid, ref, state) in held:
                continue  # a live foreign holder owns it: the sweep skips it (see ``_remote``)
            if remote._ledger_settled(conn, (sid,), ref, state) or _expired_unobserved(
                    conn, source, task, ref, state, now):
                # Nothing for the sweep to do, and an open chat changes none of it: ``_deliver_link`` ends at
                # ``_note_open_chat`` (a log line) for a chat this process holds, with no claim and no write. The
                # hub's owner chats are open in its gateway and own hundreds of settled cards each, so ONE such
                # link kept the whole pass hot and cold every 30 s (t_01a1dfa94f2251cd). The live set is part of the
                # memo key, so a chat unloading re-judges this.
                continue
            if sid in live:
                return False  # unsettled in an open chat: its poller claims it within seconds
            if parked is not None and _chat_inert(profile, sid):
                parked.add((str(profile), str(sid)))  # re-read by every probe, memo hit or not
                continue
            return False
    return True


def _expired_unobserved(conn, source: str, task, ref: str, state: str, now: float) -> bool:
    """True when the sweep drops this card's state without a ledger row, for good: it is past ``FRESH_S`` by its
    freshness time and this host either never observed it (seeded by the source's first snapshot) or first saw it
    more than ``LATE_MAX_S`` ago (``_deliver_fresh_cards``'s ``stale`` branch). Both only age further. A state with
    no observation row yet is unknown: ``observe`` writes it, so the pass runs. Eleven of these held the hub's pass
    hot every 30 s (t_01a1dfa94f2251cd)."""
    import sqlite3
    try:
        from tui_gateway import kanban_remote_notices as remote
        row = conn.execute('SELECT first_seen FROM observations WHERE source=? AND ref=? AND state=?',
                           (source, ref, state)).fetchone()
    except sqlite3.Error:
        return False
    if row is None:
        return False
    first, at = row[0], remote._fresh_at(task)
    observed = max(at, first) if at is not None and first is not None else at
    if not (observed is None or now - observed > remote.FRESH_S):
        return False
    return first is None or remote._late_verdict(now, first) != 'late'


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
