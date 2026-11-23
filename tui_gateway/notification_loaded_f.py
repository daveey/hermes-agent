"""f-only loaded-session scheduling. One reconciler, existing delivery/ACK owners.

The 30s process clock repairs missed wakes, establishes the observation baseline
and reconciles legacy aliases once per profile, never once per chat. Completion,
producer and parked-event executors remain independent of this source schedule.
"""
from __future__ import annotations

from collections import deque
from contextvars import copy_context
import json
import logging
import sqlite3
import threading
import time

RECONCILE_SECONDS = 30.0
log = logging.getLogger(__name__)


def enabled():
    from hermes_cli.config import load_config
    return load_config().get('display', {}).get('loaded_notification_f', False) is True


class Action:
    facts = True

    def __init__(self, scheduler, session, profile, wake):
        self.scheduler, self.session, self.profile, self.wake = scheduler, session, profile, wake
        self.context = copy_context()
        self.pending = deque()
        self.closed = False
        session['_notif_remote_batch'] = ()  # no legacy scan before the first shared capture
        session['_notif_local_boards'] = ()

    def due(self, *, legacy_due):
        with self.scheduler.lock:
            if self.closed or not self.pending:
                return False
            batch, boards = self.pending.popleft()
            self.session['_notif_remote_batch'] = batch
            self.session['_notif_local_boards'] = boards
            return True

    def retry(self):
        from tui_gateway import server
        from tui_gateway.kanban_remote_notices import _linked_chats

        batch = self.session.get('_notif_remote_batch')
        if not hasattr(batch, 'rows'):
            return  # Unknown recovery belongs to the unchanged 30s clock.
        chat = self.session.get('session_key')
        if not chat:
            return
        chats = {chat, *server._session_lineage(chat)}
        keys = [(gw, key) for gw, key, task in json.loads(batch.rows)
                if any(profile == self.profile and sid in chats
                       for profile, sid in _linked_chats(task, batch.me))]
        if keys:
            # Full captures contain other chats/profiles too. Incompleteness
            # cannot turn their population into a one-second keyed census.
            self.scheduler.invalidate(keys)

    def deadline(self, now, last_poll):
        with self.scheduler.lock:
            if self.pending:
                return now
        if self.session.get('_kanban_pending') or self.session.get('_kanban_claims'):
            return last_poll + 1.0
        return self.session.get('_notif_f_retry_at')

    def tick(self, sid, now, last_poll, error_retry_at, poll):
        from tui_gateway.notification_remote_retry import cancel, deadline
        due = self.due(legacy_due=False)
        retry_at = deadline(self.session, now)
        source_retry = self.session.get('_notif_f_retry_at')
        if any(at is not None and now >= at for at in (retry_at, source_retry, error_retry_at)):
            cancel(self.session)
            self.session.pop('_notif_f_retry_at', None)
            self.retry()  # fresh shared evidence, never an expired batch or a per-chat full scan
            error_retry_at = None
        continuation = bool(self.session.get('_kanban_pending') or self.session.get('_kanban_claims'))
        if not due and not (continuation and now >= last_poll + 1):
            return last_poll, error_retry_at
        try:
            if due:
                poll(sid, self.session, remote_only=not self.session['_notif_local_boards'])
            else:
                self.session['_notif_remote_batch'] = ()
                poll(sid, self.session, remote_only=True)  # custody/ACK only; no source polling
            return now, None
        except Exception:  # health: allow BLE001 -- retain independent action retry after a failed delivery pass
            log.exception('loaded notification action failed')
            return now, now + 1.0

    def close(self):
        with self.scheduler.lock:
            self.closed = True
            self.scheduler.actions.discard(self)
            self.scheduler.catchup.discard(self)
            self.pending.clear()
        self.session.pop('_notif_remote_batch', None)
        self.session.pop('_notif_local_boards', None)
        self.session.pop('_notif_local_incomplete', None)
        self.scheduler.event.set()


class Scheduler:
    def __init__(self, *, clock=time.monotonic):
        self.clock = clock
        self.lock = threading.Lock()
        self.pass_lock = threading.Lock()
        self.event = threading.Event()
        self.actions = set()
        self.dirty = set()
        self.catchup = set()
        self.known_keys = {}
        # scope -> {board slug: reason} whose local discovery failed. An explicit
        # incomplete/retry obligation, repaid only by the next unchanged 30s
        # reconciliation (never an accelerated census); cleared on recovery.
        self.local_incomplete = {}
        self.remote_incomplete = {}  # scope failures remain unknown until capture recovers
        self.hint_incomplete = False
        self.next_reconcile = 0.0
        self.stop = threading.Event()
        self.thread = None
        self.watch = None
        self.source_dirty = True
        self.last_seq = (0, '', '')
        self.source_identity = None

    def start(self, *, native=True):
        if native:
            from hermes_cli.fact_source_watch import NativeSourceWatch
            from tui_gateway.kanban_remote_notices import hub_db_path
            try:
                self.watch = NativeSourceWatch((hub_db_path(),), self.source_changed)
            except (OSError, NotImplementedError):
                log.warning('native remote notice hints unavailable; 30s reconciliation remains active', exc_info=True)
        self.thread = threading.Thread(target=_run, args=(self,), name='loaded-notice-reconciler', daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        self.event.set()
        if self.thread is not None:
            self.thread.join(5)
        if self.watch is not None:
            self.watch.close()

    def source_changed(self):
        with self.lock:
            self.source_dirty = True
        self.event.set()

    def _changed_keys(self):
        """Indexed sequence hints only; deletion/reset repair belongs to the clock."""
        from contextlib import closing
        from tui_gateway.kanban_remote_notices import _ro, hub_db_path
        path = hub_db_path()
        if not path.is_file():
            return ()
        with closing(_ro(path)) as conn:
            conn.execute('BEGIN')
            meta = dict(conn.execute("SELECT k,v FROM meta WHERE k IN ('gateway','cursor','head')"))
            stat = path.stat()
            identity = (str(path), stat.st_dev, stat.st_ino, meta.get('gateway'))
            if identity != self.source_identity:
                self.last_seq = (0, '', '')
                self.source_identity = identity
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type='index' "
                            "AND name='replica_poll_metadata'").fetchone() is None:
                return ()  # old replicas remain on bounded reconciliation, never a hint-triggered scan
            rows = conn.execute("SELECT gw,key,seq FROM replica INDEXED BY replica_poll_metadata "
                                "WHERE coll='tasks' AND (seq,gw,key) > (?,?,?) "
                                "ORDER BY seq,gw,key LIMIT 500", self.last_seq).fetchall()
            if rows:
                self.last_seq = (rows[-1][2], rows[-1][0], rows[-1][1])
            if len(rows) == 500:
                self.source_changed()
            return [(gw, key) for gw, key, _seq in rows if gw != meta.get('gateway')]

    def add(self, session, profile, wake):
        action = Action(self, session, profile, wake)
        with self.lock:
            self.actions.add(action)
            self.catchup.add(action)
        self.event.set()
        return action

    def invalidate(self, keys=None):
        with self.lock:
            if keys is None:
                self.source_dirty = True  # reset/health hints cannot borrow a census
            else:
                from tui_gateway.kanban_remote_notices_keyed import coalesce
                self.dirty.update(coalesce(keys))
        self.event.set()

    def step(self):
        # Only this process owner captures. Consumers receive immutable evidence
        # but retain their original serialized claim/delivery executors.
        with self.pass_lock:
            now = self.clock()
            with self.lock:
                scan = bool(self.actions) and (self.source_dirty or now >= self.next_reconcile)
                self.source_dirty = False
            if scan:
                try:
                    self.invalidate(self._changed_keys())
                    self.hint_incomplete = False
                except (OSError, ValueError, RuntimeError, sqlite3.Error):
                    # A remote hint is not authority over local discovery. Retry
                    # on the next native hint or unchanged reconciliation clock.
                    self.hint_incomplete = True
                    log.warning('remote notification hints unavailable', exc_info=True)
            with self.lock:
                reconcile = now >= self.next_reconcile
                if not self.actions or (not reconcile and not self.dirty and not self.catchup):
                    return
                # Cache identities, not deliverable evidence: a late chat gets a
                # fresh keyed capture (including deletions/changes), never stale
                # rows, expired observations, or another scope's batch.
                for action in self.catchup:
                    scope = (action.session.get('profile_home'), action.profile)
                    chat = action.session.get('session_key')
                    self.dirty.update(key for key, chats in self.known_keys.get(scope, {}).items()
                                      if chat in chats)
                keys = None if reconcile else tuple(self.dirty)
                self.dirty.clear()
                if reconcile:
                    # Reserve the budget after hint discovery, even if capture
                    # fails. No wake, chat registration or overflow can reset it.
                    self.next_reconcile = self.clock() + RECONCILE_SECONDS
                actions = tuple(self.actions)
            groups = {}
            batches = {}
            for action in actions:
                groups.setdefault((action.session.get('profile_home'), action.profile), []).append(action)
            for scope, group in groups.items():
                first = group[0]
                local = {}
                if reconcile:
                    from tui_gateway.notification_loaded_local import capture
                    failed = {}
                    try:
                        local, failed = first.context.run(capture, group)
                    except (OSError, ValueError, RuntimeError, sqlite3.Error):
                        log.warning('local notification discovery failed; retry next reconciliation', exc_info=True)
                        failed = {'*': 'board inventory unavailable'}
                    self._local_obligation(scope, group, failed)
                try:
                    source = first.context.run(self._source_identity)
                    if source in batches:
                        from copy import copy
                        batch = copy(batches[source])
                        if hasattr(batch, 'profile'):
                            batch.profile = first.profile
                    else:
                        batch = first.context.run(self._capture, first.profile, keys)
                        batches[source] = batch
                    self.remote_incomplete.pop(scope, None)
                except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
                    log.warning('loaded notification capture failed: %s', exc)
                    self.remote_incomplete[scope] = str(exc)
                    # Unknown remote work is retried by the unchanged 30s
                    # reconciliation; never accelerate a broken source census.
                    batch = ()  # local delivery only, never fall back to a source scan
                with self.lock:
                    if scope not in self.remote_incomplete:
                        self._remember(scope, first.profile, batch, keys)
                    live = [a for a in group if not a.closed]
                    for action in live:
                        self.catchup.discard(action)
                        if keys is None or len(action.pending) >= 32:
                            # Overflow drops discovery only. The unchanged
                            # process clock repairs it; durable claims survive.
                            action.pending.clear()  # discovery only: no claims are discarded
                        action.pending.append((batch, tuple(local.get(action, ()))))
                for action in live:
                    action.wake()

    def _remember(self, scope, profile_name, batch, keys):
        from tui_gateway.kanban_remote_notices import _linked_chats
        captured = ({(gw, key): tuple(sid for profile, sid in _linked_chats(task, batch.me)
                                      if profile == profile_name)
                     for gw, key, task in json.loads(batch.rows)}
                    if hasattr(batch, 'rows') else {})
        if keys is None:
            self.known_keys[scope] = captured
        else:
            known = self.known_keys.setdefault(scope, {})
            for key in keys:
                known.pop(key, None)
            known.update(captured)

    def _local_obligation(self, scope, group, failed):
        """Record or clear failed-board discovery; healthy boards were already delivered."""
        marker = tuple(sorted(failed))
        with self.lock:
            if failed:
                self.local_incomplete[scope] = dict(failed)
            elif self.local_incomplete.pop(scope, None) is not None:
                log.info('local notification discovery recovered for %s', scope)
            for action in group:
                if action.closed:
                    continue
                if marker:
                    action.session['_notif_local_incomplete'] = marker
                else:
                    action.session.pop('_notif_local_incomplete', None)
        if failed:
            log.warning('local notification discovery incomplete for %s: %s; retry next reconciliation',
                        scope, ', '.join(marker))

    @staticmethod
    def _source_identity():
        from tui_gateway.kanban_remote_notices import hub_db_path, ledger_path
        return str(hub_db_path().resolve()), str(ledger_path().resolve())

    @staticmethod
    def _capture(profile, keys):
        from tui_gateway.kanban_remote_notices import hub_db_path, ledger_path
        from tui_gateway.kanban_remote_notices_keyed import WakeBatch
        path = hub_db_path()
        return WakeBatch(path, ledger_path(), time.time(), profile, keys) if path.is_file() else ()


_owner_lock = threading.Lock()
_owner = None


def register(session, wake):
    """Lazy process lifetime owner. No feed, lifecycle or other family activation."""
    global _owner
    # Only initialized loaded sessions carry this captured routing field (None
    # explicitly means the launch profile). Do not infer it for legacy callers.
    if not enabled() or 'profile_home' not in session:
        return None
    from hermes_cli.profiles import get_active_profile_name
    with _owner_lock:
        if _owner is None:
            _owner = Scheduler()
            _owner.start()
        return _owner.add(session, get_active_profile_name() or 'default', wake)


def signal(profile_home, delta):
    """Feed callbacks carry hints only. No SQLite or delivery on the callback."""
    owner = _owner
    if owner is None or not enabled():
        return
    with owner.lock:
        if not any(a.session.get('profile_home') == profile_home for a in owner.actions):
            return
    keys = [fact.key for fact in delta.upserts] + list(delta.deletes)
    remote = [key.id for key in keys if key.kind == 'TaskFact' and key.id.count('/') >= 3]
    if delta.reset or delta.health or len(remote) != len(keys):
        owner.invalidate()
    else:
        owner.invalidate(remote)


def _run(owner):
    while not owner.stop.is_set():
        owner.event.clear()
        try:
            owner.step()
        except Exception:  # health: allow BLE001 -- a source failure must not kill the sole reconciler
            log.exception('loaded notification reconciliation failed')
            owner.stop.wait(1)
            owner.invalidate()
            continue
        with owner.lock:
            delay = max(0.0, owner.next_reconcile - owner.clock()) if owner.actions else None
            if delay is not None and (owner.dirty or owner.catchup):
                delay = min(delay, 1.0)  # failed keyed capture/catch-up, not a full census
        owner.event.wait(delay)
